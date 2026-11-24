# CI Workflow 修改书

## 1. 基线

- 分支：`ci/v4-dev-baseline`
- 基线提交：`56e42e2`
- 范围：`.github/workflows/*.yml`
- 本文档不计入 Python V4 的 4500 行预算。

基线 workflow 规模（`56e42e2`）：

| Workflow | 总行数 | 内联 shell |
|---|---:|---:|
| `ascend-pr.yml` | 468 | 158 |
| `ascend-image-pr.yml` | 328 | 118 |
| `ascend-image-pipeline.yml` | 295 | 46 |
| `ascend-image-promote.yml` | 176 | 52 |
| `ascend-manual.yml` | 62 | 0 |
| `ascend-nightly.yml` | 22 | 0 |
| 合计 | 1351 | 374 |

当前工作区为 7 个 workflow、合计 1211 行：`ascend-pr.yml` 326、
`ascend-image-pr.yml` 263、`ascend-image-pipeline.yml` 251、
`ascend-image-promote.yml` 176、`ascend-manual-test.yml` 62、
`ascend-manual-image.yml` 64、`ascend-nightly.yml` 69。

问题不是 workflow 文件太多，而是 YAML 中重复承载了执行策略、结果校验和
Gate 判断。

## 2. 设计判断

当前整体分层是合理的：

```text
pull_request        -> ascend-pr.yml
pull_request_target -> ascend-image-pr.yml
push main           -> ascend-image-promote.yml
workflow_dispatch   -> ascend-manual-test.yml / ascend-manual-image.yml /
                       ascend-nightly.yml / ascend-image-promote.yml
schedule            -> ascend-nightly.yml
reusable image      -> ascend-image-pipeline.yml
```

需要保留的领域边界：

```text
execution graph -> ascend-pr.yml
image candidate -> ascend-image-pipeline.yml
image promotion -> ascend-image-promote.yml
```

Workflow 应该只负责：

- trigger、permission、concurrency
- checkout、Python setup
- matrix 展开
- artifact 上传和下载
- Job 依赖与最终状态接线

Python 应该负责：

- 变更分类
- 镜像选择
- Plan 生成
- Executor 调用
- 结果校验
- Gate 决策

当前 YAML 主要在两类地方越界：

1. CPU 和 container Job 重复校验 Executor 结果。
2. Image PR 和 promote 的 Gate 用 shell 实现策略。

### 2.1 当前文件划分判断

基线为 6 个文件，当前拆成 7 个文件；`ascend-manual-test.yml` 与
`ascend-manual-image.yml` 分别负责测试和镜像入口。文件数量不是主要问题，
职责边界才是重点。

| 文件 | 当前职责 | 判断 |
|---|---|---|
| `ascend-pr.yml` | 普通 PR 入口、可复用执行图 | 合理，直接 dispatch 已删除 |
| `ascend-image-pr.yml` | Image PR 编排：选镜像、CPU preflight、构建测试、Gate | 合理，保持独立 |
| `ascend-image-pipeline.yml` | 单镜像候选构建、候选测试、Gate | 合理，按镜像复用 |
| `ascend-image-promote.yml` | 合并后 promote | 合理，必须和 Image PR 分开 |
| `ascend-manual-test.yml` | 手动单 Job 测试入口 | 合理，保留 |
| `ascend-manual-image.yml` | 手动镜像构建/复用入口 | 合理，保留 |
| `ascend-nightly.yml` | nightly 入口 | 入口模式合理，但当前没有启用 schedule |

合理的地方：

- 普通执行图和镜像候选图分开，避免镜像域污染执行域。
- Image PR 和 Image Promote 分开，符合不同信任模型。
- `ascend-image-pr.yml` 只负责编排，单镜像工作复用
  `ascend-image-pipeline.yml`。
- `ascend-manual-test.yml` 和 `ascend-nightly.yml` 使用薄 wrapper 是合理的，不应
  合并进主执行图。

需要调整的地方：

1. `ascend-pr.yml` 只保留 `pull_request` 和 `workflow_call`；直接
   `workflow_dispatch` 已删除。
2. `ascend-nightly.yml` 当前没有启用 `schedule`。资源允许前保留 nightly 名称，
   但要明确它是手动全量入口、未来再启用定时触发。
3. `ascend-image-pipeline.yml` 只保留 `workflow_call`；手动构建或调试单个
   镜像统一走 `ascend-manual-image.yml`。
4. `ascend-image-pipeline.yml` 更准确的名字是单镜像候选流水线，例如
   `ascend-image-candidate.yml`。这是可选命名优化，不是本次必须改动。

保留的文件结构：

```text
入口：
  ascend-pr.yml
  ascend-image-pr.yml
  ascend-image-promote.yml
  ascend-manual-test.yml
  ascend-manual-image.yml
  ascend-nightly.yml

复用：
  ascend-pr.yml
  ascend-image-pipeline.yml
```

