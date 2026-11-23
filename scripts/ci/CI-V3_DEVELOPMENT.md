# TorchTurbo CI V3 开发大纲

> 设计基线：`scripts/ci/CI-V3.md`  
> 开发基线：`ci/v2-dev-baseline` / `8f107fd`  
> 状态：已实施  
> 日期：2026-11-20

## 0. 最终决定

```text
TestArea     = 测试选择 + 硬件要求 + venv + timeout
Environment  = target + image_id + runner
Job          = TestArea × Environment × Venv
```

固定规则：

- 只有一个 CI YAML。
- Environment 运行时派生，不在 YAML 中物化。
- 不增加 `environments`、`lanes`、`image_pr`。
- Scope 选择 TestArea 和 image。
- Image PR 复用 `pull_request`，候选镜像由 binding 注入。
- 镜像域和执行域只交换 `ImageBinding` 和 `ImageBuildResult`。

固定顺序：

```text
契约 -> 执行域配置 -> Planner -> 镜像域 -> Workflow 接线 -> 清理
```

每个 Step 完成后停止 review，不同时修改两个域。

## 1. 域边界

| 域 | 负责 | 禁止出现 |
|---|---|---|
| 执行域 | TestArea、Scope、runner、venv、Planner、Executor | Dockerfile、registry、content tag、build |
| 收尾层 | Gate、JobResult 覆盖率、Plan 一致性、timing summary | Dockerfile、registry、构建 |
| 镜像域 | image 定义、Dockerfile 映射、select、resolve、build、promote | TestArea、Scope、venv、测试选择 |

执行链路：

```text
Scope
  -> TestArea[]
  -> unique image_id[]
  -> ImageBinding[]
  -> Environment
  -> Job / ExecutionPlan
  -> Executor / Gate
```

Image PR：

```text
changed Dockerfiles
  -> image select
  -> ImageBuildResult[]
  -> candidate ImageBinding
  -> pull_request 中同 target 的 TestArea[]
  -> Job / ExecutionPlan
```

### 1.1 代码分层

```text
contracts/base.py
  Contract / ContractError / Diagnostic / GitSource

contracts/planner_contract.py
  PlannerOutput / ExecutionPlan / Job / TestTask / Runtime
  VenvSpec / PytestSpec / IntegrationSpec
  plan_digest

contracts/executor_contract.py
  JobResult / TaskResult / TaskEvidence / Failure
  load_job_result / validation

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

规则：

- 契约层不构造 ExecutionPlan。
- ExecutionPlan 和 PlannerOutput 只由 producer 构造。
- Executor 和 Gate 消费 PlannerOutput、JobResult。
- 执行域只使用 `ImageBinding`，不再维护 `ImageRef`。

## 2. Step 1：跨域契约

文件：

```text
scripts/ci/contracts/image_contract.py
scripts/ci/tests/test_image_contract.py
```

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

验收：

- 稳定 JSON。
- 非法 kind。
- container ref 未固定 digest。
- contract 不 import 执行域或镜像域。

## 3. Step 2：配置模型

文件：

```text
scripts/ci/config/ci.yml
scripts/ci/execution/producer/config.py
scripts/ci/tests/test_config.py
```

配置结构：

```text
resources.targets
resources.images
resources.runners
resources.venvs
test_areas
scopes
```

删除：

```text
顶层 environments
resources.lanes
image.default
scope.targets
scope.images
scope.pytest
scope.integration
per-test image override
per-case image override
image_pr Scope
```

Scope 模型：

```yaml
scopes:
  pull_request:
    - test_areas:
        - pytest/core
        - integration/base_suite/qwen3_5_moe/sft_2npu
      environment:
        image: npu-a3-cann90
```

语义：

```text
environment.image -> image.target
省略 test_areas   -> target 下所有兼容 TestArea
显式 test_areas   -> 必须非空且至少产生一个 Job
```

校验：

- image 必须存在。
- 同一 Scope 不允许重复选择同一个 image。
- Scope 不出现 runner、卡数、Dockerfile、recipe、venv。
- Scope 引用未知 TestArea 直接失败。

## 4. Step 3：Planner

文件：

```text
scripts/ci/execution/producer/planner.py
scripts/ci/execution/producer/matrix.py
scripts/ci/tests/test_planner.py
```

普通 PR：

```text
Scope selection
  -> image_id
  -> image target
  -> 兼容 TestArea
  -> venv
  -> ranks
  -> 最小满足 ranks 的 runner
  -> Environment
  -> Job
