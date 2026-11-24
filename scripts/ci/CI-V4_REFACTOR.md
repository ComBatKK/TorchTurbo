# TorchTurbo CI V4 重构说明书

## 0. 文档目的

本文定义 CI V4 的 clean code 与轻量化目标、代码预算、模块边界、重构步骤
和验收标准。

基准：

- 分支：`ci/v4-dev-baseline`
- 代码范围：`scripts/ci/**/*.py`
- 不包含：`scripts/ci/tests/**`
- 不包含：`.github/workflows/**`
- V4 起始生产代码：6377 行
- R7a 后当前生产代码：5626 行
- 目标生产代码：不超过 4500 行
- R7a 后剩余需要减少：约 1126 行，约 20%

统计命令：

```bash
find scripts/ci -type f -name '*.py' \
  -not -path '*/__pycache__/*' \
  -not -path '*/tests/*' \
  -print0 | xargs -0 wc -l | tail -1
```

## 1. 当前判断

当前 CI 的主体架构合理：

```text
ci.yml
  -> Producer / Planner
  -> ExecutionPlan
  -> Executor
  -> JobResult
  -> Gate
```

镜像域和执行域也已经分离。V4 不应该推翻这些边界。

当前不合理或过重的地方：

1. `config.py`、`image_config.py` 仍各自读取 `ci.yml`，但执行域不再解释
   image build schema。
2. 契约层、Planner、Gate 对同一组不变量重复校验。
3. plan/job/task 数据模型包含不再需要的冗余字段。
4. `config.py`、`planner.py`、`facts.py`、`executor.py` 单文件过大。
5. `model_dataset.py` 的 Hugging Face 和 ModelScope 分支重复。
6. `lint.py` 包含较多工具安装和仓库检查逻辑。
7. 多个 CLI 重复实现文件读取、JSON 写入和 GitHub Output。
8. workflow 重复执行 executor/gate 步骤，但这一项不计入 4.5K 预算。

V4 的目标不是把代码移到 tests、workflow、shell、文档或生成文件里，而是
删除不必要的状态、重复校验和无行为价值的抽象。

## 2. 设计原则

1. 契约层只做边界校验，不重复解释业务。
2. Producer 只负责生成 Plan，Consumer 只负责执行 Plan，Gate 只负责
   判断 Plan 与实际结果是否一致。
3. 同一事实只能有一个所有者。
4. 能从现有字段推导的值不新增字段。
5. 只保留当前真实使用的硬件、镜像和测试路由能力。
6. 不为假想需求预留字段、分支或抽象。
7. 不为了减少行数牺牲 fail-closed 行为。
8. 不为了减少行数删除安全审批、digest 校验或测试证据校验。

## 3. 目标架构

目录结构保持不变：

```text
scripts/ci/
  config/
  contracts/
  execution/
    producer/
    consumer/
    finalization/
  image/
  tools/
```

职责约束：

```text
contracts        JSON 边界和最小不变量
producer         ci.yml + facts -> ExecutionPlan
consumer         ExecutionPlan -> JobResult
finalization     ExecutionPlan + JobResult -> GateDecision
image            image config -> RuntimeImage / build / promote
tools            lint、路径规则、JSON、CLI 小工具
```

明确删除的耦合：

- 执行域不再解析 `resources.images.*.build` 和 `publish`。
- 镜像域通过 `RuntimeImage` 提供 image ID、target、kind 和 ref。
- 执行域只消费 `RuntimeImage` 并转换为 `ResolvedImage`，不读取 Dockerfile
  或 registry。

## 4. 代码预算

文件预算用于指导改造；验收以总量和模块上限为准。