结论：当前 7 个文件，manual test/image 按域拆分；优先清理 `ascend-pr.yml`
的多入口角色，并处理没有 schedule 的 `ascend-nightly.yml`。

## 3. 目标结构

```text
入口 workflow：
  ascend-pr.yml             pull_request
  ascend-image-pr.yml       pull_request_target
  ascend-image-promote.yml  push main + workflow_dispatch
  ascend-manual-test.yml    workflow_dispatch
  ascend-manual-image.yml   workflow_dispatch
  ascend-nightly.yml        schedule（启用后）

可复用 workflow：
  ascend-pr.yml             workflow_call 执行图
  ascend-image-pipeline.yml workflow_call 单镜像候选图
```

`ascend-pr.yml` 可以同时保留 `pull_request` 和 `workflow_call`，但不应再和
`ascend-manual-test.yml` 提供测试入口，`ascend-manual-image.yml` 提供镜像入口。

当前工作区约 `1211` 行，目标控制在 `1250` 行以内。只有在不牺牲可读性和
Gate 覆盖面的前提下，才继续压缩到 `1100` 行。

## 4. 工作包

### W01：修正 fork 审批顺序

优先级：P0

文件：`ascend-image-pr.yml`

当前顺序：

```text
checkout PR head
-> install dependencies
-> lint 执行 PR 代码
-> fork approval
```

必须改为：

```text
fork approval
-> checkout PR head
-> setup Python
-> install dependencies
-> lint
-> classify and plan
```

审批步骤不能 checkout 或执行 PR 代码。

额外加固：

- 执行 PR 代码的 checkout 统一设置 `persist-credentials: false`。
- 增加 workflow 测试，断言审批步骤位于 checkout 之前。

预计行数变化：持平。

### W02：让 timing 不阻塞 Gate

优先级：P0

文件：`ascend-pr.yml`

状态：已实施。`timing` 使用 `continue-on-error: true`；Gate 只读取
`ci-artifacts/` 下的 Plan 和结果。

当前 `timing` 在 `judge` 之前执行。只要 timing 失败，就不会生成 Gate
decision。

目标行为：

- `timing` 失败不能阻塞 `judge`。
- 设置 `continue-on-error: true`，或者把 timing 移到最终判定之后。

预计行数变化：持平。

### W03：删除重复结果校验

优先级：P1

文件：`ascend-pr.yml`，CPU 和 container Job。

当前顺序：

```text
execute
-> verify result
-> upload result
-> enforce result
-> final Gate
```

`Executor` 已经在 failed、cancelled、infra_error 时返回 `1`。
`Gate` 已经检查 missing、unexpected、failed、skipped 结果。

目标顺序：

```text
execute
-> upload result if: always()
-> final Gate
```

需要修改：

- 删除 `executor.exit_code`。
- 删除 `verify result`。
- 删除 `enforce result`。
- execute 步骤直接返回 Executor 的退出码。
- 保留 `upload result` 的 `if: always()`。
- 增加测试，证明 Executor 失败时 `main()` 返回 `1`。

不要删除最终 Gate 的检查。

对应位置：

- `ascend-pr.yml:250-314`
- `ascend-pr.yml:345-409`

预计减少 YAML：`70-85` 行。

### W04：把 Image PR Gate 策略移到 Python

优先级：P1

文件：`ascend-image-pr.yml`

当前 Gate 约有 50 行 shell，判断：

- plan result
- change kind
- image count
- CPU job count
- CPU job result
- image build/test result
- `no_image`、`image_only`、`mixed` 的期望

把这些策略移到 `scripts/ci/execution/finalization/` 下的 Python 命令。

命令接收明确的工作流事实，而不是 YAML 表达式：

```text
change_kind
image_count
cpu_job_count
plan_result
cpu_tests_result
build_and_test_result
```

Workflow 只传递 `${{ needs.*.result }}` 和 Job outputs。命令负责写 summary
并返回最终退出码。

预计减少 YAML：`40-55` 行。

### W05：评估 promote Gate 收敛

优先级：P2

文件：`ascend-image-promote.yml`

Promote Gate 约有 18 行 shell，规模小于 Image PR Gate。

如果复用 W04 的实现后仍然足够小，就复用；不要为两个调用方引入通用 Gate
框架。

预计减少 YAML：`10-18` 行。

### W06：统一 manual 和直接 dispatch

优先级：P1

状态：已实施。`ascend-pr.yml` 只保留 `pull_request` 和 `workflow_call`；
手动测试和手动镜像入口已拆分为两个薄 wrapper。

文件：

- `ascend-pr.yml`
- `ascend-manual-test.yml`
- `ascend-manual-image.yml`
- `ascend-nightly.yml`

基线状态：

- `ascend-pr.yml` 同时有 `workflow_call` 和 `workflow_dispatch`。
- 原 `ascend-manual.yml`（现 `ascend-manual-test.yml`）调用
  `ascend-pr.yml`，输入几乎重复。
- `ascend-nightly.yml` 没有启用 `schedule`。

推荐模型：

