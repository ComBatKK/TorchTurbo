# TorchTurbo CI 设计

> 状态：当前 tests 与 CI 的设计基线
> 基线：`ci/image-refactor` @ `f930255`
> 日期：2026-11-18
> 配套开发手册：`scripts/ci/CI_DEVELOPMENT.md`
> 说明：本文只描述当前 tests 和已经落地的 CI；不再保留历史版本的 `model_e2e`、`catalog.yml`、`policies.yml` 和 `model_map.json` 抽象。
> 第 1～3 节是当前实现；第 4 节的结构已经落地，生产 scope 当前只启用 A3。

---

## 1. 当前 tests 方案

### 1.1 测试分层

当前 tests 分为两类：

| 类型 | 入口 | 事实来源 | 执行方式 |
|---|---|---|---|
| pytest | `tests/unit_tests` | 文件路径、marker、模型族/框架 import | Planner 按 suite/device/venv 分组后直接执行 pytest |
| integration | `tests/integration_tests` | suite runner 的 `--list-cases --json` | 一个 case 一个 Job，执行真实训练并对比 golden metrics |

pytest 固定分为三层，不使用按设备或按模型族继续拆 suite：

| suite | 归类规则 | 主要内容 | 当前文件数 |
|---|---|---|---:|
| `core` | 非模型族、非 adapter 的通用测试 | engine、ops、runtime、通用 attention/MoE 基础设施 | 86 |
| `model` | 模型族测试 | bailing、qwen、deepseek、kimi、glm 等模型实现 | 60 |
| `adapter` | `adapters/**` 或显式依赖 LF/Swift | LLaMA-Factory、ms-swift 适配层 | 16 |

当前基线：

```text
pytest files: 86 + 60 + 16 = 162
pytest routes: 88 + 64 + 16 = 168
integration cases: 3
```

`routes` 是“测试文件 × target”的可执行路由数，所以可以大于文件数。当前另有 1 条 GPU 事实路由没有对应 target，会进入 `coverage.unsupported_routes`，不会被静默丢弃。

### 1.2 pytest 事实

Planner 从 pytest 源码派生以下事实，不手工维护测试清单：

- 测试文件路径和所属 suite
- 使用的设备 marker，例如 `cpu`、`npu`、`multi_npu`
- 每个设备需要的 rank 数
- 模型族和 framework 依赖
- 是否属于 integration 测试
- 是否需要独立进程组

设备路由采用“CPU 低摩擦、加速器 fail closed”：

- 有显式 device marker 时，按 marker 路由。
- 没有 marker 且没有 NPU/GPU 代码证据时，默认归入 `cpu`；普通 CPU 多进程测试不强制补 marker。
- 检测到 NPU/GPU 代码证据但没有显式 marker 时，facts 扫描失败，并给出应补的 marker 示例。
- 目录只负责 `core` / `model` / `adapter` 分类，不参与设备选择。

adapter 测试依赖独立的 `swift` / `llamafactory` 环境，PR 和 nightly 均全量运行。

### 1.3 integration case

integration case 由 `tests/integration_tests/test_suite/base_suite.py` 声明，CI 不复制
framework、ranks、config 和 golden：

| case | framework | 设备 | ranks | PR | nightly |
|---|---|---|---:|---|---|
| `qwen3_5_moe/sft_2cpu` | Swift | CPU | 2 | 跑 | 跑 |
| `qwen3_5_moe/sft_2npu` | LLaMA-Factory | NPU | 2 | 跑 | 跑 |
| `bailing_v3/sft_16npu` | Swift | NPU | 16 | 不跑 | 跑 |

PR 使用 CPU 和 NPU 各一条轻量训练路径做框架和端到端看护；16 卡 case 只进入 nightly。

### 1.4 模型和数据集

integration case 的模型和数据集在运行时解析，不使用 CI 私有路径：