| 模块 | 当前 | 目标 | 减少 |
|---|---:|---:|---:|
| `execution/producer/config.py` | 619 | 560 | -59 |
| `execution/producer/planner.py` | 592 | 580 | -12 |
| `tools/lint.py` | 590 | 480 | -110 |
| `execution/consumer/executor.py` | 495 | 495 | 0 |
| `execution/producer/facts.py` | 445 | 440 | -5 |
| `execution/consumer/runtime/model_dataset.py` | 328 | 320 | -8 |
| `execution/finalization/gate.py` | 339 | 300 | -39 |
| `image/build_image.py` | 279 | 270 | -9 |
| `execution/consumer/runtime/device_probe.py` | 21 | 21 | 0 |
| `execution/consumer/runtime/venv_manager.py` | 195 | 195 | 0 |
| `image/image_config.py` | 199 | 199 | 0 |
| `contracts/planner_contract.py` | 146 | 110 | -36 |
| `execution/producer/cli.py` | 153 | 80 | -73 |
| `contracts/executor_contract.py` | 149 | 100 | -49 |
| `image/registry.py` | 111 | 111 | 0 |
| `image/cli.py` | 121 | 121 | 0 |
| `tools/json_io.py` | 129 | 60 | -69 |
| `execution/consumer/process.py` | 159 | 159 | 0 |
| `contracts/image_contract.py` | 69 | 69 | 0 |
| `image/select_images.py` | 54 | 54 | 0 |
| `execution/producer/matrix.py` | 67 | 30 | -37 |
| `execution/finalization/timing.py` | 65 | 40 | -25 |
| `execution/consumer/pytest_job.py` | 65 | 55 | -10 |
| `tools/change_kind.py` | 62 | 45 | -17 |
| `execution/consumer/integration_job.py` | 52 | 45 | -7 |
| `contracts/base.py` | 41 | 30 | -11 |
| `tools/cli.py` | 40 | 35 | -5 |
| `tools/repo_paths.py` | 37 | 25 | -12 |
| `tools/hashing.py` | 13 | 13 | 0 |
| 其他 `__init__.py` | 16 | 20 | +4 |

分类预算：

下表为 V4 初始预算；各阶段实际结果见第 5 节。

| 分类 | 当前 | 目标 | 减少 |
|---|---:|---:|---:|
| contracts | 559 | 350 | -209 |
| tools | 752 | 600 | -152 |
| producer | 2000 | 1300 | -700 |
| consumer | 1692 | 1300 | -392 |
| finalization | 463 | 300 | -163 |
| image | 895 | 630 | -265 |
| misc | 16 | 20 | +4 |
| 总计 | 6377 | 4500 | -1877 |

## 5. 重构工作包

### R1. 契约层瘦身

目标：`contracts` 核心文件从 430 行降到 370-390 行；当前已到 390 行。
原 350 行目标调整为 390 行，差额由后续 Producer/Tools 阶段吸收。

措施：

- `json_io.py` 只支持当前实际使用的类型：
  `str`、`int`、`bool`、`tuple`、`dict`、dataclass、`T | None`。
- 删除 `Contract` mixin 之外没有复用价值的通用 decode 分支。
- `planner_contract.py` 删除与 Planner 生成逻辑重复的校验。
- `executor_contract.py` 只校验 result 边界不变量：
  schema、kind、status、skip、task coverage、failure 形状。
- `gate.py` 不重复校验契约层已经保证的字段。
- `ImageBuildResult` 与 `ImageOutcome` 合并，避免两层相同字段。
- `ImageBuildResult` 删除没有消费方的 `platform`、`git_commit_sha` 和
  `promoted` 字段。

删除项：

- `TestTask.kind`：由 `spec` 类型推导。
- `TestTask.ranks`：使用 `Job.runtime.ranks`。
- 重复的 `_fail` 和 `DiagnosticError` 实现。
- 未使用的通用 JSON 类型分支。

当前结果：

| 文件 | R1 前 | 当前 |
|---|---:|---:|
| `contracts/planner_contract.py` | 165 | 149 |
| `contracts/executor_contract.py` | 149 | 129 |
| `contracts/image_contract.py` | 75 | 71 |
| `contracts/base.py` | 41 | 41 |
| 合计 | 430 | 390 |

### R2. 配置与镜像黑盒

状态：已完成。

目标：执行域不再解释 `resources.images.*.build` 和 `publish`。

原 `config.py 662 -> 400` 目标不现实，调整为 `662 -> 620`。
一个文件里与 image 直接相关的代码只有约 80-90 行，其余代码属于
targets、runners、venvs、test areas 和 scopes。

实际措施：

- `image/image_config.py` 新增 `RuntimeImage` 和 `load_runtime_images()`。
- 运行时视图只包含 `image_id`、`target`、`kind`、`ref`。
- `ImageSpec` 仍只服务镜像构建，不暴露给执行域。
- `execution/producer/config.py` 删除 `CiImage` 和 `_load_images()`。
- `load_config()` 从镜像域加载运行时镜像信息，并继续校验 scopes 引用。
- `bind_image_refs()` 只负责注入解析后的 digest。
- `tools/lint.py` 不再重复调用 `load_specs()`，配置校验统一走
  `load_config()`。

接口：

