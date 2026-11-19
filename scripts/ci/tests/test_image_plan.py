from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci.image.cli import main
from scripts.ci.image.identity import image_plan, image_plans
from scripts.ci.image.recipe import ImageError, split_reference


def _write_repo(root: Path) -> Path:
    config = root / "scripts/ci/config/ci.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump(
            {
                "resources": {
                    "targets": {"npu-a3": {"device": "npu", "hardware": "a3"}},
                    "images": {
                        "npu-default": {
                            "target": "npu-a3",
                            "kind": "container",
                            "default": True,
                            "recipe": {
                                "ref": "registry.example/team/image:cann9.0.0-a3-base",
                                "dockerfile": "docker/npu/Dockerfile_aarch64",
                                "context": "docker/npu",
                                "platform": "linux/arm64",
                                "cann_version": "9.0.0",
                                "revision": 1,
                            },
                        }
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    context = root / "docker/npu"
    context.mkdir(parents=True)
    (context / "Dockerfile_aarch64").write_text(
        "FROM base:one\nCOPY setup.sh /setup.sh\n", encoding="utf-8"
    )
    (context / "setup.sh").write_text("echo one\n", encoding="utf-8")
    return config


def test_identity_tracks_recipe_and_the_whole_build_context(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    first = image_plan(tmp_path, "npu-default")

    (tmp_path / "src").mkdir()
    (tmp_path / "src/business.py").write_text("value = 1\n", encoding="utf-8")
    assert image_plan(tmp_path, "npu-default").content_tag == first.content_tag

    (tmp_path / "docker/npu/setup.sh").write_text("echo two\n", encoding="utf-8")
    context_change = image_plan(tmp_path, "npu-default")
    assert context_change.content_tag != first.content_tag

    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-default"]["recipe"]["build_args"] = {
        "PIP_INDEX_URL": "https://example.invalid"
    }
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    recipe_change = image_plan(tmp_path, "npu-default")
    assert recipe_change.content_tag != context_change.content_tag


def test_alias_is_not_part_of_content_identity(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    first = image_plan(tmp_path, "npu-default")
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-default"]["recipe"]["ref"] = (
        "registry.example/team/image:cann9.0.0-a3-next"
    )
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    assert image_plan(tmp_path, "npu-default").content_tag == first.content_tag


def test_alias_must_include_cann_version_and_hardware(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-default"]["recipe"]["ref"] = (
        "registry.example/team/image:a3-base"
    )
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ImageError, match="alias must start with"):
        image_plan(tmp_path, "npu-default")


def test_version_prefix_supports_non_cann_images(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["targets"] = {
        "gpu-h100": {"device": "gpu", "hardware": "h100"}
    }
    recipe = document["resources"]["images"]["npu-default"]["recipe"]
    recipe.update({
        "ref": "registry.example/team/image:cuda12.4-h100-base",
        "dockerfile": "docker/gpu/Dockerfile",
        "context": "docker/gpu",
        "platform": "linux/amd64",
        "version": "12.4",
        "version_prefix": "cuda",
    })
    recipe.pop("cann_version")
    document["resources"]["images"]["npu-default"].update({
        "target": "gpu-h100",
    })
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    context = tmp_path / "docker/gpu"
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM base:gpu\n", encoding="utf-8")

    plan = image_plan(tmp_path, "npu-default")

    assert plan.spec.version == "12.4"
    assert plan.spec.version_prefix == "cuda"
    assert plan.content_tag.startswith("cuda12.4-h100-c")


def test_build_inputs_must_stay_under_the_docker_trigger_root(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-default"]["recipe"]["context"] = "src"
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ImageError, match="must be under docker/"):
        image_plan(tmp_path, "npu-default")


def test_identity_can_be_computed_from_a_git_revision(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    _write_repo(tmp_path)
    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI Test")
    git("add", ".")
    git("commit", "-qm", "base")
    base_sha = git("rev-parse", "HEAD")
    base = image_plan(tmp_path, "npu-default", revision=base_sha)

    setup = tmp_path / "docker/npu/setup.sh"
    setup.write_text("echo two\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "head")
    head_sha = git("rev-parse", "HEAD")
    head = image_plan(tmp_path, "npu-default", revision=head_sha)

    assert base.content_tag != head.content_tag
    assert image_plan(tmp_path, "npu-default").content_tag == head.content_tag


def test_split_reference_handles_registry_port() -> None:
    assert split_reference("registry.example:5000/team/image:a3-base") == (
        "registry.example:5000/team/image",
        "a3-base",
    )


def test_image_plans_sort_and_reject_unknown_names(repo_root: Path) -> None:
    plans = image_plans(repo_root)
    assert [plan.name for plan in plans] == ["npu-default"]
    with pytest.raises(ImageError, match="unknown logical image"):
        image_plans(repo_root, names="missing")


def test_plan_cli_outputs_only_the_public_matrix(repo_root: Path, tmp_path: Path) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("docker/npu/Dockerfile_aarch64\n", encoding="utf-8")
    output = tmp_path / "image-jobs.json"
    github_output = tmp_path / "github-output"

    assert main([
        "plan",
        "--root", str(repo_root),
        "--changed-files", str(changed),
        "--output", str(output),
        "--github-output", str(github_output),
    ]) == 0

    matrix = json.loads(output.read_text(encoding="utf-8"))
    assert matrix == {
        "include": [{
            "image": "npu-default",
            "target": "npu-a3",
            "device": "npu",
            "hardware": "a3",
        }]
    }
    values = github_output.read_text(encoding="utf-8")
    assert "image_count=1" in values
    assert "images=npu-default" in values
    assert "content_tag" not in values
    assert "dockerfile" not in values


def test_plan_cli_accepts_no_changed_images(
    repo_root: Path, tmp_path: Path
) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("src/model.py\n", encoding="utf-8")
    output = tmp_path / "image-jobs.json"
    github_output = tmp_path / "github-output"

    assert main([
        "plan",
        "--root", str(repo_root),
        "--changed-files", str(changed),
        "--output", str(output),
        "--github-output", str(github_output),
    ]) == 0

    assert json.loads(output.read_text(encoding="utf-8")) == {"include": []}
    values = github_output.read_text(encoding="utf-8").splitlines()
    assert "image_count=0" in values
    assert "images=" in values


def test_check_envs_cli_writes_toolchain_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.ci.image import cli

    output = tmp_path / "envs.txt"
    monkeypatch.setattr(
        cli,
        "check_envs",
        lambda: ["host=runner arch=arm64", "client=27.0.0", "buildx=v0.20.0"],
    )

    assert main(["check-envs", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "host=runner arch=arm64",
        "client=27.0.0",
        "buildx=v0.20.0",
    ]
