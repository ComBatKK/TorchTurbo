from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ci.image.__main__ import main
from scripts.ci.image.plan import ImageError, plan_image, split_reference


def test_image_plan_uses_cann_version_hardware_and_alias(repo_root: Path) -> None:
    plan = plan_image(repo_root, "npu-default")

    assert plan.alias == "cann9.0.0-a3-base"
    assert plan.hardware == "a3"
    assert plan.cann_version == "9.0.0"
    assert plan.content_tag == f"cann9.0.0-a3-c{plan.content_hash}"
    assert plan.content_ref.endswith(f":{plan.content_tag}")
    assert plan.action == "resolve"
    assert plan.repository.endswith("/ascend/torchturbo_npu")


def test_image_hash_ignores_business_code_and_tracks_dockerfile(
    repo_root: Path, tmp_path: Path
) -> None:
    config_dir = tmp_path / "scripts" / "ci" / "config"
    config_dir.mkdir(parents=True)
    shutil.copy(repo_root / "scripts/ci/config/ci.yml", config_dir / "ci.yml")
    shutil.copytree(repo_root / "scripts/ci/config/envs", config_dir / "envs")
    (tmp_path / "tests/integration_tests/scripts").mkdir(parents=True)
    (tmp_path / "tests/integration_tests/scripts/run_tests.py").write_text(
        "", encoding="utf-8"
    )
    override = tmp_path / "tests/unit_tests/models/bailing_moe_v3_vl"
    override.mkdir(parents=True)
    (override / "test_lf_image_parity.py").write_text("", encoding="utf-8")
    context = tmp_path / "docker" / "npu"
    context.mkdir(parents=True)
    dockerfile = context / "Dockerfile_aarch64"
    dockerfile.write_text("FROM example:cann@sha256:abc\n", encoding="utf-8")

    first = plan_image(tmp_path, "npu-default")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "business.py").write_text("value = 1\n", encoding="utf-8")
    second = plan_image(tmp_path, "npu-default")
    dockerfile.write_text("FROM example:cann@sha256:def\n", encoding="utf-8")
    third = plan_image(tmp_path, "npu-default")

    assert first.content_hash == second.content_hash
    assert first.content_tag == second.content_tag
    assert first.content_hash != third.content_hash
    assert first.content_tag != third.content_tag


def test_split_reference_handles_registry_port() -> None:
    assert split_reference("registry.example:5000/team/image:a3-base") == (
        "registry.example:5000/team/image",
        "a3-base",
    )


def test_candidate_build_can_be_blocked(repo_root: Path) -> None:
    plan = plan_image(
        repo_root,
        "npu-default",
        policy="build_if_missing",
        build_allowed=False,
    )
    assert plan.action == "blocked"


def test_alias_must_include_cann_version_and_hardware(tmp_path: Path) -> None:
    config = tmp_path / "scripts/ci/config/ci.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "resources:\n"
        "  targets:\n"
        "    npu-a3: {device: npu, hardware: a3}\n"
        "  images:\n"
        "    npu-default:\n"
        "      target: npu-a3\n"
        "      kind: container\n"
        "      ref: registry.example/team/image:a3-base\n"
        "      build:\n"
        "        dockerfile: docker/npu/Dockerfile_aarch64\n"
        "        context: docker/npu\n"
        "        platform: linux/arm64\n"
        "        cann_version: '9.0.0'\n",
        encoding="utf-8",
    )
    context = tmp_path / "docker/npu"
    context.mkdir(parents=True)
    (context / "Dockerfile_aarch64").write_text("FROM base:one\n", encoding="utf-8")

    with pytest.raises(ImageError, match="alias must start with"):
        plan_image(tmp_path, "npu-default")