```python
@dataclass(frozen=True)
class RuntimeImage:
    image_id: str
    target: str
    kind: str
    ref: str = ""
```

执行域只拿到：

```text
image_id
target
kind
ref
```

当前结果：

| 文件 | R2 前 | R2 后 | 变化 |
|---|---:|---:|---:|
| `execution/producer/config.py` | 662 | 619 | -43 |
| `image/image_config.py` | 175 | 214 | +39 |
| `execution/producer/planner.py` | 629 | 635 | +6 |
| `tools/lint.py` | 594 | 590 | -4 |
| 合计 | 2060 | 2058 | -2 |

R2 的收益是删除跨域 schema 泄漏，不是压缩总行数。镜像域增加的行数
基本抵消执行域删除的行数，净减少 2 行；总代码预算仍需由 R3-R5 完成。

### R3. Facts 扫描收敛

状态：已完成（安全收敛版）。

目标：`facts.py` 从 483 行降到约 450 行；实际 445 行。

原 360 行目标不可行。实测：

- 删除 framework/family 分类会让 12 个文件改变 suite。
- 删除 family 参与 `process_group` 会让 15 个文件改变分组，
  Task 从 38 组降到 29 组。
- integration `targets` 当前为空，但它是 A5/GPU 按 target 选择用例的
  扩展点。

实际措施：

- 合并 `_dotted_name()`、`_parametrized_ranks()` 到 `_ranks()`。
- 压缩 `_manifest()` 的错误路径，保持原有诊断语义。
- 压缩 integration manifest 头部校验和 `_targets()` 排版。
- 删除没有下游消费者的 `config_sha256`、`golden_sha256` 和
  `file_sha256`。
- 保留 config/golden 路径存在性校验，统一为 `_check_repo_file()`。
- 修复非法 `target` 被静默忽略的问题。
- 修复 `framework`/`platform` 非字符串时抛异常的问题。
- `compare=false` 不再额外要求 golden 文件。
- 保留 `frameworks`、`model_families`、`process_group`、`targets`。
- 保留 manifest schema、case 和 artifact path 的全部 fail-closed 校验。

验证：

- 162 个 pytest fact 与重构前完全一致；3 个 integration fact 除删除的
  hash 字段外完全一致。
- PR/nightly 的 Job ID、Task ID、image、runner、ranks 快照完全一致。
- 新增 module-level marker list、`world_size` parametrize、非法 target、
  manifest 类型错误和 `compare=false` 测试。

结果：`facts.py 483 -> 445`，净减少 38 行。少减的部分由 R4/R5 吸收。

暂不修改：

- 多设备文件的 module/helper rank 扫描不对称。
- `multi_*` marker 对 CPU rank 的影响。
- integration `targets` 保留，作为 A5/GPU 按 target 选择用例的扩展点。

### R4. Planner 数据化

状态：已完成（安全收敛版）。

目标：`planner.py` 从 633 行降到约 580 行；实际 592 行。

原 430 行目标不现实：

- `_pytest_jobs` 和 `_integration_jobs` 是实际的分组、runner 选择和
  Job 生成逻辑，不是可删除的样板代码。
- 删除 `image_override` 会破坏镜像 PR candidate tests。
- 删除 target filter / `target-job` 会破坏 image pipeline preflight 和
  手动调试。
- 删除 used area / used selection 检查会让 Scope 配置错误静默少跑用例。

实际措施：

- 提取 `_active_selections()`，复用 `build_plan` 和 `requested_images`
  的 selection + target 过滤。
- 将 integration case 选择从 per-selection dict 压平为
  `(selection, case)` 列表。
- 收敛 `_selected_plan` 的 unknown 检查，预计算 wanted set。
- 删除冗余的最终 Job ID 唯一性检查，保留 `_claim_job_id` 生成唯一 ID。
- 压缩 pytest/integration Job 生成中的重复排版。
- 保留 `image_override`、`targets`、`target-job`、used area 检查和
  空 suite 检查。

结果：`planner.py 633 -> 592`，净减少 41 行。

验证：

- PR 7 Job、nightly 12 Job。
- target slice、`target-job`、image override、同 target 双镜像的 Plan
  JSON 快照完全一致。
- 157 个测试通过。

剩余约 12 行留给 R4b；不通过删除行为继续压缩。

### R5. Executor 与 Gate 收敛

状态：已完成（安全收敛版）。

原目标：`consumer + finalization` 2155 -> 1600。
本阶段实际：2154 -> 2060，净减少 94 行。

