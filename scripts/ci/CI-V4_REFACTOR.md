# TorchTurbo CI V4 重构说明书

## 0. 文档目的

本文定义 CI V4 的 clean code 与轻量化目标、代码预算、模块边界、重构步骤
和验收标准。

基准：

- 分支：`ci/v4-dev-baseline`
- 代码范围：`scripts/ci/**/*.py`
- 不包含：`scripts/ci/tests/**`
- 不包含：`.github/workflows/**`
- 当前生产代码：6377 行
- 目标生产代码：不超过 4500 行
- 需要减少：约 1877 行，约 29%

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

1. `config.py`、`image_config.py` 分别解析 `ci.yml`，存在 schema 重复。
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
image            image config -> resolve/build/promote -> ResolvedImage
tools            lint、路径规则、JSON、CLI 小工具
```

明确删除的耦合：

- 执行域不再解析 `resources.images.*.build` 和 `publish`。
- 镜像域通过稳定 API 提供 image ID、target、kind 和 immutable ref。
- 执行域只消费 `ResolvedImage`，不读取 Dockerfile 或 registry。

## 4. 代码预算

文件预算用于指导改造；验收以总量和模块上限为准。

| 模块 | 当前 | 目标 | 减少 |
|---|---:|---:|---:|
| `execution/producer/config.py` | 662 | 400 | -262 |
| `execution/producer/planner.py` | 635 | 430 | -205 |
| `tools/lint.py` | 594 | 480 | -114 |
| `execution/consumer/executor.py` | 575 | 420 | -155 |
| `execution/producer/facts.py` | 483 | 360 | -123 |
| `execution/consumer/runtime/model_dataset.py` | 411 | 270 | -141 |
| `execution/finalization/gate.py` | 398 | 260 | -138 |
| `image/build_image.py` | 383 | 270 | -113 |
| `execution/consumer/runtime/device.py` | 262 | 230 | -32 |
| `execution/consumer/runtime/venv_manager.py` | 249 | 210 | -39 |
| `image/image_config.py` | 175 | 130 | -45 |
| `contracts/planner_contract.py` | 165 | 110 | -55 |
| `execution/producer/cli.py` | 153 | 80 | -73 |
| `contracts/executor_contract.py` | 149 | 100 | -49 |
| `image/registry.py` | 135 | 110 | -25 |
| `image/cli.py` | 130 | 70 | -60 |
| `tools/json_io.py` | 129 | 60 | -69 |
| `execution/consumer/process.py` | 78 | 70 | -8 |
| `contracts/image_contract.py` | 75 | 50 | -25 |
| `image/select_images.py` | 72 | 50 | -22 |
| `execution/producer/matrix.py` | 67 | 30 | -37 |
| `execution/finalization/timing.py` | 65 | 40 | -25 |
| `execution/consumer/pytest_job.py` | 65 | 55 | -10 |
| `tools/change_kind.py` | 62 | 45 | -17 |
| `execution/consumer/integration_job.py` | 52 | 45 | -7 |
| `contracts/base.py` | 41 | 30 | -11 |
| `tools/cli.py` | 40 | 35 | -5 |
| `tools/repo_paths.py` | 37 | 25 | -12 |
| `tools/hashing.py` | 19 | 15 | -4 |
| 其他 `__init__.py` | 16 | 20 | +4 |

分类预算：

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

目标：`contracts` 从 559 行降到约 350 行。

措施：

- `json_io.py` 只支持当前实际使用的类型：
  `str`、`int`、`bool`、`tuple`、`dict`、dataclass、`T | None`。
- 删除 `Contract` mixin 之外没有复用价值的通用 decode 分支。
- `planner_contract.py` 删除与 Planner 生成逻辑重复的校验。
- `executor_contract.py` 只校验 result 边界不变量：
  schema、kind、status、skip、task coverage、failure 形状。
- `gate.py` 不重复校验契约层已经保证的字段。
- `ImageBuildResult` 与 `ImageOutcome` 合并，避免两层相同字段。

删除项：

- `TestTask.kind`：由 `spec` 类型推导。
- `TestTask.ranks`：使用 `Job.runtime.ranks`。
- 重复的 `_fail` 和 `DiagnosticError` 实现。
- 未使用的通用 JSON 类型分支。

### R2. 配置与镜像黑盒

目标：`config.py` 从 662 行降到约 400 行。

措施：

- `execution/producer/config.py` 不再解析 `build` 和 `publish`。
- 执行域需要的 image 信息由镜像域通过稳定接口提供。
- `ci.yml` 中 `resources.images` 的 build 细节只由 `image/image_config.py`
  解释。
- `ci.yml` 基础字段仍由 `tools/lint.py` 同时验证执行域和镜像域。
- 删除逐字段 `_row(..., allowed_keys)` 的重复模式，只保留顶层未知字段
  失败和关键资源字段校验。
- 删除已经由 YAML 结构和类型提示保证的防御性检查。

目标接口：

```python
class ImageCatalog:
    def get(self, image_id: str) -> ImageSpec: ...
    def ids(self) -> tuple[str, ...]: ...
