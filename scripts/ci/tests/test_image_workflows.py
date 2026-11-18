from __future__ import annotations

from pathlib import Path

import yaml


def _workflow(root: Path, name: str) -> dict:
    return yaml.safe_load((root / ".github/workflows" / name).read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    value = document.get(True, document.get("on"))
    assert isinstance(value, dict)
    return value


def test_normal_pr_workflow_only_derives_a_content_ref(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-pr.yml")
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert "image" not in jobs
    assert all("image" not in str(job.get("needs", [])) for job in jobs.values())
    assert all("ascend-image-pipeline.yml" not in str(job.get("uses", "")) for job in jobs.values())
    assert "scripts.ci.image build" not in dumped
    assert "scripts.ci.image promote" not in dumped
    assert "SWR_USERNAME" not in dumped
    assert _triggers(document)["pull_request"]["paths-ignore"] == [
        "docker/**/Dockerfile*"
    ]
    assert _triggers(document)["workflow_call"]["inputs"]["targets"]["default"] == ""
    assert _triggers(document)["workflow_call"]["inputs"]["image_name"]["default"] == "npu-default"

    plan_steps = jobs["plan"]["steps"]
    classify = next(step for step in plan_steps if step.get("id") == "changes")
    assert "scope=delegated" in classify["run"]
    identity = next(step for step in plan_steps if step.get("id") == "runtime_image")
    assert "github.event.pull_request.base.sha" in identity["env"]["PR_BASE_SHA"]
    assert "scripts.ci.image refs" in identity["run"]
    assert "--revision" in identity["run"]
    assert "ci-image-refs" in identity["run"]
    assert "--image-ref" in plan_steps[-2]["run"]
    assert _triggers(document)["workflow_dispatch"]["inputs"]["image_ref"]["default"] == ""


def test_image_pr_plans_builds_and_tests_the_candidate(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-pr.yml")
    triggers = _triggers(document)
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert "pull_request_target" in triggers
    assert triggers["pull_request_target"]["branches"] == ["main"]
    assert triggers["pull_request_target"]["paths"] == ["docker/**/Dockerfile*"]
    assert "image-ci/run" in dumped
    assert "pull_request" not in triggers
    assert set(jobs) == {"plan", "cpu-tests", "candidates", "gate"}
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
    assert "scripts.ci.routing.change_router" in plan_step["run"]
    assert "scripts.ci.image.selection" in plan_step["run"]
    assert "--plan-policy build_if_missing" in plan_step["run"]
    assert "--plans-output" in plan_step["run"]
    assert "scripts.ci.execution.producer.planner" in plan_step["run"]
    assert 'if [ "$route" = mixed ]' in plan_step["run"]
    assert "printf 'cpu_jobs=[]\\ncpu_job_count=0\\n'" in plan_step["run"]
    assert "--targets cpu" in plan_step["run"]
    assert "route=${route#change_route=}" in plan_step["run"]
    assert "case \"$route\" in" in plan_step["run"]
    assert "image_only|mixed" in plan_step["run"]
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
    assert set(jobs["candidates"]["needs"]) == {"plan", "cpu-tests"}
    assert "needs.cpu-tests.result == 'skipped'" in jobs["candidates"]["if"]
    assert jobs["candidates"]["uses"] == "./.github/workflows/ascend-image-pipeline.yml"
    assert jobs["candidates"]["strategy"]["fail-fast"] is False
    assert jobs["candidates"]["strategy"]["max-parallel"] == (
        "${{ fromJSON(vars.CI_IMAGE_MAX_PARALLEL || '3') }}"
    )
    assert jobs["candidates"]["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.image_jobs) }}"
    )
    assert jobs["candidates"]["with"]["image"] == "${{ matrix.image }}"
    assert jobs["candidates"]["with"]["image_plan"] == "${{ matrix.image_plan }}"
    assert jobs["candidates"]["with"]["content_tag"] == "${{ matrix.content_tag }}"
    assert set(jobs["gate"]["needs"]) == {"plan", "cpu-tests", "candidates"}
    assert jobs["gate"]["name"] == "Image gate"
    assert "scripts.ci.execution.consumer.gate" in yaml.safe_dump(jobs["gate"])
    assert "promote" not in jobs
    assert {"candidate", "smoke", "tests"}.isdisjoint(jobs)


def test_image_pipeline_validation_is_serial_per_image(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-pipeline.yml")
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert set(jobs) == {"build", "tests"}
    build = jobs["build"]
    assert "uses" not in build
    assert set(build["outputs"]) == {"ref", "digest", "content_tag", "built"}
    assert build["concurrency"]["group"] == "image-${{ inputs.content_tag || inputs.image }}"
    assert "scripts.ci.image build" in dumped
    assert "scripts.ci.image resolve" not in dumped
    assert "inputs.image_mode" not in dumped
    assert "--plan-file" in dumped
    assert "docker buildx" in dumped
    assert "inputs.run_tests" in jobs["tests"]["if"]
    assert jobs["tests"]["needs"] == "build"
    assert jobs["tests"]["uses"] == "./.github/workflows/ascend-pr.yml"
    assert jobs["tests"]["with"]["image_name"] == "${{ inputs.image }}"
    assert jobs["tests"]["with"]["image_ref"] == "${{ needs.build.outputs.ref }}"
    assert jobs["tests"]["with"]["targets"] == "${{ inputs.target }}"
    assert "smoke" not in dumped
    assert "scripts.ci.execution.consumer" not in dumped
    assert "scripts.ci.image promote" not in dumped


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
    assert "scripts.ci.image.selection" in select["run"]
    assert "github.event.before" in select["env"]["BEFORE_SHA"]
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
    assert "scripts.ci.image resolve-candidate" in yaml.safe_dump(promotions)
    assert "scripts.ci.image promote" in yaml.safe_dump(promotions)
    assert set(gate["needs"]) == {"discover", "promotions"}
    assert "no affected image" in gate["steps"][0]["run"]
    assert "build_if_missing" not in yaml.safe_dump(document)


def test_image_promotion_resolves_then_promotes_the_same_digest(
    repo_root: Path,
) -> None:
    document = _workflow(repo_root, "ascend-image-promote.yml")
    steps = document["jobs"]["promotions"]["steps"]
    resolve = next(step for step in steps if step.get("id") == "resolve")
    promote = next(step for step in steps if step.get("id") == "promote")

    assert "scripts.ci.image resolve-candidate" in resolve["run"]
    assert promote["env"]["DIGEST"] == "${{ steps.resolve.outputs.digest }}"
    assert "scripts.ci.image promote" in promote["run"]
    assert "--digest" in promote["run"]


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
    assert "--plan-file" in yaml.safe_dump(document["jobs"]["build"])
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