- 支持 Hugging Face 和 ModelScope 主来源 + fallback。
- Hugging Face 默认应设置 `HF_ENDPOINT=https://hf-mirror.com`。
- revision 固定到 commit，不能使用浮动的 branch/tag。
- 下载结果写入 `resolved_inputs.json`，记录实际来源和本地路径。
- 训练前将 case 中的远程路径替换为本地 resolved path，训练框架不需要感知 CI 资产逻辑。

---

## 2. 当前 CI 方案

### 2.1 主链路

```text
ci.yml + TestFacts
        │
        ▼
      lint
        │
        ▼
     Planner ──────► PlanArtifact
                         │
                         ▼
                      Executor
                         │
                         ▼
                     JobResult
                         │
                         ▼
                        Gate
                         │
                         ▼
                      Decision
```

职责边界：

- `ci.yml`：资源、venv、超时、PR/nightly 范围。
- `TestFacts`：从 tests 自动派生的测试事实。
- `Planner`：把 scope、可选的 target 执行切片和测试事实编译为自包含的 Plan，不安装环境、
  不执行测试，也不知道 base/candidate 的来源。
- `Executor`：消费一个 Plan 和一个 `job_id`，不读取 `ci.yml` 或 TestFacts。
- `Gate`：只消费 Plan 和 JobResult，检查预期覆盖是否全部执行，不重新发现测试。

代码域也与数据流同构：

```text
routing/change_router.py
        │
        ├── image/       candidate 构建、smoke、promotion
        └── execution/   producer -> consumer -> gate

config/ + contracts/    两个域共享的配置与 artifact 契约
tools/lint.py           横切质量检查，不参与选测
```

`image/` 不导入 `execution/`，`execution/` 也不导入镜像构建实现；两者的唯一运行时
交界是不可变 `image_ref`。

### 2.2 配置边界

测试执行配置是：

```text
scripts/ci/config/ci.yml
```

它分为三层：

```text
resources   target、镜像、venv、runner lane
workloads   pytest/integration 的 venv 映射和超时
scopes      pull_request / nightly 选择哪些 suite 和 integration case
```

Job 与 Step 的超时统一使用分钟；Executor 只在设置进程计时器和 deadline 时转换为秒。

镜像构建配置也放在同一个文件的 `resources.images.<name>.build`：

```yaml
resources:
  images:
    npu-default:
      target: npu-a3
      kind: container
      ref: swr.../torchturbo_npu:cann9.0.0-a3-base
      build:
        dockerfile: docker/npu/Dockerfile_aarch64
        context: docker/npu
        platform: linux/arm64
        cann_version: "9.0.0"
        revision: 1
        args: {}
```

测试配置加载器允许 `build` 字段但不解释它；只有 `scripts/ci/image` 读取构建语义。
`ci_config.py`、Planner、Executor 和 Gate 都不解析 Dockerfile 或 digest。workflow 在
lint-plan 阶段把逻辑 image 解析成不可变 content ref，再通过 Planner 的参数覆盖写入
Plan，因此仍然保持镜像构建域和测试域解耦。

镜像身份有两层，不能混用：

```text
content tag  cann9.0.0-a3-c<hash>   CI 实际运行的不可变身份
alias        cann9.0.0-a3-base      人类可读的发布别名，只用于回滚和发布
```

普通 PR 使用 `base.sha` 对应的 content tag；nightly/manual 使用当前 SHA 对应的
content tag；Image PR 使用 candidate digest。任何测试入口都不依赖 alias 是否已经
promote。

当前资源规模：

| 类型 | 数量 | 内容 |
|---|---:|---|
| target | 2 | `cpu`、`npu-a3` |
| image | 2 | `cpu-default`、`npu-default` |
| venv | 5 | `default`、`qwen4_exp`、`lf`、`swift`、`qwen4_exp_swift` |
| lane | 5 | CPU default、NPU 2/4/8/16 卡 |