1600 目标需要删除 timing、versions 或更多 fail-closed 检查，本阶段不做。

实际措施：

- `gate.py` 增加 `_decision()` 和 `_empty_summary()`，统一
  `GateDecision` 构造。
- 删除 `GateDecision.checks`，只保留 `summary` 和 `failures`。
- duplicate pytest route 检查改为 image-aware key
  `(image_id, path, target, device)`；同一 target 的不同 image 允许，
  同一 image 的重复路由拒绝。
- 保留 skipped-tasks 和 failed-detail 校验。`decide_plan()` 仍可直接
  接收 JobResult 列表，不能假定调用方已完成契约校验。
- `_passed_failures()` 使用局部 `add()` 去掉重复 `_failure()` 样板。
- 压缩 Executor 的 `main()`、`render_summary()` 和 task result 排版。
- 保留 `timing.py`、`versions.txt`、provisional result、pytest evidence、
  integration artifact 和 allowed skip 校验。

结果：

| 文件 | R5 前 | R5 后 | 变化 |
|---|---:|---:|---:|
| `execution/consumer/executor.py` | 574 | 539 | -35 |
| `execution/finalization/gate.py` | 398 | 339 | -59 |
| consumer + finalization | 2154 | 2060 | -94 |

验证：

- passed / missing / duplicate / extra / skip / pytest evidence /
  integration artifact / planner failure 的 GateDecision 行为一致。
- 161 个测试通过。

### R6. Runtime 轻量化

状态：已完成。

原目标：`model_dataset.py` 从 411 行降到约 270 行，其他 runtime 小幅收敛。
实际：6101 -> 5758，净减少 343 行。

措施：

- Hugging Face 和 ModelScope 共用：
  `_remote_args()`、`_remote_file()`、`_remote_error()`。
- 删除重复的 `snapshot_download` 调用分支。
- `model_dataset` 用字段表统一 model/tokenizer/dataset 解析。
- 删除原 install guard（torch 前后快照/check）。venv constraints 已固定
  torch/numpy，`versions.txt` 记录最终版本。
- `runtime/device_probe.py` 只保留 CPU/NPU/GPU 可用性断言，Executor 只负责
  执行 probe command。
- cleanup 从 venv 子进程移到 `process.cleanup_environment()`，只清理
  `/tmp/tblf-sft` 训练输出，不触碰 CI 结果目录。
- venv manager 删除 `extends` chain、多 spec 查找和重复 ready 读取，
  直接消费已解析的 `VenvSpec`。
- 删除无消费者的 `resources.venvs.*.constraints`。

结果：

| 文件 | R6 前 | R6 后 | 变化 |
|---|---:|---:|---:|
| `execution/consumer/runtime/model_dataset.py` | 411 | 328 | -83 |
| `execution/consumer/runtime/venv_manager.py` | 249 | 195 | -54 |
| `execution/consumer/runtime/device_probe.py` | 262 | 21 | -241 |
| `execution/consumer/process.py` | 78 | 159 | +81 |
| `execution/consumer/executor.py` | 539 | 495 | -44 |
| consumer + finalization | 2060 | 1719 | -341 |
| total | 6101 | 5758 | -343 |

验证：

- CPU 侧 `import torch` 冒烟通过。
- cleanup 完整输出保留、部分输出清理、CI 结果目录不被删除三条行为已验证。
- 164 个测试通过。

### R7. 镜像域收敛

状态：R7a 安全收敛完成；R7b 待做。

R7a 目标：不改 content identity、registry 查询、build、promote 语义，
只压缩重复样板和未使用字段。

R7a 结果：`image` 目录 894 -> 764（-130），
`contracts/image_contract.py` 71 -> 69（-2）。

R7a 措施：

- 删除 `Manifest.reference` 和 `Manifest.raw` 两个未使用字段。
- 压缩 `build_command()`、`max_jobs()`、`_run_build()`、outcome 和
  promote 的重复样板。
- 保留 `GIT_HTTP_USER_AGENT` 的 Dockerfile patch 形式，不改 content hash。
- 压缩 `image_config` 的校验分支和错误文案。
- `select_images` 与 `image/cli` 做轻量压缩；完整 CLI 统一归 R9。

R7b 待做：

- CLI 合并、ci.yml 解析共享、Dockerfile patch 前移等结构改造。
- 原 630 行目标不作为 R7a 验收标准；在当前边界内需要跨模块改造。

### R8. Tools 与 lint 收敛

目标：`tools` 从 752 行降到约 600 行。