- `ascend-pr.yml`：只保留 `pull_request` 和 `workflow_call`。
- `ascend-manual-test.yml`：手动单 Job 测试入口。
- `ascend-manual-image.yml`：手动镜像构建/复用入口。
- `ascend-nightly.yml`：启用 schedule 后作为唯一定时入口。
- `ascend-nightly.yml` 仅在 `main` 上于测试前 best-effort 同步 stale alias；
  修复失败只告警，不阻塞全量测试，并与 promote workflow 共用
  `image-promote` 串行锁。
- 删除 `ascend-pr.yml` 的 `workflow_dispatch` 输入块。
- 如果仍然需要手动 target 切片，在 manual-test wrapper 中补 `targets`。

不要在同一个 manual workflow 中混入测试和镜像两套输入。

净变化：新增 manual-image wrapper；主要收益是测试/镜像职责隔离，而不是压缩行数。

### W07：统一 artifact 和命名规范

优先级：P2

文件：所有 workflow。

统一 artifact：

- 普通 Job result：`ci-result-<job_id>`
- Image PR CPU result：`ci-result-cpu-<job_id>`
- 候选镜像 result：`ci-result-<image_id>-<job_id>`
- decision：`ci-decision-<entry>`

执行 PR 代码的 checkout 统一使用 `persist-credentials: false`。

所有 Job 都使用 bash 时，使用 workflow 级：

```yaml
defaults:
  run:
    shell: bash
```

统一 workflow 名称：

- `Ascend-PR-CI`
- `Ascend-Image-PR`
- `Ascend-Image-Candidate`
- `Ascend-Image-Promote`
- `Ascend-Manual-Test`
- `Ascend-Manual-Image`
- `Ascend-Nightly-CI`

预计行数变化：小幅减少，主要提升可读性。

### W08：双触发问题后续决策

优先级：P3

Dockerfile-only 和 mixed PR 当前同时触发：

```text
ascend-pr.yml
ascend-image-pr.yml
```

普通 PR 对 image-only 和 mixed 会产生 0 Job。

暂时不要给 `ascend-pr.yml` 增加：

```yaml
paths-ignore:
  - docker/**/Dockerfile*
```

原因是“删除 Dockerfile + 修改代码”的 PR：

- Image PR 使用 `--diff-filter=AM`，删除项不会进入镜像选择。
- `image_count=0`，Image PR 走 `no_image`。
- 如果普通 PR 被 paths-ignore 跳过，代码测试会漏跑。

该问题需要单独修改删除场景的分类语义，不纳入第一阶段。

## 5. 实施顺序

### Phase A：安全和正确性

- W01 修正 fork 审批顺序
- W02 解除 timing 对 Gate 的阻塞
- 增加审批顺序测试

不设行数目标。

### Phase B：Executor 结果收敛

- W03 删除重复结果校验
- 增加 Executor 失败退出码测试

目标减少：`70-85` 行。

### Phase C：Gate 收敛

- W04 Image PR Gate 移到 Python
- W05 promote Gate 在保持简单的前提下复用

目标减少：`50-70` 行。

### Phase D：入口收敛

- W06 删除 `ascend-pr.yml` 的直接 dispatch
- 保留 test/image 两个按域拆分的 manual 入口和一个 nightly 入口

目标减少：`25-35` 行。

### Phase E：清理

- W07 artifact、checkout 凭据、defaults、命名
- W08 暂不修改，单独决策

目标减少：`10-20` 行。

当前范围：`1211` 行。

## 6. 验收标准

必须同时满足：

1. fork 审批发生在任何 PR 代码 checkout 或执行之前。
2. timing 失败不会阻塞最终 Gate decision。
3. CPU 和 container Job 不再重复校验 Executor 结果。
4. Executor 失败退出码有测试覆盖。
5. 最终 Gate 仍然检查 missing、failed、skipped、unexpected。
6. Image PR 对 `no_image`、`image_only`、`mixed` 的行为不变。
7. manual 和 nightly 使用统一入口模型。
8. artifact 上传名和下载 pattern 一致。
9. `pytest scripts/ci/tests`、`scripts.ci.tools.lint`、`compileall`、
   `git diff --check` 全部通过。
10. workflow 总行数不超过 `1200`。

## 7. 风险

- 删除 `enforce result` 依赖 Executor 退出码，必须先补失败退出测试。
- Gate 移到 Python 会改变错误文案和 summary，需要同步更新 workflow 测试。
- 删除 `ascend-pr.yml` 的直接 dispatch 会改变手动 UI，必须保留 manual
  wrapper 所需输入。
- artifact 改名会静默破坏下载，必须在同一变更中更新上传和下载。
- 双触发问题本阶段不解决，保持现状。

## 8. 非目标

- 不合并 image domain 和 execution domain。
- 不引入通用 workflow engine。
- 不把策略逻辑搬到更多 shell 中。
- 不删除最终 Gate 检查。
- 不为假想复用引入 composite action。
- 不在本次修改 Dockerfile 删除语义。
