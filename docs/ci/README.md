# TorchTurbo CI

Current design and test scope:
[`scripts/ci/CI_DESIGN.md`](../../scripts/ci/CI_DESIGN.md).

Local development, commands, and contracts:
[`scripts/ci/CI_DEVELOPMENT.md`](../../scripts/ci/CI_DEVELOPMENT.md).

The execution path is:

```text
lint -> Planner -> PlanArtifact -> Executor -> JobResult -> Gate
```

- `scripts/ci/config/ci.yml` is the only CI configuration. It declares targets,
  images, lanes, workload mappings, and pull-request/nightly scopes.
- `scripts/ci/execution/producer/facts.py` derives pytest facts from `tests/unit_tests` and
  integration cases from the suite runner manifest.
- Pull requests run all core pytest plus the two lightweight CPU/NPU integration
  cases; nightly runs all pytest suites and all integration cases.
- `.github/workflows/ascend-pr.yml` is the main PR and reusable workflow entry.
- `.github/workflows/ascend-manual.yml` reduces the normal Plan to one exact job.
- `.github/workflows/ascend-nightly.yml` reuses the main workflow with the
  nightly scope.
- `.github/workflows/ascend-image.yml` is the reusable image identity/build/resolve engine.
- `.github/workflows/ascend-image-pr.yml` maps changed Dockerfiles to logical images and
  runs one independent `build -> smoke -> tests` pipeline per affected image.
- `.github/workflows/ascend-image-promote.yml` reuses the validated candidate per affected
  image and promotes its alias after a second smoke test.
- `.github/workflows/ascend-image-pipeline.yml` is the reusable single-image validation or
  promotion unit.
