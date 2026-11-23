from __future__ import annotations

from pathlib import Path

import yaml


def _workflow(root: Path, name: str) -> dict:
    return yaml.safe_load((root / ".github/workflows" / name).read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    value = document.get(True, document.get("on"))
    assert isinstance(value, dict)
    return value


def _step(job: dict, step_id: str) -> dict:
    return next(step for step in job["steps"] if step.get("id") == step_id)


def test_image_workflows_use_only_the_black_box_contract(repo_root: Path) -> None:
    for name in (
        "ascend-pr.yml",
        "ascend-image-pr.yml",
        "ascend-image-pipeline.yml",
        "ascend-image-promote.yml",
    ):
        dumped = yaml.safe_dump(_workflow(repo_root, name))
        assert "content_tag" not in dumped
        assert "image_plan" not in dumped
        assert "scripts/ci/image/images.yml" not in dumped
        for command in (
            "compute-image-refs",
            "resolve-image-digest",
            "build-image",
            "promote-alias",
            "check-image-env",
        ):
            assert command not in dumped


def test_normal_pr_workflow_resolves_runtime_images_at_head(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-pr.yml")
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)
    triggers = _triggers(document)

    assert set(jobs) == {"plan", "cpu", "container", "gate"}
    assert jobs["gate"]["needs"] == ["plan", "cpu", "container"]
    assert "paths-ignore" not in triggers["pull_request"]
    assert triggers["workflow_call"]["inputs"]["image_id"]["default"] == ""
    assert triggers["workflow_call"]["inputs"]["targets"]["default"] == ""
    assert "scripts.ci.image.cli build " not in dumped
    assert "scripts.ci.image.cli promote " not in dumped
    assert "SWR_USERNAME" not in dumped

    plan_steps = jobs["plan"]["steps"]
    step_names = [step.get("name", "") for step in plan_steps]
    assert step_names.index("classify changes") > next(
        index for index, step in enumerate(plan_steps)
        if str(step.get("uses", "")).startswith("actions/setup-python")
    )
    assert step_names.index("install CI dependencies") > step_names.index(
        "classify changes"
    )
    assert step_names.index("lint") > step_names.index("install CI dependencies")
    assert step_names.index("resolve runtime images") > step_names.index("lint")
    assert step_names.index("plan") > step_names.index("resolve runtime images")

    classify = _step(jobs["plan"], "changes")
    assert "scripts.ci.tools.change_kind" in classify["run"]
    assert "--diff-filter=AM" in classify["run"]
    assert "change_mode=$kind" in classify["run"]
    assert "delegated" not in classify["run"]

    identity = _step(jobs["plan"], "runtime_image")
    assert "steps.lint.outcome == 'success'" in identity["if"]
    assert "scripts.ci.image.cli resolve" in identity["run"]
    assert "scripts.ci.execution.producer.cli images" in identity["run"]
    assert "--revision" not in identity["run"]
    assert "github.event.pull_request.base.sha" not in yaml.safe_dump(identity)

    publish = _step(jobs["plan"], "publish")
    assert "--image-ref" in publish["run"]
    assert "ci-image-refs" in publish["env"]["IMAGE_REFS_FILE"]
    assert 'COMMON+=(--image "$CI_IMAGE_ID")' in publish["run"]
    assert "--fail-code" not in publish["run"]
    assert 'echo "::error::lint failed"' in publish["run"]
    assert 'echo "::error::cannot resolve runtime image digest"' in publish["run"]


def test_image_pr_separates_change_kind_image_selection_and_cpu_preflight(
    repo_root: Path,
) -> None:
    document = _workflow(repo_root, "ascend-image-pr.yml")
    triggers = _triggers(document)
    jobs = document["jobs"]

    assert triggers["pull_request_target"]["paths"] == ["docker/**/Dockerfile*"]
    assert set(jobs) == {"plan", "cpu-tests", "build_and_test_images", "gate"}
    assert jobs["plan"]["outputs"]["image_jobs"].startswith("${{ steps.images")
    assert jobs["plan"]["outputs"]["cpu_jobs"].startswith("${{ steps.cpu")

    classify = _step(jobs["plan"], "changes")
    select = _step(jobs["plan"], "images")
    cpu = _step(jobs["plan"], "cpu")
    assert "--diff-filter=AM" in classify["run"]
    assert "scripts.ci.tools.change_kind" in classify["run"]
    assert "scripts.ci.image.cli plan" in select["run"]
    assert "effective_kind=no_image" in select["run"]
    assert "case \"$KIND\" in" in select["run"]
    assert "image_only|mixed" in select["run"]
    assert 'if [ "$CHANGE_KIND" = mixed ]' in cpu["run"]
    assert "--targets cpu" in cpu["run"]
    assert "printf 'cpu_jobs=[]\\ncpu_job_count=0\\n'" in cpu["run"]

    assert jobs["cpu-tests"]["needs"] == "plan"
    assert jobs["cpu-tests"]["if"].find("change_kind == 'mixed'") >= 0
    assert jobs["build_and_test_images"]["uses"] == (
        "./.github/workflows/ascend-image-pipeline.yml"
    )
    assert jobs["build_and_test_images"]["if"].find(
        "needs.cpu-tests.result == 'skipped'"
    ) >= 0
    assert jobs["build_and_test_images"]["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.image_jobs) }}"
    )
    assert jobs["build_and_test_images"]["with"]["image_id"] == (
        "${{ matrix.image_id }}"
    )
    assert jobs["gate"]["name"] == "Image gate"
    assert "promote" not in jobs


def test_image_pipeline_builds_one_candidate_and_reuses_pr_scope(
    repo_root: Path,
) -> None:
    document = _workflow(repo_root, "ascend-image-pipeline.yml")
    triggers = _triggers(document)
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert set(jobs) == {"build", "container-tests", "gate"}
    assert triggers["workflow_call"]["inputs"]["image_id"]["required"] is True
    assert triggers["workflow_call"]["inputs"]["run_tests"]["default"] is True
    assert triggers["workflow_dispatch"]["inputs"]["run_tests"]["default"] is False
    assert triggers["workflow_dispatch"]["inputs"]["image_id"]["default"] == (
        "npu-a3-cann90"
    )
    assert triggers["workflow_call"]["inputs"]["checkout_ref"]["default"] == ""
    assert document["jobs"]["build"]["steps"][0]["with"]["ref"].startswith(
        "${{ inputs.checkout_ref"
    )
    assert "scripts.ci.image.cli build" in dumped
    assert "scripts.ci.image.cli check-envs" in dumped
    assert "scripts.ci.image.cli resolve" not in dumped
    assert "scripts.ci.image.cli promote " not in dumped
    assert "scripts.ci.tools.lint" not in dumped
    assert "docker/login-action@v3" in dumped
    assert "docker/setup-buildx-action@v3" in dumped

    build = jobs["build"]
    assert set(build["outputs"]) == {
        "ref", "container_jobs", "container_job_count"
    }
    build_step = next(
        step for step in build["steps"] if step.get("id") == "result"
    )
    assert '--image "$IMAGE_ID"' in build_step["run"]
    plan = next(
        step for step in build["steps"]
        if step.get("name") == "plan candidate tests"
    )
    assert '--image "$IMAGE_ID"' in plan["run"]
    assert '--image-ref "$IMAGE_ID=$IMAGE_REF"' in plan["run"]
    assert "--targets \"$TARGETS\"" in plan["run"]
    assert "${{ steps.result.outputs.ref }}" in yaml.safe_dump(build)
    assert "${{ needs.build.outputs.ref }}" in yaml.safe_dump(
        jobs["container-tests"]["container"]
    )
    assert jobs["container-tests"]["strategy"]["max-parallel"] == (
        "${{ fromJSON(vars.CI_CONTAINER_MAX_PARALLEL || '2') }}"
    )
    assert jobs["gate"]["needs"] == "container-tests"
    assert "ci-plan-${{ inputs.image_id }}" in dumped
    assert "ci-result-${{ inputs.image_id }}" in dumped
    assert "npu-default" not in dumped


def test_image_promote_selects_affected_images_and_fans_out(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-promote.yml")
    triggers = _triggers(document)
    jobs = document["jobs"]
    discovered = jobs["discover"]
    select = discovered["steps"][-1]
    promotions = jobs["promotions"]
    gate = jobs["gate"]

    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["paths"] == ["docker/**/Dockerfile*"]
    assert triggers["workflow_dispatch"]["inputs"]["images"]["default"] == "all"
    assert "refs/heads/main" in discovered["steps"][0]["run"]
    assert "scripts.ci.image.cli plan" in select["run"]
    assert "github.event.before" in select["env"]["BEFORE_SHA"]
    assert "--diff-filter=AM" in select["run"]
    assert promotions["strategy"]["matrix"] == (
        "${{ fromJSON(needs.discover.outputs.image_jobs) }}"
    )
    promote = next(step for step in promotions["steps"] if step.get("id") == "promote")
    assert '--image "$IMAGE_ID"' in promote["run"]
    assert "scripts.ci.image.cli promote" in promote["run"]
    assert "scripts.ci.image.cli resolve" not in yaml.safe_dump(promotions)
    assert "scripts.ci.image.cli check-envs" in yaml.safe_dump(promotions)
    assert set(gate["needs"]) == {"discover", "promotions"}


def test_ci_yaml_uses_one_environment_inline_model(repo_root: Path) -> None:
    document = yaml.safe_load(
        (repo_root / "scripts/ci/config/ci.yml").read_text(encoding="utf-8")
    )

    assert document["schema_version"] == 5
    assert list(document["scopes"]) == ["pull_request", "nightly"]
    assert "environments" not in document
    assert "lanes" not in document["resources"]
    assert "image_pr" not in document["scopes"]
    assert document["resources"]["images"]["npu-a3-cann90"]["build"] == {
        "dockerfile": "docker/npu/Dockerfile_aarch64",
        "platform": "linux/arm64",
        "revision": 1,
        "ci_build_args": {"GIT_HTTP_USER_AGENT": "git/2.56.1"},
    }
    assert {
        selection["environment"]["image"]
        for selection in document["scopes"]["pull_request"]
    } == {"cpu-default", "npu-a3-cann90"}


def test_nightly_reuses_the_same_graph_without_building_images(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-nightly.yml")
    job = document["jobs"]["nightly"]

    assert job["uses"] == "./.github/workflows/ascend-pr.yml"
    assert job["with"] == {"mode": "full"}
    assert "scripts.ci.image.cli" not in yaml.safe_dump(document)


def test_manual_workflow_can_pair_an_image_ref_with_an_image_id(
    repo_root: Path,
) -> None:
    document = _workflow(repo_root, "ascend-manual.yml")
    job = document["jobs"]["manual"]

    assert "image_id" in _triggers(document)["workflow_dispatch"]["inputs"]
    assert job["with"]["image_id"] == "${{ inputs.image_id }}"