```

Runner 规则：

```text
rank_capacity >= test.ranks
选择最小 rank_capacity
没有满足项 -> 失败
target + rank_capacity 重复 -> 配置失败
```

ImageBinding：

- Planner 按 `image_id` 查找 binding。
- 缺失 binding 直接失败。
- `binding.target` 必须与 image 定义和 Scope target 一致。
- 最终 Job 的 `image.ref` 必须是 digest。

Image PR candidate：

```text
binding = ImageBuildResult.binding
areas = pull_request 中 binding.target 对应的 TestArea 并集
plan = Planner(areas, candidate_binding=binding)
```

规则：

- 不新增 `image_pr` Scope。
- 不复制测试清单。
- 同一 target 的多个 PR 选择取 TestArea 并集并去重。
- candidate target 在 `pull_request` 中没有测试时失败。

验收：

- `pull_request` 7 Job。
- `nightly` 12 Job。
- 普通 PR 只解析实际使用的 image。
- 未知 image、缺失 binding、target 不匹配、无 runner 都明确失败。

## 5. Step 4：镜像域

文件：

```text
scripts/ci/image/recipe.py
scripts/ci/image/selection.py
scripts/ci/image/identity.py
scripts/ci/image/release.py
scripts/ci/image/cli.py
```

镜像定义：

```yaml
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
    ref: swr.../torchturbo_npu:cann9.0.0-a3-base
```

规则：

- 基础镜像只来自 Dockerfile 的 `FROM`。
- `publish.ref` 是候选镜像发布位置，不是基础镜像。
- context 从 Dockerfile 父目录派生。
- context 从 Dockerfile 父目录派生，但 hash 只包含该 Dockerfile。
- 一个 image 对应一个 Dockerfile。
- 多个 image 共用 Dockerfile 失败。
- 未映射 Dockerfile 失败。

构建身份：

```text
build_input_hash = sha256(Dockerfile + platform + revision)
```

- `candidate_tag` 只存在镜像域内部。
- `ci_build_args` 传给构建，但不进入 hash。
- platform/revision 变化使用手动镜像构建，不改变自动触发规则。

自动触发：

```text
新增或修改 docker/**/Dockerfile*
```

## 6. Step 5：Workflow 接线

普通 PR：

```text
classify changes
  -> Planner 产出 image requests
  -> image resolve at HEAD
  -> Planner 接收 ImageBinding[]
  -> publish PlannerOutput
```

必须删除 base revision 镜像解析。缺失 binding 直接失败。

Image PR：

```text
collect changed Dockerfiles
  -> image plan / build
  -> candidate tests 使用 pull_request Scope
  -> same Planner / Executor / Gate
```

Mixed PR：

```text
CPU preflight 使用 pull_request 中 target=cpu 的 TestArea
随后构建候选镜像并执行其 target 对应的 PR TestArea
```

PR 变更分类固定为：

```text
code
docs_only
image_only
mixed
```

## 7. Step 6：清理和总验收

删除：

```text
旧 environments 配置模型
旧 lanes 配置模型
recipe.ref
重复的 context 配置
lane_for()
base revision 镜像解析
per-test/per-case image override
image_pr 相关分支
与实现不一致的旧文档
```

命令：

```bash
python3 -m pytest -q scripts/ci/tests
python3 -m scripts.ci.tools.lint
```

关键场景：

| 场景 | 期望 |
|---|---|
| 只改文档 | 0 Job，成功 |
| 只改代码 | 代码测试 |
| 新增/修改 Dockerfile | 构建对应 image 并执行候选测试 |
| 多个 Dockerfile | 多个独立候选流水线 |
| 删除 Dockerfile | 不构建 |
| 代码 + Dockerfile | 普通 PR mixed；Image PR CPU preflight + 候选测试 |
| 未映射 Dockerfile | 配置失败 |
| 候选 target 无 PR 测试 | 配置失败 |
| 缺少 ImageBinding | 配置失败 |

完成标准：

- 执行域不知道 Dockerfile、content tag、registry、build。
- 镜像域不知道 TestArea、Scope、venv。
- Image PR 不维护第二套测试清单。
- 新增 A5/GPU 只增加 target、image、runner 和 Scope。
- 新增 CANN/CUDA 版本只增加 image，并按需要更新 Scope。
