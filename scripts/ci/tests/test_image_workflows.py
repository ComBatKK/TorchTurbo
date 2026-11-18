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
    assert set(jobs) == {"plan", "cpu", "candidates", "gate"}
    assert "ascend-pr.yml" not in dumped
    assert "approval" not in jobs
    assert "discover" not in jobs
    plan = jobs["plan"]
    assert plan["name"] == "Ascend-Image-PR-Plan"
    assert set(plan["outputs"]) == {
        "change_route", "cpu_needed", "cpu_jobs", "cpu_job_count", "image_jobs",
        "image_count", "images",
    }
    assert plan["outputs"]["image_jobs"].startswith("${{ steps.plan")
    approval = next(step for step in plan["steps"] if step["name"] == "require approval label for fork PRs")
    assert "image-ci/run" in approval["run"]
    plan_step = next(step for step in plan["steps"] if step.get("id") == "plan")
    assert "scripts.ci.routing.change_router" in plan_step["run"]
    assert "scripts.ci.image.selection" in plan_step["run"]
    assert "--plan-policy build_if_missing" in plan_step["run"]
    assert "--plans-output" in plan_step["run"]
    assert "scripts.ci.execution.producer.planner" in plan_step["run"]
    assert "--targets cpu" in plan_step["run"]
    assert "change_route=${route_output#change_route=}" in plan_step["run"]
    assert "cpu_needed=true" in plan_step["run"]
    assert "refs/pull/${{ github.event.pull_request.number }}/head" in yaml.safe_dump(
        plan
    )
    assert jobs["cpu"]["needs"] == "plan"
    assert jobs["cpu"]["strategy"]["matrix"]["include"] == (
        "${{ fromJSON(needs.plan.outputs.cpu_jobs) }}"
    )
    assert jobs["cpu"]["strategy"]["max-parallel"] == 5
    assert "scripts.ci.execution.consumer.executor" in yaml.safe_dump(jobs["cpu"])
    assert "ci-plan" in yaml.safe_dump(jobs["cpu"])
    assert set(jobs["candidates"]["needs"]) == {"plan", "cpu"}
    assert "needs.cpu.result == 'skipped'" in jobs["candidates"]["if"]
    assert jobs["candidates"]["uses"] == "./.github/workflows/ascend-image-pipeline.yml"
    assert jobs["candidates"]["strategy"]["fail-fast"] is False
    assert jobs["candidates"]["strategy"]["max-parallel"] == (
        "${{ fromJSON(vars.CI_IMAGE_MAX_PARALLEL || '2') }}"
    )
    assert jobs["candidates"]["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.image_jobs) }}"
    )
    assert jobs["candidates"]["with"]["image"] == "${{ matrix.image }}"
    assert jobs["candidates"]["with"]["image_mode"] == "${{ matrix.image_mode }}"
    assert jobs["candidates"]["with"]["image_plan"] == "${{ matrix.image_plan }}"
    assert jobs["candidates"]["with"]["content_tag"] == "${{ matrix.content_tag }}"
    assert set(jobs["gate"]["needs"]) == {"plan", "cpu", "candidates"}
    assert jobs["gate"]["name"] == "Ascend-Image-PR-Gate"
    assert "scripts.ci.execution.consumer.gate" in yaml.safe_dump(jobs["gate"])
    assert "promote" not in jobs
    assert {"candidate", "smoke", "tests"}.isdisjoint(jobs)


def test_image_pipeline_validation_is_serial_per_image(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-pipeline.yml")
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert set(jobs) == {"build", "tests", "promote"}
    assert jobs["build"]["uses"] == "./.github/workflows/ascend-image.yml"
    assert jobs["build"]["with"]["mode"] == "${{ inputs.image_mode }}"
    assert jobs["build"]["with"]["image_plan"] == "${{ inputs.image_plan }}"
    assert jobs["tests"]["if"] == "inputs.image_mode == 'build_if_missing'"
    assert jobs["tests"]["needs"] == "build"
    assert jobs["tests"]["uses"] == "./.github/workflows/ascend-pr.yml"
    assert jobs["tests"]["with"]["image_name"] == "${{ inputs.image }}"
    assert jobs["tests"]["with"]["image_ref"] == "${{ needs.build.outputs.ref }}"
    assert jobs["tests"]["with"]["targets"] == "${{ inputs.target }}"
    assert jobs["promote"]["if"] == "inputs.image_mode == 'resolve_candidate'"
    assert "smoke" not in dumped
    assert "scripts.ci.execution.consumer" not in dumped


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
    assert promotions["uses"] == "./.github/workflows/ascend-image-pipeline.yml"
    assert promotions["strategy"]["fail-fast"] is False
    assert promotions["strategy"]["max-parallel"] == (
        "${{ fromJSON(vars.CI_IMAGE_MAX_PARALLEL || '2') }}"
    )
    assert promotions["strategy"]["matrix"] == (
        "${{ fromJSON(needs.discover.outputs.image_jobs) }}"
    )
    assert promotions["with"]["image"] == "${{ matrix.image }}"
    assert promotions["with"]["image_mode"] == "resolve_candidate"
    assert promotions["with"]["checkout_ref"] == "${{ github.sha }}"
    assert promotions["secrets"]["SWR_USERNAME"] == "${{ secrets.SWR_USERNAME }}"
    assert set(gate["needs"]) == {"discover", "promotions"}
    assert "no affected image" in gate["steps"][0]["run"]
    assert "build_if_missing" not in yaml.safe_dump(document)


def test_image_pipeline_promotion_is_serial_and_fail_closed(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-pipeline.yml")
    jobs = document["jobs"]

    assert jobs["build"]["uses"] == "./.github/workflows/ascend-image.yml"
    assert jobs["build"]["with"]["mode"] == "${{ inputs.image_mode }}"
    assert jobs["promote"]["if"] == "inputs.image_mode == 'resolve_candidate'"
    assert jobs["promote"]["needs"] == "build"
    promote = next(step for step in jobs["promote"]["steps"] if step.get("id") == "promote")
    assert "scripts.ci.image promote" in promote["run"]
    assert "--digest" in promote["run"]


def test_reusable_image_workflow_accepts_explicit_checkout_ref(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image.yml")
    workflow_call = _triggers(document)["workflow_call"]

    assert workflow_call["inputs"]["checkout_ref"]["default"] == ""
    assert set(document["jobs"]) == {"image"}
    assert document["jobs"]["image"]["steps"][0]["with"]["ref"].startswith(
        "${{ inputs.checkout_ref"
    )
    assert workflow_call["inputs"]["image_plan"]["default"] == ""
    assert "--plan-file" in yaml.safe_dump(document)
    options = _triggers(document)["workflow_dispatch"]["inputs"]["mode"]["options"]
    assert "resolve_candidate" in options


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
