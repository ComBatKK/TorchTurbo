# TorchTurbo CI V3 设计

> 状态：已实施  
> 日期：2026-11-20  
> 范围：测试选择、硬件资源、镜像资源、venv、Scope、镜像构建和 PR 变更分类

## 1. 设计目标

CI V3 只使用三个执行概念：

```text
TestArea     = 测试选择 + 硬件要求 + venv 解析规则 + timeout
Environment  = target + image_id + runner
Job          = TestArea × Environment × Venv
```

Scope 不是新的执行模型，只负责为一次运行选择 TestArea 和镜像。

约束：

- 只有一个 CI 配置文件：`scripts/ci/config/ci.yml`。
- Environment 由 Planner 运行时组合，不在 YAML 中物化。
- 不配置 target × 卡数 × image 的笛卡尔组合。
- 不维护 image → test_areas 的持久映射。
- TestArea 不绑定镜像版本。
- 镜像域是黑盒；执行域不知道 Dockerfile、registry、content tag 或构建过程。
- 镜像域不知道 TestArea、Scope、venv 或测试选择。
- 普通 PR 和 Image PR 共用同一份 `pull_request` 测试策略。
- Job 的机器 ID 和展示名称分离，协议字段不依赖名称反解析。

明确不做：

- 不引入通用 CI DSL。
- 不实现版本约束求解器。
- 不增加 `environments`、`lanes` 或 `image_pr` Scope。
- 不为单个测试维护 image override。
- 不在 Image PR 中复制一套测试清单。

## 2. 总体结构

```text
resources
  targets
  images
  runners
  venvs

test_areas
  pytest
  integration

scopes
  pull_request
  nightly
```

运行关系：

```text
Scope
  -> TestArea 选择
  -> image 选择
  -> image.target
  -> runner 选择
  -> Environment
  -> Job
```

### 2.1 代码分层

```text
contracts/base.py
  Contract / ContractError / Diagnostic / GitSource

contracts/planner_contract.py
  PlannerOutput / ExecutionPlan / Job / TestTask / Runtime
  VenvSpec / PytestSpec / IntegrationSpec / validation

contracts/executor_contract.py
  JobResult / TaskResult / TaskEvidence / Failure / validation

contracts/image_contract.py
  ImageBinding / ImageBuildResult

execution/producer/config.py
  CiConfig / load

execution/producer/facts.py
  pytest marker/rank facts / integration manifest facts / load

execution/producer/planner.py
  build_execution_plan / build_planner_output
  ImageSelection / pytest and integration Job compilation
  build / planner_output_for

execution/producer/matrix.py
  matrix_outputs

execution/producer/cli.py
  command line entry point

tools/hashing.py
  content_digest / file_sha256
```

结果层级：

```text
TestTask   -> TaskResult -> TaskEvidence / Failure
Job        -> JobResult  -> TaskResult[] / Failure
```

`TaskResult` 只描述对应 `TestTask` 的执行事实。`JobResult.failure`
只保存不属于任何 task 的 Job 级失败，例如 venv、环境 probe 或 Executor 中断。

约束：

- `contracts` 只定义和校验数据，不构造 ExecutionPlan。
- `Runtime.image` 直接使用 `ImageBinding`，不存在执行域镜像模型。
- `contracts` 不 import `execution` 或 `image`。
- `ExecutionPlan.plan_id` 是内容摘要，不手工赋值。

```python
@dataclass(frozen=True)
class ExecutionPlan(Contract):
    plan_id: str  # Content-addressed ID of this execution plan.
    ci_type: str  # CI run type: pull_request, nightly, or manual.
    change_mode: str  # Change mode: code, docs_only, image_only, or mixed.
    git_source: GitSource  # Git repository, ref, and commit.
    jobs: tuple[Job, ...]  # Jobs scheduled by this plan.
```

`ExecutionPlan` 只保存执行所需事实，不保存 `inputs` 或自报的 `coverage`。
Gate 通过 `jobs` 和 `JobResult` 校验实际覆盖。

## 3. 配置文件结构

