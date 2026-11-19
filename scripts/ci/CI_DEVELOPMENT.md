# CI 开发与本地校验

> 测试范围、PR/nightly 策略和架构设计见 `scripts/ci/CI_DESIGN.md`。

CI 只有一条测试数据流：

```text
lint -> Planner -> PlanArtifact -> Executor -> JobResult -> Gate
```

Planner 读取四类输入：

```text
CiConfig   用户意图：资源、venv 映射、PR/nightly 范围
TestFacts  仓库事实：pytest 文件、device/rank/framework、integration manifest
TargetSlice 可选执行切片：例如 cpu 或 npu-a3
ChangeScope 执行范围：code / docs_only / delegated
```

变更路由与执行计划是两个层次：

```text
changed files -> routing.change_router -> image | execution
                                         │
                                         └─ Planner -> Executor -> Gate
```

`image_only` / `mixed` 由 Image PR 编排独占；普通 PR 只发布 0 Job 的 delegated
Plan 作为检查占位，不构建、不解析 candidate，也不接收 registry 凭据。

Executor 只消费 Plan 中的一个 Job；Gate 只消费 Plan 与 JobResult。两者都不重新选测，
也不解析镜像。lint-plan 先用 `scripts.ci.image refs` 把全部 container image 解析成
`cann<cann版本>-<hardware>-c<hash>` content ref，Plan 只携带这些不可变引用。
Normal PR/Nightly 的 job graph 不包含镜像构建节点。

## 目录

```text
scripts/ci/
  config/
    ci.yml            唯一 CI 配置：测试资源、镜像 build recipe、workloads、scopes
    envs/             Python 依赖清单
  image/              镜像发布域：构建、解析、registry、promotion
    plan.py           Git revision + image inputs -> content hash/tag/action
    build.py          resolve / resolve-candidate / build / verify -> ImageResult
    registry.py       SWR manifest、digest、alias
  execution/          测试执行域：事实、计划、执行、判定
    producer/
      ci_config.py    CiConfig loader
      facts.py        pytest 扫描 + integration suite manifest
      scan.py         pytest AST 事实
      planner.py      CiConfig + TestFacts -> PlanArtifact
    consumer/
      executor.py     执行一个 Job -> JobResult
      gate.py         Plan + JobResults -> Decision
      report.py       非阻断 timing summary
      runtime/
        env.py        CPU/NPU/GPU 探针与清理
        venv.py       内容寻址 venv
        model_dataset.py 模型/数据集的本地解析、缓存与下载
  routing/            跨域变更路由，不执行测试
    change_router.py  changed files -> docs/code/image/mixed
  contracts/
    artifacts.py      schema v4 artifact 编解码与校验
  tools/
    lint.py           通用 linter + CI 契约检查
  tests/              config / facts / contracts / planner / executor / gate
```

`image/` 与 `execution/` 是两个并行域，二者不互相导入。`routing/` 只负责把
changed files 路由到两个域；`contracts/` 和 `config/` 是共享边界。图构建和镜像
构建分别拥有自己的编排，测试执行域只接收不可变的 `image_ref`。

## 边界

| 阶段 | 输入 | 输出 | 不做什么 |
|---|---|---|---|
| lint | 源码、workflow、ci.yml、tests | annotations | 不选择用例 |
| Planner | CiConfig、TestFacts、scope、target slice、change scope | `plan.json` | 不安装环境、不执行测试、不解析物理镜像 |
| Executor | PlanArtifact + 一个 `job_id` | `job-result.json` | 不读取 CiConfig/TestFacts |
| Gate | PlanArtifact + 全部 JobResult | `decision.json` | 不重新选测 |

镜像发布链路只读取 `scripts/ci/config/ci.yml` 中 image 的 `ref`、`target` 和
`build`；测试配置加载器保留 `build` 字段但不解释它。Image PR 的外层 Plan 一次计算
全部受影响镜像的 identity、action 和 content tag；`ascend-image-pipeline.yml` 直接消费该
`image_plan`，只有手动触发时才在内部计算 identity。测试入口继续复用既有
Plan/JobResult/Gate 语义。

Planner 按 CPU/NPU/GPU 输出 matrix。matrix 项只包含 `job_id`、`image_ref`、
`runner_variable`、`runner`、`timeout_minutes`，完整 Job 只存在于 PlanArtifact。
Planner 和 `ci.yml` 中 Job/Step 超时统一使用分钟，Executor 在启动子进程时转换为秒。

Executor 执行前先写 `infra_error/executor-incomplete` 占位结果，结束时原子覆盖。即使
runner 被外部杀死，Gate 仍能区分“缺结果”和“执行未完成”。

## 镜像发布链路

```text
Image PR
  changed Dockerfiles -> selection -> affected images
  Dockerfile-only -> per image: build candidate -> tests
  Dockerfile+code -> CPU preflight once -> per image: build -> tests

Image Main
  main merge -> selected images -> per image: resolve candidate -> promote alias

测试 CI（PR/nightly/manual）
  base/current SHA -> content ref -> Plan -> CPU/NPU -> Gate
```