```

执行域只拿到：

```text
image_id
target
kind
ref
```

### R3. Facts 扫描收敛

目标：`facts.py` 从 483 行降到约 360 行。

措施：

- 合并重复的路径分类逻辑，只保留一套 `test_parts()`。
- 删除与当前路由无关的 marker、model family、process group 分支。
- rank 检测只保留：
  `require_npus`、`init_process_group`、`spawn`、`parametrize`。
- integration manifest 校验拆成：
  `load_manifest()`、`parse_case()`、`validate_case()`。
- 删除对同一字段的重复类型检查和二次错误包装。

### R4. Planner 数据化

目标：`planner.py` 从 635 行降到约 430 行。

措施：

- 用统一 selection 结构代替 `ScopeSelection` 与 `ImageSelection` 双模型。
- pytest job 和 integration job 都通过同一组：
  `select -> group -> materialize -> validate` 函数生成。
- 删除重复的 `image_override` 校验和 unused area 检查。
- 删除重复的 job ID 去重逻辑，生成时保证唯一。
- 将 `build_execution_plan()` 和 `build_planner_output()` 收敛为一个小工厂。
- 只保留当前 Scope 需要的 target filter 和 manual target-job 能力。

### R5. Executor 与 Gate 收敛

目标：`consumer` 加 `finalization` 从 2155 行降到约 1600 行。

措施：

- Executor 只保留：
  `prepare -> run tasks -> write result -> summary` 四条主路径。
- 删除重复的 provisional result 构造和版本文件写入，由结果构造函数统一。
- `gate.py` 只检查：
  Plan/Result 的 job 覆盖、runtime 一致性、task coverage、pytest evidence、
  integration artifact、allowed skip。
- 删除已经由 `load_planner_output()` 和 `load_job_result()` 保证的校验。
- `timing.py` 合并到 gate summary，或收敛为 40 行以内的小模块。

### R6. Runtime 轻量化

目标：`model_dataset.py` 从 411 行降到约 270 行，其他 runtime 小幅收敛。

措施：

- Hugging Face 和 ModelScope 共用：
  `_remote_args()`、`_remote_file()`、`_remote_error()`。
- 删除重复的 `snapshot_download` 调用分支。
- device probe/check/cleanup 只保留当前 NPU/CPU/GPU 实际路径。
- venv manager 删除重复的 ready 文件读取和错误包装。

### R7. 镜像域收敛

目标：`image` 从 895 行降到约 630 行。

措施：

- `build_image.py` 只保留 content identity、resolve、build、promote。
- `ImageOutcome` 与 `ImageBuildResult` 合并。
- `image_config.py` 删除重复的通用类型校验。
- `registry.py` 只保留 SWR token、manifest 查询和 alias promote。
- `image/cli.py` 复用 `tools/cli.py`，只保留命令编排。
- 删除未使用的 manifest 字段和调试输出。

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

- 固定当前 153 个测试为基线。
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

- 目标：约 6100 -> 约 5500。
- 处理 R2、R3。
- 完成 image 黑盒接口，删除执行域对 build/publish 的解析。

### Phase 3：Planner

- 目标：约 5500 -> 约 5100。
- 处理 R4。
- 验证普通 PR 7 Job、docs-only 0 Job、nightly 12 Job。

### Phase 4：Executor + Gate + Runtime

- 目标：约 5100 -> 约 4750。
- 处理 R5、R6。
- 验证失败结果、skip、timeout、artifact、pytest evidence。

### Phase 5：Image + Tools

- 目标：约 4750 -> 不超过 4500。
- 处理 R7、R8。
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