```yaml
schema_version: 5

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

    npu-a3-cann90:
      target: npu-a3
      kind: container
      build:
        dockerfile: docker/npu/Dockerfile_aarch64
        platform: linux/arm64
        revision: 1
        ci_build_args:
          GIT_HTTP_USER_AGENT: git/2.56.1
      publish:
        ref: swr.cn-north-12.myhuaweicloud.com/ascend/torchturbo_npu:cann9.0.0-a3-base

    npu-a3-cann92:
      target: npu-a3
      kind: container
      build:
        dockerfile: docker/npu/Dockerfile_cann92
        platform: linux/arm64
        revision: 1
      publish:
        ref: swr.cn-north-12.myhuaweicloud.com/ascend/torchturbo_npu:cann9.2.0-a3-base

  runners:
    - target: cpu
      rank_capacity: 8
      runs_on:
        variable: CI_CPU_RUNNER
        fallback: ubuntu-22.04-arm

    - target: npu-a3
      rank_capacity: 2
      runs_on:
        variable: CI_NPU_2_RUNNER
        fallback: linux-aarch64-a3-800i-2
      container_options: --user root

    - target: npu-a3
      rank_capacity: 16
      runs_on:
        variable: CI_NPU_16_RUNNER
        fallback: linux-aarch64-a3-800i-16
      container_options: --user root

  venvs:
    default: {}

test_areas:
  pytest:
    defaults:
      job_timeout_minutes: {cpu: 40, npu: 90, gpu: 90}
      step_timeout_minutes: {cpu: 40, npu: 90, gpu: 90}
    model_venvs: {}
    framework_venvs: {}
    combo_venvs: {}
    overrides: {}

  integration:
    suites: {}

scopes:
  pull_request:
    - test_areas:
        - pytest/core
        - pytest/adapter
        - integration/base_suite/qwen3_5_moe/sft_2cpu
      environment:
        image: cpu-default

    - test_areas:
        - pytest/core
        - integration/base_suite/qwen3_5_moe/sft_2npu
      environment:
        image: npu-a3-cann90

  nightly:
    - test_areas:
        - pytest/core
        - pytest/model
        - integration/base_suite/qwen3_5_moe/sft_2npu
      environment:
        image: npu-a3-cann90

    - test_areas:
        - integration/base_suite/bailing_v3/sft_16npu
      environment:
        image: npu-a3-cann90
```

`test_areas` 内部继续沿用现有 pytest 和 integration venv、timeout 规则，本方案不改变其职责。

## 4. Scope

### 4.1 职责

Scope 是一次运行的显式选择：

```text
运行哪些 TestArea
使用哪个 image
```

`environment` 是内联选择器，不是全局资源。它的语义是：

```text
本次运行使用 image=... 的环境
```

而不是：

```text
image 拥有这些测试
```

### 4.2 结构

```yaml
scopes:
  pull_request:
    - test_areas:
        - pytest/core
        - integration/base_suite/qwen3_5_moe/sft_2npu
      environment:
        image: npu-a3-cann90
```

规则：

- `environment.image` 必须引用已定义的 image。
- Scope 不写 target、runner、卡数、Dockerfile、recipe 或 venv。
- 同一 Scope 中不允许重复选择同一个 image；需要更多 TestArea 时合并列表。
- 省略 `test_areas`：运行该 image 的 target 下所有兼容 TestArea。
- 显式 `test_areas` 必须非空。
- 显式列出的 TestArea 必须至少产生一个 Job，否则配置失败。
- 一个 TestArea 可以出现在多个 Scope 选择中，但不存在持久映射。

### 4.3 多版本

同一 target 可以有多个 image：

```yaml
scopes:
  nightly:
    - test_areas: [pytest/core, pytest/model]
      environment:
        image: npu-a3-cann90
    - test_areas: [pytest/core, pytest/model]
      environment:
        image: npu-a3-cann92
```

新增 image 不会自动扩大任何 Scope。必须显式写入 `pull_request` 或 `nightly`。

## 5. TestArea

### 5.1 职责

TestArea 回答：

```text
跑哪些测试
需要什么设备
需要多少个 ranks
使用哪个 venv
超时多少
```

TestArea 不回答：

```text
使用 A3 还是 A5
使用 CANN 9.0 还是 9.2
使用哪个 runner
```

### 5.2 标识

```text
pytest/<suite>
integration/<suite>/<case>
```

示例：

```text
pytest/core
pytest/adapter
pytest/model
integration/base_suite/qwen3_5_moe/sft_2npu
```

### 5.3 Facts

pytest facts 继续由测试代码派生：

```text
suite
devices
targets
ranks_by_device
frameworks
model_families
process_group
```

integration facts 继续由 suite manifest 派生：

```text
suite
case_id
platform
targets
ranks
framework
config / golden
```

test code 中不出现 image、CANN/CUDA 版本或 runner。

### 5.4 Venv

venv 属于 TestArea，不属于 Environment：

```text
image    提供 OS、驱动、CANN/CUDA、Python 基础运行时
venv     提供项目 Python 依赖
```

一个 TestArea 可以解析出多个 venv，每个 venv 生成一个 Job。

## 6. Environment 和 Runner

### 6.1 Runtime Environment

Environment 由 Planner 运行时生成：

```text
Environment = target + image_id + runner
```

示例：

```text
npu-a3 + npu-a3-cann90 + linux-aarch64-a3-800i-2
```

不维护 `lanes` 配置。