Planner 先按 device 与 scope 选择 target，再选择逻辑 image，最后按 rank 选择满足容量
且最小的 lane。资源不存在时记录 unsupported route；target、image 或 lane 缺失时直接
失败。Planner 把 workflow 传入的不可变 content ref 写入 Job runtime，自己不查询
registry；没有 override 时只使用逻辑 image 的默认 ref。

### 2.3 运行范围

| scope | pytest | integration | Job 数 |
|---|---|---|---:|
| `pull_request` | `core=all`，`model=none`，`adapter=all` | `sft_2cpu`、`sft_2npu` | 7 |
| `nightly` | `core/model/adapter=all` | 全部 3 case | 12 |
| docs-only | 无 | 无 | 0 |
| delegated | 无 | 无 | 0 |

PR 的 7 个执行 Job：

```text
integration-base-suite-qwen3-5-moe-sft-2cpu
integration-base-suite-qwen3-5-moe-sft-2npu
pytest-adapter-cpu-cpu-default-lf
pytest-adapter-cpu-cpu-default-qwen4-exp-swift
pytest-adapter-cpu-cpu-default-swift
pytest-core-cpu-cpu-default-default
pytest-core-npu-npu-default-default
```

nightly 的 12 个 Job 由 9 个 pytest Job 和 3 个 integration Job 组成。16 卡 case 不出现在 PR Plan 中。

### 2.4 Job 粒度

只有两种 Job kind：

- `pytest`：按 `(suite, target, image, venv)` 聚合；同一聚合内再按进程组和 marker 拆分 Step。
- `integration`：一个 case 一个 Job，避免 ranks、venv、timeout 和失败影响互相污染。

Job ID 由 suite、设备和 venv 等稳定字段生成，手动单点执行时直接使用 Plan 中的 Job ID。

### 2.5 Workflow 入口

| Workflow | 触发 | 作用 |
|---|---|---|
| `ascend-pr.yml` | `pull_request`、`workflow_dispatch`、`workflow_call` | 主入口，统一执行 PR 和 nightly scope |
| `ascend-manual.yml` | `workflow_dispatch` | 复用主链路，单点执行一个 Plan Job |
| `ascend-nightly.yml` | 手动 dispatch，cron 暂时关闭 | 以 nightly scope 调用主入口 |
| `ascend-image.yml` | `workflow_call`、手动 dispatch | 可复用的镜像构建/解析引擎，不运行测试 |
| `ascend-image-pr.yml` | `pull_request_target` 的 Dockerfile 变更 | 唯一的 Dockerfile PR 入口：可选 CPU 预检、构建 candidate、smoke、NPU 测试和最终 Gate |
| `ascend-image-promote.yml` | `main` push 的镜像输入变更、手动 dispatch | 只解析已测试 candidate，smoke 后 promote alias；不重建 |

所有测试入口复用同一条 `Plan -> Executor -> Gate` 链路，没有第二套测试选择逻辑。
镜像构建引擎 `ascend-image.yml` 不生成 Plan。Image PR 通过通用的 `pull_request` scope
加 target 切片复用 `Plan -> Executor -> Gate`，不引入第二套测试选择逻辑。

镜像 PR 与普通 PR 是两条独立链路：

```text
Dockerfile-only PR        -> build candidate -> smoke -> NPU 测试
Dockerfile + 代码 PR      -> CPU 预检 -> build candidate -> smoke -> NPU 测试
普通代码 PR               -> base SHA content ref -> CPU/NPU 测试，不构建镜像
main 合并                 -> resolve 同一个 candidate -> smoke -> promote alias
nightly                   -> 当前 SHA content ref -> 全量 pytest + 全部 integration
```

同仓镜像 PR 自动运行；fork 镜像 PR 必须由维护者添加 `image-ci/run` 标签，未审批时
approval gate 失败。普通 PR 永远不接收 SWR 凭据。

