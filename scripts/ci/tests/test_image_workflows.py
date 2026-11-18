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
    assert all("ascend-image.yml" not in str(job.get("uses", "")) for job in jobs.values())
    assert "scripts.ci.image build" not in dumped
    assert "scripts.ci.image promote" not in dumped
    assert "SWR_USERNAME" not in dumped
    assert _triggers(document)["pull_request"]["paths-ignore"] == [
        "docker/**/Dockerfile*"
    ]
    assert _triggers(document)["workflow_call"]["inputs"]["targets"]["default"] == ""

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


def test_image_pr_builds_smokes_and_tests_the_candidate(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-pr.yml")
    triggers = _triggers(document)
    jobs = document["jobs"]
    dumped = yaml.safe_dump(document)

    assert "pull_request_target" in triggers
    assert triggers["pull_request_target"]["branches"] == ["main"]
    assert triggers["pull_request_target"]["paths"] == ["docker/**/Dockerfile*"]
    assert "image-ci/run" in dumped
    assert "pull_request" not in triggers
    assert jobs["approval"]["name"] == "Ascend-Image-Approval"
    assert jobs["classify"]["outputs"]["cpu_needed"].startswith("${{ steps.classify")
    classify = jobs["classify"]["steps"][-1]
    assert "scripts.ci.routing.change_router" in classify["run"]
    assert "change_route=mixed" in classify["run"]
    assert "refs/pull/${PR_NUMBER}/head" in classify["run"]
    assert jobs["cpu"]["uses"] == "./.github/workflows/ascend-pr.yml"
    assert jobs["cpu"]["with"] == {
        "mode": "pr",
        "scope": "pull_request",
        "targets": "cpu",
        "checkout_ref": "refs/pull/${{ github.event.pull_request.number }}/head",
    }
    assert set(jobs["candidate"]["needs"]) == {"approval", "classify", "cpu"}
    assert "needs.cpu.result == 'skipped'" in jobs["candidate"]["if"]
    assert jobs["candidate"]["uses"] == "./.github/workflows/ascend-image.yml"
    assert jobs["candidate"]["with"]["mode"] == "build_if_missing"
    assert jobs["candidate"]["with"]["checkout_ref"].startswith("refs/pull/")
    assert jobs["smoke"]["needs"] == "candidate"
    assert jobs["tests"]["uses"] == "./.github/workflows/ascend-pr.yml"
    assert set(jobs["tests"]["needs"]) == {"candidate", "smoke"}
    assert jobs["tests"]["with"] == {
        "mode": "pr",
        "scope": "pull_request",
        "targets": "npu-a3",
        "checkout_ref": "refs/pull/${{ github.event.pull_request.number }}/head",
        "image_ref": "${{ needs.candidate.outputs.ref }}",
    }
    assert set(jobs["gate"]["needs"]) == {
        "approval", "classify", "cpu", "candidate", "smoke", "tests"
    }
    assert jobs["gate"]["name"] == "Ascend-Image-PR-Gate"
    assert "promote" not in jobs
    assert "scripts.ci.execution.consumer" not in dumped


def test_image_promote_runs_only_after_smoke(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image-promote.yml")
    triggers = _triggers(document)
    jobs = document["jobs"]

    assert triggers["push"]["branches"] == ["main"]
    assert jobs["image"]["with"]["mode"] == "resolve_candidate"
    assert "secrets" not in jobs["image"]
    assert jobs["smoke"]["needs"] == "image"
    assert set(jobs["promote"]["needs"]) == {"image", "smoke"}
    assert "scripts.ci.image promote" in jobs["promote"]["steps"][-2]["run"]
    assert "--digest" in jobs["promote"]["steps"][-2]["run"]
    assert "build_if_missing" not in yaml.safe_dump(document)


def test_reusable_image_workflow_accepts_explicit_checkout_ref(repo_root: Path) -> None:
    document = _workflow(repo_root, "ascend-image.yml")
    workflow_call = _triggers(document)["workflow_call"]

    assert workflow_call["inputs"]["checkout_ref"]["default"] == ""
    assert document["jobs"]["plan"]["steps"][0]["with"]["ref"].startswith(
        "${{ inputs.checkout_ref"
    )
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