### 6.2 Runner 选择

Runner 规则：

```text
同 target 下选择 rank_capacity >= test.ranks 的最小 runner
没有满足项：配置错误
target + rank_capacity 重复：配置错误
```

当前 pytest 按 `(suite, device, venv)` 分组，组内 ranks 取最大值。未来需要把不同 ranks 拆成不同 Job 时，再把 ranks 加入分组键。

如果确实存在特殊的 runner 限制，后续可以增加 Scope 级可选 runner override；不恢复全局 lane 笛卡尔配置。

### 6.3 兼容规则

```text
target.device 在 TestArea devices 中
TestArea 未声明 targets，或包含 environment.target
TestArea ranks 不超过 runner rank_capacity
```

## 7. 普通 PR

普通 PR 的主链路：

```text
Scope
  -> TestArea 选择
  -> image_id 集合
  -> image resolve at HEAD
  -> ImageBinding 集合
  -> Planner
  -> PlannerOutput
```

规则：

- 只解析 Scope 实际使用的 image。
- 使用 HEAD 中的 image 配置，不使用 base revision。
- 缺失 binding、未知 image、target 不匹配都直接失败。
- binding 必须来自镜像域，Planner 不自行推断镜像。
- 最终 ExecutionPlan 中的 container image 必须是 digest-pinned ref。

## 8. Image PR

### 8.1 触发

自动镜像构建的唯一标准：

```text
新增或修改 docker/**/Dockerfile*
```

删除 Dockerfile 不触发构建。

### 8.2 构建和候选测试

```text
changed Dockerfiles
  -> image select
  -> image build / reuse
  -> ImageBuildResult[]
  -> candidate ImageBinding
  -> pull_request Scope 中同 target 的 TestArea
  -> Planner
  -> Executor / Gate
```

不新增 `image_pr` Scope，不维护第二份测试清单。候选测试直接复用 `pull_request`：

```text
test_areas = union(
  pull_request 中选择的 TestArea
  where selected_image.target == binding.target
)
image = binding
```

规则：

- 同一 target 的所有 `pull_request` TestArea 取并集并去重。
- 如果 Scope 省略 test_areas，则包含该 target 下所有兼容 TestArea。
- candidate target 在 `pull_request` 中没有测试时直接失败。
- 最终 Job 使用候选镜像的不可变 digest。
- 不同镜像仍然独立构建、独立测试、独立报告。

### 8.3 Mixed PR

代码 + Dockerfile：

```text
Image PR
  -> CPU preflight 使用 pull_request 中 target=cpu 的 TestArea
  -> 构建受影响镜像
  -> 每个候选镜像复用 pull_request 中同 target 的 TestArea
```

普通 PR 对 image_only / mixed 变更产生 0 Job。

## 9. 镜像域

### 9.1 镜像定义

```text
kind: host
  ref 直接作为运行时引用

kind: container
  target  镜像所属硬件 target
  build   构建输入
  publish 候选镜像发布位置
```

基础镜像只由 Dockerfile 的 `FROM` 决定。`publish.ref` 是候选镜像发布仓库别名，不是基础镜像引用。

### 9.2 Dockerfile 规则

```text
一个 container image = 一个 Dockerfile
新增/修改 1 个 Dockerfile  -> 构建 1 个 image
新增/修改 N 个 Dockerfile  -> 并行构建 N 个 image
删除 Dockerfile             -> 不构建
未映射 Dockerfile           -> 配置失败
多个 image 共用 Dockerfile  -> 配置失败
```

构建 context 由 Dockerfile 父目录派生；`build_input_hash` 只包含该 Dockerfile，不扫描 context 中的其他文件。

### 9.3 构建身份

```text
build_input_hash = sha256(Dockerfile + platform + revision)
```

规则：

- `candidate_tag` 是镜像域内部查找键，不进入跨域契约。
- `ci_build_args` 传给构建，但不进入 `build_input_hash`。
- `GIT_HTTP_USER_AGENT` 保留在 `ci_build_args`，用于 GitCode clone workaround。
- `MAX_JOBS` 是构建控制参数，不进入 hash。
- platform 或 revision 改变不会自动触发 PR 构建，使用手动镜像构建流程。

### 9.4 构建结果

```json
{
  "schema_version": 1,
  "binding": {
    "image_id": "npu-a3-cann90",
    "target": "npu-a3",
    "kind": "container",
    "ref": "registry/repo@sha256:..."
  },
  "source_sha": "...",
  "build_input_hash": "sha256:...",
  "platform": "linux/arm64",
  "built": true
}
```

## 10. 跨域契约