Image PR 的自动触发路径只有 `docker/**/Dockerfile*`。只修改 Dockerfile 的 PR 跳过
CPU 预检；Dockerfile + 代码的混合 PR 先执行 CPU 切片，再构建 candidate，最后执行 NPU
candidate 切片。`Ascend-PR-CI` 检测到 Dockerfile 变更时生成 0 Job 的 delegated Plan，
只承担分支检查占位，不再重复执行任何用例。

`scripts/ci/image/**`、`ci.yml` 的 image build recipe 以及 image workflow 自身的变更
不会自动构建 candidate，避免普通 CI 维护 PR 触发数小时的镜像任务；这些变更需要在合并
前手动执行 candidate 验证，并确认目标 content tag 已存在。否则 main promotion 会按
fail-closed 策略直接失败，不会发布未经验证的镜像。smoke 使用
`CI_IMAGE_SMOKE_RUNNER`（未配置时回退到 `CI_NPU_2_RUNNER`）。NPU candidate 切片只跑
真正使用 candidate 镜像的 NPU core pytest 和 `qwen3_5_moe/sft_2npu`，共 2 个 Job；混合
PR 的 CPU 切片跑 core、adapter 和 `sft_2cpu`，共 5 个 Job。

### 2.6 执行与环境

- Executor 在执行前写入 `infra_error/executor-incomplete` 占位结果，结束时原子覆盖，runner 被杀死时 Gate 能区分“缺结果”和“执行失败”。
- CPU Job 使用宿主 runner；NPU Job 使用 Plan 中不可变的 content ref。
- 每个 Job 在局部 venv 中安装依赖，并在 pytest 前后做 torch/设备 probe，防止环境被静默替换。
- NPU Job 结束或失败后执行清理，删除训练临时目录和残留进程。
- pytest 结果同时写 JUnit 和结构化 JobResult；integration 结果保留 `actual.json`、`resolved_inputs.json`、`training.log` 和配置快照。
- 大目录 `training/`、`initial_checkpoint/`、`tensorboard/` 在 artifact 上传前删除。

### 2.7 Gate

Gate 的核心规则是 fail closed：

- Plan 中出现的每个 Job 都必须有对应 JobResult。
- pytest 的期望文件和实际执行文件必须一致。
- 计划选择的 integration case 必须全部实际执行。
- 不允许未知 skip reason、缺 artifact、失败/超时结果被当成通过。
- 文档变更生成 0 个 Job 时，Gate 直接通过。

### 2.8 扩展点

| 需求 | 修改位置 |
|---|---|
| 新增硬件代际 | `resources.targets`，并提供 target 的默认 image 和 lane |
| 新增逻辑镜像 | `resources.images`，再由 scope/workload/test 覆盖选择 |
| 新增镜像构建版本 | `resources.images.<name>.build`；只影响 Image CI |
| 新增 runner/rank lane | `resources.lanes`，lane 只绑定 target，不绑定 image |
| 新增 venv | `resources.venvs`，可用 `extends` 复用依赖 |
| 新增 pytest | 补 marker 和依赖事实；必要时补 `framework_venvs` / override |
| 新增 adapter | 补 framework venv 映射；PR 是否运行由 scope 决定 |
| 新增 integration case | 在 suite manifest 中增加 case；PR 显式列入，nightly 自动全量 |
| 新增 integration suite | 在 `workloads.integration.suites` 增加 runner 和超时 |

### 2.9 当前限制和后续增强

- PR 不跑 model pytest；adapter 全量在 PR 与 nightly 执行。
- Dockerfile-only PR 跳过 CPU；Dockerfile + 代码的混合 PR 采用
  `CPU -> candidate -> NPU`，CPU 失败时不会开始镜像构建。