def test_auto_policy_only_builds_when_image_inputs_change(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    ci_config = tmp_path / "scripts/ci/config/ci.yml"
    ci_config.parent.mkdir(parents=True)
    ci_config.write_text(
        "resources:\n"
        "  targets:\n"
        "    npu-a3: {device: npu, hardware: a3}\n"
        "  images:\n"
        "    npu-default:\n"
        "      target: npu-a3\n"
        "      kind: container\n"
        "      ref: registry.example/team/image:cann9.0.0-a3-base\n"
        "      build:\n"
        "        dockerfile: docker/npu/Dockerfile_aarch64\n"
        "        context: docker/npu\n"
        "        platform: linux/arm64\n"
        "        cann_version: '9.0.0'\n",
        encoding="utf-8",
    )
    context = tmp_path / "docker/npu"
    context.mkdir(parents=True)
    dockerfile = context / "Dockerfile_aarch64"
    dockerfile.write_text("FROM base:one\n", encoding="utf-8")

    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI Test")
    git("add", ".")
    git("commit", "-qm", "base")
    base_sha = git("rev-parse", "HEAD")

    assert plan_image(tmp_path, "npu-default", base_sha=base_sha).action == "resolve"

    dockerfile.write_text("FROM base:two\n", encoding="utf-8")
    changed = plan_image(tmp_path, "npu-default", base_sha=base_sha)
    assert changed.image_changed is True
    assert changed.action == "build_if_missing"

    blocked = plan_image(
        tmp_path,
        "npu-default",
        base_sha=base_sha,
        build_allowed=False,
    )
    assert blocked.action == "blocked"


def test_image_identity_can_be_computed_from_a_git_revision(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    config = tmp_path / "scripts/ci/config/ci.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "resources:\n"
        "  targets:\n"
        "    npu-a3: {device: npu, hardware: a3}\n"
        "  images:\n"
        "    npu-default:\n"
        "      target: npu-a3\n"
        "      kind: container\n"
        "      ref: registry.example/team/image:cann9.0.0-a3-base\n"
        "      build:\n"
        "        dockerfile: docker/npu/Dockerfile_aarch64\n"
        "        context: docker/npu\n"
        "        platform: linux/arm64\n"
        "        cann_version: '9.0.0'\n",
        encoding="utf-8",
    )
    context = tmp_path / "docker/npu"
    context.mkdir(parents=True)
    dockerfile = context / "Dockerfile_aarch64"
    dockerfile.write_text("FROM base:one\n", encoding="utf-8")

    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI Test")
    git("add", ".")
    git("commit", "-qm", "base")
    base_sha = git("rev-parse", "HEAD")
    base = plan_image(tmp_path, "npu-default", revision=base_sha)

    dockerfile.write_text("FROM base:two\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "head")
    head_sha = git("rev-parse", "HEAD")
    head = plan_image(tmp_path, "npu-default", revision=head_sha)

    assert base.content_ref == f"{base.repository}:{base.content_tag}"
    assert head.content_tag != base.content_tag
    assert plan_image(tmp_path, "npu-default").content_tag == head.content_tag


def test_resolve_candidate_policy_uses_only_the_content_tag(repo_root: Path) -> None:
    plan = plan_image(repo_root, "npu-default", policy="resolve_candidate")

    assert plan.action == "resolve_candidate"


def test_identity_cli_emits_a_content_ref_without_registry_access(
    repo_root: Path, tmp_path: Path
) -> None:
    output = tmp_path / "github-output"
    assert main([
        "identity",
        "--root",
        str(repo_root),
        "--image",
        "npu-default",
        "--github-output",
        str(output),
    ]) == 0

    expected = plan_image(repo_root, "npu-default").content_ref
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["content_ref"] == expected


def test_refs_cli_resolves_only_container_images(
    repo_root: Path, tmp_path: Path
) -> None:
    output = tmp_path / "image-refs"
    assert main([
        "refs",
        "--root",
        str(repo_root),
        "--output",
        str(output),
    ]) == 0

    refs = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if line
    )
    expected = plan_image(repo_root, "npu-default").content_ref
    assert refs == {"npu-default": expected}
