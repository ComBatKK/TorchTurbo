from __future__ import annotations

from pathlib import Path

import yaml


def _workflow(root: Path, name: str) -> dict:
    return yaml.safe_load((root / ".github/workflows" / name).read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    value = document.get(True, document.get("on"))
    assert isinstance(value, dict)
    return value


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


def test_normal_pr_workflow_resolves_runtime_images_once(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-pr.yml")
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert set(jobs) == {"plan", "cpu", "container", "gate"}
    assert jobs["gate"]["needs"] == ["plan", "cpu", "container"]
    assert jobs["container"]["strategy"]["matrix"] == {
        "include": "${{ fromJSON(needs.plan.outputs.container_jobs) }}"
    }
    assert jobs["container"]["strategy"]["max-parallel"] == (
        "${{ fromJSON(vars.CI_CONTAINER_MAX_PARALLEL || '2') }}"
    )
    assert jobs["container"]["container"]["options"] == (
        "${{ matrix.container_options }}"
    )
    assert jobs["container"]["if"].find(
        "needs.plan.outputs.container_job_count != '0'"
    ) >= 0
    assert "image" not in jobs
    assert all("image" not in str(job.get("needs", [])) for job in jobs.values())
    assert all("ascend-image-pipeline.yml" not in str(job.get("uses", "")) for job in jobs.values())
    assert "scripts.ci.image build " not in dumped
    assert "scripts.ci.image promote " not in dumped
    assert "SWR_USERNAME" not in dumped
    assert _triggers(document)["pull_request"]["paths-ignore"] == [
        "docker/**/Dockerfile*",
    ]
    assert _triggers(document)["workflow_call"]["inputs"]["targets"]["default"] == ""
    assert _triggers(document)["workflow_call"]["inputs"]["image_name"]["default"] == "npu-default"

    plan_steps = jobs["plan"]["steps"]
    step_names = [step.get("name", "") for step in plan_steps]
    assert step_names.index("route changes") > next(
        index for index, step in enumerate(plan_steps)
        if str(step.get("uses", "")).startswith("actions/setup-python")
    )
    assert step_names.index("install CI dependencies") > step_names.index(
        "route changes"
    )
    assert step_names.index("lint") > step_names.index("install CI dependencies")
    assert step_names.index("resolve runtime image") > step_names.index("lint")
    assert "resolve runtime digest" not in step_names
    assert step_names.index("plan") > step_names.index("resolve runtime image")
    classify = next(step for step in plan_steps if step.get("id") == "changes")
    assert "scripts.ci.routing.ci_router" in classify["run"]
    assert "--diff-filter=AM" in classify["run"]
    assert "scope=delegated" in classify["run"]
    identity = next(step for step in plan_steps if step.get("id") == "runtime_image")
    assert "steps.lint.outcome == 'success'" in identity["if"]
    assert "github.event.pull_request.base.sha" in identity["env"]["PR_BASE_SHA"]
    assert "scripts.ci.image resolve" in identity["run"]
    assert "--revision" in identity["run"]
    assert "ci-image-refs" in identity["run"]
    publish = next(step for step in plan_steps if step.get("id") == "publish")
    assert "--image-ref" in publish["run"]
    assert "ci-image-refs" in publish["env"]["IMAGE_REFS_FILE"]
    assert publish["run"].index("--fail-code lint") < publish["run"].index(
        "--fail-code image-identity"
    )
    assert 'COMMON+=(--image "$CI_IMAGE_NAME")' in publish["run"]
    assert _triggers(document)["workflow_dispatch"]["inputs"]["image_ref"]["default"] == ""


def test_image_pr_plans_and_builds_and_tests_images(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-pr.yml")
    triggers = _triggers(document)
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert "pull_request_target" in triggers
    assert triggers["pull_request_target"]["branches"] == [
        "main", "ci/pr-replay-baseline"
    ]
    assert triggers["pull_request_target"]["paths"] == ["docker/**/Dockerfile*"]
    assert "image-ci/run" in dumped
    assert "pull_request" not in triggers
    assert set(jobs) == {"plan", "cpu-tests", "build_and_test_images", "gate"}
    assert "ascend-pr.yml" not in dumped
    assert "approval" not in jobs
    assert "discover" not in jobs
    plan = jobs["plan"]
    assert plan["name"] == "Image plan"
    assert set(plan["outputs"]) == {
        "change_route", "cpu_jobs", "cpu_job_count", "image_jobs",
        "image_count", "images",
    }
    assert plan["outputs"]["image_jobs"].startswith("${{ steps.plan")
    approval = next(
        step for step in plan["steps"]
        if step.get("name") == "Fork PR approval"
    )
    assert "image-ci/run" in approval["run"]
    step_names = [step.get("name", "") for step in plan["steps"]]
    assert step_names.index("lint") < step_names.index("Fork PR approval")
    assert step_names.index("Fork PR approval") < step_names.index("Route and plan")
    plan_step = next(step for step in plan["steps"] if step.get("id") == "plan")
    assert "scripts.ci.routing.ci_router" in plan_step["run"]
    assert "scripts.ci.image plan" in plan_step["run"]
    assert "--diff-filter=AM" in plan_step["run"]
    assert "--base-revision" not in plan_step["run"]
    assert "--plan-policy" not in plan_step["run"]
    assert "--plans-output" not in plan_step["run"]
    assert "scripts.ci.execution.producer.planner" in plan_step["run"]
    assert plan_step["run"].index("scripts.ci.image plan") < plan_step["run"].index(
        'case "$route" in'
    )
    assert 'if [ "$effective_route" = mixed ]' in plan_step["run"]
    assert "printf 'cpu_jobs=[]\\ncpu_job_count=0\\n'" in plan_step["run"]
    assert "--targets cpu" in plan_step["run"]
    assert "case \"$route\" in" in plan_step["run"]
    assert "image_only|mixed" in plan_step["run"]
    assert "effective_route=no_image" in plan_step["run"]
    assert "refs/pull/${{ github.event.pull_request.number }}/head" in yaml.safe_dump(
        plan
    )
    assert jobs["cpu-tests"]["needs"] == "plan"
    assert jobs["cpu-tests"]["strategy"]["matrix"]["include"] == (
        "${{ fromJSON(needs.plan.outputs.cpu_jobs) }}"
    )
    assert jobs["cpu-tests"]["name"] == "${{ matrix.display_name }}"
    assert jobs["cpu-tests"]["strategy"]["max-parallel"] == 5
    assert "scripts.ci.execution.consumer.executor" in yaml.safe_dump(jobs["cpu-tests"])
    assert "ci-plan" in yaml.safe_dump(jobs["cpu-tests"])
    assert set(jobs["build_and_test_images"]["needs"]) == {"plan", "cpu-tests"}
    assert "needs.cpu-tests.result == 'skipped'" in jobs["build_and_test_images"]["if"]
    assert jobs["build_and_test_images"]["uses"] == (
        "./.github/workflows/ascend-image-pipeline.yml"
    )
    assert jobs["build_and_test_images"]["strategy"]["fail-fast"] is False
    assert jobs["build_and_test_images"]["strategy"]["max-parallel"] == (
        "${{ fromJSON(vars.CI_IMAGE_MAX_PARALLEL || '3') }}"
    )
    assert jobs["build_and_test_images"]["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.image_jobs) }}"
    )
    assert jobs["build_and_test_images"]["with"] == {
        "image": "${{ matrix.image }}",
        "target": "${{ matrix.target }}",
        "checkout_ref": "refs/pull/${{ github.event.pull_request.number }}/head",
        "max_jobs": "auto",
        "use_cache": True,
        "run_tests": True,
    }
    assert set(jobs["gate"]["needs"]) == {
        "plan", "cpu-tests", "build_and_test_images"
    }
    assert jobs["gate"]["name"] == "Image gate"
    assert "scripts.ci.execution.consumer.gate" in yaml.safe_dump(jobs["gate"])
    assert "promote" not in jobs
    assert {
        "candidate", "candidates", "image_pipelines", "smoke", "tests"
    }.isdisjoint(jobs)


def test_image_pipeline_validation_is_serial_per_image(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-pipeline.yml")
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert set(jobs) == {"build", "container-tests", "gate"}
    build = jobs["build"]
    assert "uses" not in build
    assert set(build["outputs"]) == {
        "ref", "container_jobs", "container_job_count"
    }
    assert build["concurrency"]["group"] == (
        "image-${{ inputs.image }}-${{ inputs.checkout_ref || github.sha }}"
    )
    assert "scripts.ci.image build" in dumped
    assert "scripts.ci.image check-envs" in dumped
    assert "scripts.ci.image resolve" not in dumped
    assert "inputs.image_mode" not in dumped
    assert "inputs.image_plan" not in dumped
    assert "inputs.content_tag" not in dumped
    assert "docker/login-action@v3" in dumped
    assert "docker/setup-buildx-action@v3" in dumped
    build_steps = [step.get("name") or step.get("uses", "") for step in build["steps"]]
    assert build_steps.index("check build environment") > build_steps.index(
        "docker/setup-buildx-action@v3"
    )
    assert jobs["container-tests"]["needs"] == "build"
    assert jobs["container-tests"]["strategy"]["max-parallel"] == (
        "${{ fromJSON(vars.CI_CONTAINER_MAX_PARALLEL || '2') }}"
    )
    assert jobs["container-tests"]["container"]["options"] == (
        "${{ matrix.container_options }}"
    )
    assert jobs["gate"]["needs"] == "container-tests"
    assert "ascend-pr.yml" not in dumped
    assert "scripts.ci.execution.producer.planner" in dumped
    assert "--image-ref" in dumped
    plan_tests = next(
        step for step in build["steps"]
        if step.get("name") == "plan candidate tests"
    )
    assert '--image "$IMAGE_NAME"' in plan_tests["run"]
    assert "${{ steps.result.outputs.ref }}" in yaml.safe_dump(build)
    assert "${{ needs.build.outputs.ref }}" in yaml.safe_dump(
        jobs["container-tests"]["container"]
    )
    assert "${{ inputs.target }}" in yaml.safe_dump(build)
    assert "scripts.ci.tools.lint" not in dumped
    assert "inputs.run_tests" in yaml.safe_dump(build)
    assert "smoke" not in dumped
    assert "scripts.ci.execution.consumer.executor" in yaml.safe_dump(
        jobs["container-tests"]
    )
    assert "scripts.ci.execution.consumer.gate" in yaml.safe_dump(jobs["gate"])
    assert jobs["container-tests"]["name"] == (
        "Container (${{ matrix.display_name }})"
    )
    assert jobs["gate"]["name"] == "Candidate gate"
    assert "ci-plan-${{ inputs.image }}" in dumped
    assert "ci-result-${{ inputs.image }}" in dumped
    assert "scripts.ci.image promote " not in dumped


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
    assert discovered["outputs"]["image_jobs"].startswith("${{ steps.select")
    assert "scripts.ci.image plan" in select["run"]
    assert "--base-revision" not in select["run"]
    assert "github.event.before" in select["env"]["BEFORE_SHA"]
    assert "--diff-filter=AM" in select["run"]
    assert "--images" in select["run"]
    assert promotions["needs"] == "discover"
    assert "uses" not in promotions
    assert promotions["runs-on"].startswith("${{ vars.CI_CPU_RUNNER")
    assert promotions["strategy"]["fail-fast"] is False
    assert promotions["strategy"]["max-parallel"] == (
        "${{ fromJSON(vars.CI_IMAGE_MAX_PARALLEL || '2') }}"
    )
    assert promotions["strategy"]["matrix"] == (
        "${{ fromJSON(needs.discover.outputs.image_jobs) }}"
    )
    assert "scripts.ci.image resolve" not in yaml.safe_dump(promotions)
    assert "scripts.ci.image promote" in yaml.safe_dump(promotions)
    assert "scripts.ci.image check-envs" in yaml.safe_dump(promotions)
    assert set(gate["needs"]) == {"discover", "promotions"}
    assert "no Dockerfile changed" in gate["steps"][0]["run"]
    assert "build_if_missing" not in yaml.safe_dump(document)


def test_image_promotion_is_one_black_box_operation(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-promote.yml")
    steps = document["jobs"]["promotions"]["steps"]
    promote = next(step for step in steps if step.get("id") == "promote")

    assert "scripts.ci.image promote" in promote["run"]
    assert "DIGEST" not in promote["env"]
    assert "scripts.ci.image resolve" not in yaml.safe_dump(steps)


def test_image_pipeline_accepts_manual_build_and_explicit_checkout_ref(
    repo_root: Path,
) -> None:
    document = _workflow(repo_root, "ascend-image-pipeline.yml")
    triggers = _triggers(document)
    workflow_call = triggers["workflow_call"]

    assert workflow_call["inputs"]["checkout_ref"]["default"] == ""
    assert workflow_call["inputs"]["run_tests"]["default"] is True
    assert document["jobs"]["build"]["steps"][0]["with"]["ref"].startswith(
        "${{ inputs.checkout_ref"
    )
    assert "--plan-file" not in yaml.safe_dump(document["jobs"]["build"])
    assert "image_plan" not in triggers["workflow_call"]["inputs"]
    assert "image_mode" not in triggers["workflow_dispatch"]["inputs"]
    assert triggers["workflow_dispatch"]["inputs"]["run_tests"]["default"] is False


def test_pr_scope_is_sliced_by_target_for_image_pipeline(repo_root: Path) -> None:
    document = yaml.safe_load(
        (repo_root / "scripts/ci/config/ci.yml").read_text(encoding="utf-8")
    )
    scope = document["scopes"]["pull_request"]

    assert list(document["scopes"]) == ["pull_request", "nightly"]
    assert scope["targets"] == ["cpu", "npu-a3"]
    assert scope["pytest"] == {"core": "all", "model": "none", "adapter": "all"}
    assert scope["integration"]["base_suite"] == {
        "mode": "explicit",
        "cases": ["qwen3_5_moe/sft_2cpu", "qwen3_5_moe/sft_2npu"],
    }


def test_nightly_reuses_full_test_scope_without_building_images(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-nightly.yml")
    job = document["jobs"]["nightly"]

    assert job["uses"] == "./.github/workflows/ascend-pr.yml"
    assert job["with"] == {"mode": "full"}
    assert "scripts.ci.image" not in yaml.safe_dump(document)