- NPU 2 卡资源池目前只有一个在线 runner 时，NPU pytest 和 NPU integration 会串行，`max-parallel: 2` 只表示允许并发，不创造 runner 容量。
- nightly 的 cron 暂时关闭，当前只做 Plan 验证。
- 普通 PR 使用 base SHA 对应的 content tag，不接收 SWR 凭据，也不等待镜像构建。
- Dockerfile PR 由 `Ascend-Image-PR` 独占测试所有权；混合 PR 串行执行 CPU 预检、
  candidate 构建/NPU 测试。fork PR 需要 `image-ci/run` 标签。
- `Ascend-Image-Promote` 只解析同一个 candidate，smoke 成功后才 promote；candidate 缺失
  时 fail closed，不临时构建替代镜像。
- nightly/manual 只消费当前 SHA 对应的 content tag，不构建、不 promote 镜像。
- Dockerfile PR 必须把 `Ascend-Image-PR-Gate` 作为测试必过项；普通 PR 的
  `Ascend-PR-Gate` 在 Dockerfile PR 上只是 delegated 占位，不能替代 Image Gate。
- 模型 index 和全部 shard 的严格校验、下载超时/错误分类、`resolved_inputs.json` 的耗时和大小、ModelScope fallback 与 HF golden 一致性仍是非阻塞增强项。
- 4 卡 lane 已声明但暂未纳入当前验收；16 卡 lane 已通过 manual 真机验证。

---

## 3. 验收基线

当前已验证：

- 真实 PR 事件生成 7 个执行 Job，CPU/NPU core、adapter pytest 和两条 integration 均通过，Gate 成功。
- `2cpu`、`2npu` manual 单点通过。
- 16 卡 case manual 单点通过。
- `pull_request + targets=npu-a3` 切片为 2 个 NPU Job：core pytest 和 `sft_2npu`。
- nightly Plan 为 12 个 Job，16 卡只进入 nightly。
- docs-only Plan 为 0 个 Job。
- CI 单元与契约测试 119 项通过，7 项 lint 通过。

常用本地校验：

```bash
python3 -m scripts.ci.execution.producer.ci_config check
python3 -m scripts.ci.execution.producer.facts check
python3 -m scripts.ci.execution.producer.planner --verify
python3 -m scripts.ci.tools.lint --only contract
python3 -m pytest -q scripts/ci/tests
```

---

## 4. 资源模型：target / image / lane 解耦

> 状态：已实现。生产配置当前只声明 `cpu` 和 `npu-a3`；A5 通过临时配置单测验证扩展能力，尚未接入生产 runner。

长期需要同时支持 A3、A5 等硬件代际和多个镜像版本。核心原则：

```text
lane   只描述在哪里跑、有多少卡
image  描述用什么环境跑
target 描述 device + hardware 的组合
job    在 target 上选择 image、venv、ranks
```

### 4.1 资源模型

| 概念 | 职责 | 不负责 |
|---|---|---|
| `target` | `device + hardware`，例如 `cpu`、`npu-a3`、`npu-a5` | 不描述镜像版本 |
| `image` | 某个 target 的运行环境，例如 `npu-a3-base-v1` | 不描述 runner 容量 |
| `lane` | 某个 target 的 runner 和卡数容量 | 不绑定具体镜像版本 |
| `venv` | Python 依赖环境 | 不决定硬件 target |

建议配置结构：

```yaml
resources:
  targets:
    cpu:
      device: cpu
    npu-a3:
      device: npu
      hardware: a3
    npu-a5:
      device: npu
      hardware: a5

  images:
    cpu-default:
      target: cpu
      kind: host
      ref: python:3.11
      default: true
    npu-a3-base:
      target: npu-a3
      kind: container
      ref: swr.../torchturbo_npu:cann9.0.0-a3-base
      default: true
    npu-a5-base:
      target: npu-a5
      kind: container
      ref: swr.../torchturbo_npu:cann9.0.0-a5-base
      default: true

  lanes:
    npu-a3/default-2:
      target: npu-a3
      capacity_slots: 2
      runner_variable: CI_NPU_A3_2_RUNNER
      runner: linux-aarch64-a3-800i-2
    npu-a5/default-2:
      target: npu-a5
      capacity_slots: 2
      runner_variable: CI_NPU_A5_2_RUNNER
      runner: linux-aarch64-a5-800i-2
```