普通代码 PR 不等待镜像构建，也不查询 registry。所有 Dockerfile PR 由
`.github/workflows/ascend-image-pr.yml` 独占测试所有权；`selection.py` 根据
`resources.images.<name>.build.dockerfile` 只选择被修改的逻辑镜像。每个镜像调用
`ascend-image-pipeline.yml`，内部串行执行 build、candidate tests；不同镜像
由 matrix 并行执行，没有全局阶段栅栏。Dockerfile-only PR 跳过 CPU，混合 PR 先执行
一次 CPU preflight，失败时不构建 candidate。`Ascend-PR-CI` 检测到 Dockerfile 时只生成
delegated Plan，不重复跑用例。同仓 PR 自动运行，fork PR 需要维护者添加
`image-ci/run` 标签。

main 合并后，`.github/workflows/ascend-image-promote.yml` 用同一套 selection 规则选择
main 上修改的 Dockerfile 对应镜像，并在 promotion workflow 内完成
`resolve candidate -> promote alias`。每个单镜像单元只解析已测试 candidate，然后把
content tag promote 为 `cann9.0.0-a3-base`。candidate 不存在时该镜像直接失败，不创建
未经 Image PR 测试的替代镜像。手动 dispatch 使用 `images=all` 或逗号分隔的逻辑镜像列表。

其他 `docker/**` 文件不自动触发 Image PR，仍由普通 PR CI 看护。

`scripts/ci/image/**`、`ci.yml` 的 image build recipe 和 image workflow 变更不会自动
构建 candidate，避免 CI 维护 PR 占用数小时的镜像资源。这类变更必须在合并前手动执行
candidate 验证，并确认目标 content tag 已存在；否则 main promotion 会 fail closed。

镜像身份分为两层：

```text
content tag  cann9.0.0-a3-c<hash>   测试实际运行身份，不可变
alias        cann9.0.0-a3-base      发布/回滚入口，不参与 CI 正确性
```

普通 PR 使用 `github.event.pull_request.base.sha` 的 content tag；nightly/manual 使用
当前 checkout SHA 的 content tag；Image PR 使用 candidate digest。alias 必须包含 CANN
版本和硬件，支持多个版本长期并存。rank 数、lane、pytest 选择都不进入镜像 tag。

迁移到 content tag 前，旧 base 提交可能还没有 `resources.images.*.build`。此时普通 PR
会 fail closed，不会静默回退 alias。首次迁移先用 `Ascend-Manual-CI` 的显式
`image_ref` 完成 bootstrap，并在新的 main candidate 可解析后恢复普通 PR。

## 用例模型

只有两种 Job kind：

```text
pytest       单元、组件、adapter 测试
integration  tests/integration_tests 的训练回归
```

pytest suite 固定为三层：

| suite | 规则 | PR | nightly |
|---|---|---|---|
| `core` | 通用 engine/ops/runtime 等 | 全量 | 全量 |
| `model` | 模型族依赖测试 | 不跑 | 全量 |
| `adapter` | LLaMA-Factory / ms-swift 适配测试 | 全量 | 全量 |

integration case 的事实源是 suite runner 的 `--list-cases --json`。一个 case 一个 Job，
不把 case 的 framework、ranks、config、golden 再抄进 `ci.yml`。

## 运行范围

范围只由 `config/ci.yml` 的 `scopes` 决定：

| scope | pytest | integration | Job |
|---|---|---|---:|
| `pull_request` | `core=all`、`adapter=all` | `sft_2cpu`、`sft_2npu` | 7 |
| `nightly` | 全部 | 全部 3 case | 12 |
| docs-only | 无 | 无 | 0 |
| delegated | 无 | 无 | 0 |

执行切片使用通用的 `targets` 过滤，不增加第二套 scope：

| 切片 | pytest | integration | Job |
|---|---|---:|
| `pull_request targets=cpu` | `core=all`、`adapter=all` | `sft_2cpu` | 5 |
| `pull_request targets=npu-a3` | `core=all` | `sft_2npu` | 2 |

PR 不跑模型 pytest，因为不同模型族依赖的 venv 不同，安装成本和稳定性不可控；core 和
adapter 全量运行。PR 使用两个轻量 integration case 看护 CPU 和 NPU 主路径；完整模型和
16 卡 case 留给 nightly。

纯文档包括 `docs/**`、`*.md`、`*.rst`、`*.adoc`、`LICENSE`、`NOTICE`。空 diff、
分类失败或读取失败按代码变更处理。

## 远端资产

integration case 的 `TRAIN_ARGS.model` / `TRAIN_ARGS.dataset` 可配置一个远程主来源和
一个 `fallback` 来源；主来源解析或校验失败时才会切换到 fallback。实际来源会写入
`resolved_inputs.json`，便于区分命中主来源还是回退。

Hugging Face 来源应显式设置 `HF_ENDPOINT`（例如 `https://hf-mirror.com`），不要依赖
runner 的默认网络。远端 revision 必须固定到 commit；ModelScope 使用其仓库 commit，
不能复用 Hugging Face revision。