措施：

- `lint.py` 合并：
  `check_contract()`、`check_config()`、`check_markers()` 的公共错误收集。
- 工具安装逻辑只保留 CI 必需工具。
- 删除重复的 YAML 读取和 workflow run 文本扫描。
- `change_kind` 只保留 docs/code/image/mixed 四类判断。

### R9. CLI 统一

目标：`producer/cli.py`、`image/cli.py` 共减少约 130 行。

措施：

- 统一使用 `tools/cli.py`：
  `repository_path`、`read_lines`、`write_json_output`、
  `append_github_output`。
- 统一参数解析和错误输出格式。
- 删除只被单一命令使用且没有复用价值的包装函数。

## 6. 实施阶段

### Phase 0：保护网

- 固定当前 157 个测试为基线。
- 每次重构只允许减少生产行数，不修改 CI 行为。
- 每阶段执行：

```bash
python3 -m pytest -q scripts/ci/tests
python3 -m scripts.ci.tools.lint
python3 -m compileall -q scripts/ci
git diff --check
```

### Phase 1：Contracts + CLI

- 目标：6377 -> 约 6100。
- 处理 R1、R9。
- 更新 `test_contracts.py` 和 CLI 测试。

### Phase 2：Config + Facts

- 状态：R2、R3 已完成。
- 结果：6377 -> 6236。
- R2 完成 image 黑盒接口；R3 安全收敛 38 行，保留路由和 Task
  分组语义。
- 原 Phase 2 目标 5500 未达到，差额由 R4-R8 吸收。

### Phase 3：Planner

- 状态：R4 已完成。
- 结果：6236 -> 6195。
- 保留 image override、target filter、`target-job` 和覆盖校验。
- 验证普通 PR 7 Job、docs-only 0 Job、nightly 12 Job。

### Phase 4：Executor + Gate + Runtime

- 状态：R5、R6 已完成。
- R5 结果：6195 -> 6101。
- R6 结果：6101 -> 5758。
- 验证失败结果、skip、timeout、artifact、pytest evidence。

### Phase 5：Image + Tools

- 状态：R7a 已完成；R7b、R8、R9 待做。
- 目标：5626 -> 不超过 4500。
- 如仍有差额，继续压缩 consumer/image，不回到 Planner 删除行为。
- 验证 image plan/build/promote 和所有 lint 检查。

## 7. 验收标准

必须同时满足：

1. 生产 Python 代码不超过 4500 行。
2. 不通过移动到 tests、docs、workflow、shell 或生成文件达标。
3. `scripts/ci/tests` 全部通过。
4. `python3 -m scripts.ci.tools.lint` 7 项全部通过。
5. `compileall` 和 `git diff --check` 通过。
6. 普通 code PR 仍生成 7 个 Job。
7. docs-only PR 仍为 0 Job。
8. Dockerfile PR 仍只构建受影响镜像。
9. Image PR 的 fork 审批必须先于 PR 代码执行。
10. 普通 PR 仍使用 immutable digest image ref。
11. 失败测试、缺失 result、非法 skip 仍然 fail closed。
12. nightly 和 manual scope 的 Job 数量不变。

## 8. 不允许的轻量化方式

- 不把生产逻辑移入测试辅助代码。
- 不删除失败检查来减少行数。
- 不删除 image digest 校验。
- 不删除 fork PR 审批。
- 不删除 Plan/Result 的 job 覆盖率检查。
- 不为假想硬件和镜像预留学生分支。
- 不为了减少文件数量合并不相关职责。
- 不用超长单行或压缩格式代替清晰代码。

## 9. 安全与正确性前置项

以下问题优先于行数压缩：

1. Image PR 的 fork approval 必须在 checkout、pip、lint 之前。
2. PR head checkout 必须设置 `persist-credentials: false`。
3. 明确 Dockerfile PR 是否只保留 Image PR 入口。
4. 明确 `main`、`ci/pr-replay-baseline` 和 promote 的分支策略。
5. 确认基础镜像 tag 是否可变；如果可变，将 base digest 纳入 content hash。

## 10. 最终结果

V4 完成后应达到：

```text
生产 Python 代码：<= 4500 行
契约边界：稳定且不重复校验
配置所有权：执行域和镜像域各自单一职责
执行链：Plan -> Execute -> Gate
扩展方式：新增 target/image/scope，不新增并行框架
```

行数是约束，不是最终目标。V4 的核心结果是更少的重复、更清晰的职责和
更低的维护成本。