`lane` 不再包含 `image`。同一 target 的 2/4/8/16 卡 lane 可以复用任意兼容镜像。

### 4.2 image 选择与发布边界

Planner 在生成 Job 时只选择逻辑 image，优先级从具体到通用：

```text
test/case 显式 image
    > scope image[target]
    > workload/suite image[target]
    > target 的默认 image
```

示例：PR 使用 A3 稳定镜像，nightly 额外覆盖 A5 和新版 A3 镜像。

```yaml
scopes:
  pull_request:
    targets: [cpu, npu-a3]
    images:
      npu-a3: npu-a3-base-v1

  nightly:
    targets: [cpu, npu-a3, npu-a5]
    images:
      npu-a3: npu-a3-base-v2
      npu-a5: npu-a5-base-v1
```

pytest 文件或 integration case 可以进一步覆盖：

```yaml
workloads:
  pytest:
    overrides:
      tests/unit_tests/ops/test_new_a5_op.py:
        image: npu-a5-base-v1
        targets: [npu-a5]
  integration:
    suites:
      base_suite:
        overrides:
          bailing_v3/sft_16npu:
            image: npu-a5-base-v1
```

解析规则必须是确定性的：

- 一个 Job 最终只能解析出一个 image。
- image 的 target 必须与 Job 的 target 一致。
- 每个 target 默认只能有一个 `default: true` 镜像。
- 显式选择的镜像不存在或不兼容时 Planner 失败，不静默回退。
- Planner 只使用 image 的 target/ref/default，不解释 `build`，也不查询 registry。

Planner 不查询 registry，也不解析 digest。normal PR/nightly/manual 由 lint-plan 先调用
`scripts.ci.image refs`，从 Git revision 计算全部 container image 的 content ref，再通过
`--image-ref` 覆盖逻辑 image；Image PR 则直接传入 candidate 的 digest ref。最终 Job
runtime 只保存不可变 ref。测试 job graph 始终是：

```text
lint -> Plan -> CPU/NPU -> Gate
```

镜像构建和 alias 发布走独立链路。Dockerfile PR 是唯一入口；混合 PR 在 Image 链路内
按 `CPU -> build -> NPU` 串行：

```text
Dockerfile-only
  content tag -> build candidate -> NPU smoke -> NPU candidate tests

Dockerfile + code
  CPU tests -> content tag -> build candidate -> NPU smoke -> NPU candidate tests

main merge
  resolve same content tag -> NPU smoke -> promote alias
```

- Normal PR 使用 `base.sha` 的 content tag；nightly/manual 使用当前 SHA 的 content tag。
- 测试 CI 不增加 image job，不接收 SWR 凭据，也不因镜像构建耗时改变执行拓扑。
- Image PR 使用内容 tag 构建 candidate；同内容已存在时直接复用。
- Dockerfile-only PR 跳过 CPU 预检；混合 PR 先跑 CPU 切片，失败时不构建 candidate。
- Image PR smoke 后用 `pull_request + targets=npu-a3` 执行 PR 的 NPU 切片。
- Image Main 只解析 PR candidate，缺失时直接失败；smoke 通过后才 promote。
- Dockerfile PR 的测试由 Image PR 独占；普通 PR workflow 只生成 delegated Plan。
- image engine / build recipe 变更走手动 candidate 验证，不自动触发昂贵的 Image PR。
- nightly 只运行当前 content tag 上的全量测试，不构建、不 promote 镜像。
- alias 必须使用 `cann<cann_version>-<hardware>-...` 前缀，使多个 CANN 版本可以并存。
- 禁止使用 `base-latest` 等无版本 alias。alias 是发布和回滚入口；CI 正确性只依赖
  `cann<cann_version>-<hardware>-c<hash>` content tag。