integration 结果上传前会删除 `training/`、`initial_checkpoint/`、`tensorboard/` 等临时
大目录，只保留 `actual.json`、`resolved_inputs.json`、`training.log` 和配置快照。Job 的
真实退出状态由最后的 `enforce result` 步骤判定，确保失败结果先上传再由 Gate 消费。

## 扩展点

- 新增硬件代际：增加 `resources.targets`，并提供该 target 的默认 image 和 lane。
- 新增逻辑镜像：增加 `resources.images`；需要按 scope、workload 或单个 test/case
  覆盖时，分别使用 scope `images[target]`、workload `images[target]` 或 override
  `image`。选择顺序是“显式 > scope > workload > target 默认”。
- 新增镜像构建 recipe：修改 `resources.images.<name>.build`，不修改
  Planner/Executor/Gate；该变更需在合并前手动构建并验证 candidate。
- 新增 lane：改 `resources.lanes`，lane 只绑定 target 和 runner 容量，不绑定 image。
  Planner 选择容量最小且满足 ranks 的 lane，不在 consumer 写平台分支。
- 新增 venv：改 `resources.venvs`，可用 `extends` 复用依赖链；依赖文件内容计入 digest。
- 新增 pytest：普通 CPU 用例无需 device marker，会自动归入 `cpu`；NPU/GPU 用例必须补
  对应的 `pytest.mark.npu` / `pytest.mark.gpu`，多进程加速器用例补
  `pytest.mark.multi_npu` / `pytest.mark.multi_gpu`。必要时增加 `model_venvs`、
  `framework_venvs`、`combo_venvs` 或文件 override。
- 新增 adapter：补 framework venv 映射和测试事实。PR 是否运行由 `scopes` 决定。
- 新增 integration case：在 suite 中增加 case；PR 使用 `explicit`，不会自动纳入。
- 新增 integration suite：在 `workloads.integration.suites` 增加 runner 和默认超时。
- 新增 platform：增加 target、默认 image、lane 和 workload timeout。integration case
  的 platform 来自 manifest；需要限定硬件代际时，在 manifest 增加 `target` 或
  `targets`，pytest 则在 path override 中增加 `targets`。

资源完全不存在于 CI 能力范围时，Planner 把路由写入 `coverage.unsupported_routes`；
资源存在但无法满足 lane/ranks 时 Planner 直接失败，不会静默丢路由。

## 常用命令

```bash
# 配置与测试事实
python3 -m scripts.ci.execution.producer.ci_config check
python3 -m scripts.ci.execution.producer.facts check

# PR / nightly / docs-only Plan 均可构建
python3 -m scripts.ci.execution.producer.planner --verify

# 静态检查
python3 -m scripts.ci.tools.lint
python3 -m scripts.ci.tools.lint --only contract
python3 -m scripts.ci.tools.lint --only config

# 从 Git revision 计算全部 container image 的不可变身份；不访问 registry
python3 -m scripts.ci.image refs \
  --revision <sha> \
  --output /tmp/image-refs

# 生成 PlanArtifact 和平台 matrix
python3 -m scripts.ci.execution.producer.planner \
  --image-ref "npu-default=<content-ref>" \
  --scope pull_request \
  --output /tmp/plan.json \
  --github-output /tmp/github-output

# Dockerfile PR 的 CPU preflight / NPU candidate 切片
python3 -m scripts.ci.execution.producer.planner \
  --scope pull_request \
  --targets cpu \
  --output /tmp/pr-cpu-plan.json

python3 -m scripts.ci.execution.producer.planner \
  --scope pull_request \
  --targets npu-a3 \
  --image-ref "npu-default=<candidate-ref>" \
  --output /tmp/pr-npu-plan.json

# 单 Job 诊断
python3 -m scripts.ci.execution.producer.planner \
  --scope pull_request \
  --target-job integration-base-suite-qwen3-5-moe-sft-2cpu \
  --output /tmp/plan.json

# 同一 Plan 选择多个 job：CPU/NPU 会在同一 run 内并行执行
python3 -m scripts.ci.execution.producer.planner \
  --scope pull_request \
  --target-job 'pytest-core-cpu-cpu-default-default,pytest-core-npu-npu-default-default' \
  --output /tmp/plan.json

# 执行一个 Job
python3 -m scripts.ci.execution.consumer.executor \
  --plan /tmp/plan.json \
  --job-id <job-id> \
  --output /tmp/ci-result/job-result.json

# 判定完整 Plan
python3 -m scripts.ci.execution.consumer.gate \
  --plan /tmp/plan.json \
  --results /tmp/ci-results \
  --output /tmp/decision.json \
  --summary /tmp/summary.md

# 全部 CI 单元与契约测试
python3 -m pytest scripts/ci/tests -q
```

## 约束

- integration Job 只能有一个 case，step spec 的 digest 必须与 runner manifest 一致。
- CI 执行 integration 时固定 `--skip-install --forbid-update-golden`。
- pytest 必须收集到测试；integration evidence 的 suite/case 必须与 Plan 一致。
- skip 必须带原因，且原因出现在 Plan 的 `allowed_skip_reasons` 中。
- 跨阶段只传带顶层 `schema_version` 和内容 digest 的 JSON artifact；嵌套对象不重复写版本。