```python
@dataclass(frozen=True)
class ImageBinding:
    image_id: str  # 逻辑镜像 ID，例如 npu-a3-cann90
    target: str    # 镜像所属硬件 target
    kind: str      # host | container
    ref: str       # container 必须是 repository@sha256:<64 hex>


@dataclass(frozen=True)
class ImageBuildResult:
    binding: ImageBinding  # 构建或复用后的不可变绑定
    source_sha: str        # 构建代码对应的 Git commit
    build_input_hash: str  # Dockerfile + platform + revision 的完整 SHA-256
    platform: str          # 构建平台，例如 linux/arm64
    built: bool            # 是否实际执行了构建
```

约束：

- `ImageBinding` 是执行域唯一可消费的镜像结果。
- `ImageBuildResult` 只用于构建链路，不直接进入 Runtime。
- `ImageBinding.digest` 从 `ref` 派生，不单独保存。
- 执行域不得 import `scripts.ci.image`。
- 镜像域不得 import `scripts.ci.execution`。

## 11. PR 变更分类

| 变更 | 普通 PR | Image PR |
|---|---|---|
| 只改代码 | 执行代码测试 | 不运行 |
| 只改文档 | 0 Job，成功 | 不运行 |
| 只新增/修改 Dockerfile | image_only，0 Job | 构建并执行候选测试 |
| 新增/修改多个 Dockerfile | image_only，0 Job | N 个镜像独立构建并测试 |
| Dockerfile 改名 | image_only，0 Job | 新增路径对应的镜像构建并测试 |
| 代码 + Dockerfile | mixed，0 Job | CPU preflight + 构建并执行候选测试 |
| 删除 Dockerfile | 正常运行代码测试 | 0 镜像，成功 |
| 未映射 Dockerfile | 不处理 | 配置失败 |

两个 Workflow 都先执行 `git diff --diff-filter=AM`，因此删除项不会进入镜像选择；只有新增或修改 Dockerfile 才能触发构建。

分类结果固定为：

```text
code
docs_only
image_only
mixed
```

## 12. Job 命名

Job 使用结构化字段：

```text
kind
area
target
image_id
runner
venv
```

禁止从展示名称反解析字段。

机器 ID 由结构化字段生成，必须稳定且唯一。展示名称示例：

```text
NPU-A3 (CANN9.0 2NPU): core
NPU-A3 (CANN9.0 2NPU): adapter (LF)
NPU-A3 (CANN9.0 2NPU): adapter (Swift)
NPU-A5 (CANN9.2 2NPU): model (Qwen4-exp)
```

规则：

- 全部使用英文。
- 不使用 `/` 作为展示分隔符。
- 默认 venv 不显示。
- 非默认 venv 放在末尾圆括号中。

## 13. 演进规则

| 变化 | 需要修改 |
|---|---|
| 新增 4/8/16 卡 runner | 只增加 `resources.runners` 条目 |
| 新增 CANN/CUDA 版本 | 增加 image；需要进入 PR/nightly 时修改 Scope |
| 新增 A5/GPU | 增加 target、image、runner，并选择进入 Scope |
| 新增普通测试 | 通常只改测试代码，不改 `ci.yml` |
| 同一 target 多镜像版本 | Scope 显式选择不同 image；Image PR 自动复用同 target 的 PR 测试 |

新增 Image PR 镜像时，不需要新增测试映射。只要对应 target 已存在于 `pull_request`，候选镜像自动执行该 target 的 PR 测试。

## 14. 迁移和验收

迁移时删除：

```text
顶层 environments
resources.lanes
recipe.ref
recipe.context 的重复配置
Planner 的 lane_for()
普通 PR 的 base revision 镜像解析
image.default
per-test image override
per-case image override
image_pr Scope
```

验收：

- 只有一个 CI YAML。
- YAML 中没有 `environments`、`lanes`、`image_pr`。
- Scope 不包含 runner、卡数、Dockerfile、venv 或 recipe。
- 普通 PR 只解析实际使用的 image，并使用 HEAD 配置。
- Image PR 复用 `pull_request`，不复制测试清单。
- 新增/修改 N 个 Dockerfile 构建 N 个 image。
- 删除 Dockerfile 不构建。
- 未映射 Dockerfile 配置失败。
- context 只包含 Dockerfile。
- `build_input_hash = sha256(Dockerfile + platform + revision)`。
- `ci_build_args` 不进入 hash。
- 缺失 ImageBinding、target 不匹配、无 runner 均明确失败。
- 同一 target 多个 image 时 Job ID 唯一。
- A5/GPU 只通过 target、image、runner 和 Scope 扩展。
- Candidate 复用同一个 Planner、Executor、Gate。
- `pull_request` 保持 7 Job，`nightly` 保持 12 Job。
- `python3 -m pytest -q scripts/ci/tests` 通过。
- `python3 -m scripts.ci.tools.lint` 通过。