### 4.3 target 选择

pytest 和 integration 都需要声明 target 要求：

- 通用 `npu` 测试表示“可在当前 scope 允许的 NPU target 上运行”。
- `npu-a5` 表示只在 A5 上运行。
- pytest 文件通过 `workloads.pytest.overrides.<path>.targets` 声明硬件 target；如果只声明显式 image，则由 image 的 target 反推。
- integration case 在 manifest 中声明 `target` 或 `targets`。
- PR scope 默认只选 A3；nightly 可以选择 A3 + A5。

Planner 计算：

```text
selected_targets = test/case target 要求 ∩ scope.targets
```

交集为空时，测试不进入该 scope 的 Plan；Planner 不会把 A5 专用测试回退到 A3，也不会忽略配置中不存在的 target。

### 4.4 Job 和执行矩阵

Job 的 runtime 增加 target 维度：

```text
Job.runtime = device + target + image + lane + venv + ranks
```

Planner 仍按以下维度聚合 pytest：

```text
(suite, target, image, venv)
```

因此：

- 新增镜像版本不会增加 lane。
- 新增 A3/A5 会增加 target 和对应 lane，但不会把 image 乘进 lane。
- 只有 scope、case 或 test 明确选择多个 target/image 时，Job 数才会增加。
- matrix 仍然只传 `job_id`、`image_ref`、`runner_variable`、`runner`、`timeout_minutes`，workflow 不参与 image 选择。
- Job ID 暂时保持原有 `suite/device/image/venv` 命名，避免 manual 命令失效；具体硬件代际记录在 `runtime.target` 中。

### 4.5 当前落地状态

当前分支已经完成以下迁移：

1. `ci.yml` 升级到 schema 2，使用 `resources.targets`、`resources.images`、`resources.lanes`。
2. `lane.image` 已删除，lane 只绑定 target 和 runner 容量。
3. Planner 使用统一 image 解析顺序和 `lane_for(target, ranks)`。
4. `Runtime` 增加 `target`，artifact schema 升级到 3。
5. 生产配置保留 `cpu` / `npu-a3`，PR 为 7 Job、nightly 为 12 Job。
6. A5 通过临时配置单测覆盖；生产配置不声明不存在的 A5 runner。
7. 测试入口使用 `cann9.0.0-a3-c<hash>` content tag；`cann9.0.0-a3-base` 只做发布别名。
8. Image PR 用通用 target 切片执行 CPU/NPU；main 只解析 candidate，不再重建镜像。

后续增强：

- 接入真实 A3/A5 多镜像版本；每个 target 继续用独立的 CANN 版本 + content tag。
- 如需 A5 pytest，优先复用 `overrides.targets`；确有长期差异时再增加 test marker。

### 4.6 代码影响

- `ci_config.py`：已实现 target/image/lane 模型、scope/workload image 覆盖和交叉校验。
- `planner.py`：已实现 target 展开、统一 image 解析、`lane_for(target, ranks)` 和 runtime target。
- `contracts/artifacts.py`：Job runtime 已增加 target 字段，artifact schema 为 3。
- workflow：lint-plan 用 `refs` 解析全部 container content ref，matrix 已包含不可变
  `image_ref`；普通 PR 用 `targets` 做通用执行切片。
- Image PR：Dockerfile-only 直接构建；混合 PR 串行执行 CPU、candidate、NPU，最终由
  `Ascend-Image-PR-Gate` 统一裁决。
- tests：已覆盖 target/image 解析优先级、A3/A5 路由和不静默回退。
- 文档：`CI_DESIGN.md` 记录整体模型，`CI_DEVELOPMENT.md` 记录维护命令和契约。

长期方案的最终约束是：**镜像版本是环境维度，硬件代际是 target 维度，runner 容量是 lane 维度，三者不能互相编码。**

