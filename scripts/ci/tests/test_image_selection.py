from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.image.selection import (
    SelectionError,
    matrix_payload,
    select_images,
)
from scripts.ci.image.cli import main
from scripts.ci.routing.ci_router import ChangeRoute, route_changes


def _write_two_image_repo(root: Path) -> None:
    config = root / "scripts/ci/config/ci.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump(
            {
                "resources": {
                    "targets": {
                        "npu-a3": {"device": "npu", "hardware": "a3"},
                        "npu-a5": {"device": "npu", "hardware": "a5"},
                    },
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
                            },
                        },
                        "npu-a5-default": {
                            "target": "npu-a5",
                            "kind": "container",
                            "default": True,
                            "recipe": {
                                "ref": "registry.example/team/image:cann9.0.0-a5-base",
                                "dockerfile": "docker/npu-a5/Dockerfile_a5",
                                "context": "docker/npu-a5",
                                "platform": "linux/arm64",
                                "cann_version": "9.0.0",
                            },
                        },
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for name in ("npu", "npu-a5"):
        context = root / "docker" / name
        context.mkdir(parents=True)
        (context / f"Dockerfile_{'aarch64' if name == 'npu' else 'a5'}").write_text(
            "FROM base:one\n", encoding="utf-8"
        )
        (context / "setup.sh").write_text("echo setup\n", encoding="utf-8")


def test_changed_files_select_only_mapped_images(repo_root: Path) -> None:
    selections = select_images(
        repo_root, changed_files=["docker/npu/Dockerfile_aarch64"]
    )

    assert [item.name for item in selections] == ["npu-default"]
    assert matrix_payload(selections) == {
        "include": [{
            "image": "npu-default",
            "target": "npu-a3",
            "device": "npu",
            "hardware": "a3",
        }]
    }


def test_changed_images_reject_unmapped_docker_input(repo_root: Path) -> None:
    with pytest.raises(SelectionError, match="not mapped"):
        select_images(repo_root, changed_files=["docker/other/Dockerfile_unknown"])


def test_changed_images_require_a_configured_dockerfile(repo_root: Path) -> None:
    with pytest.raises(SelectionError, match="no configured Dockerfile changed"):
        select_images(repo_root, changed_files=["docs/ci.md"])


def test_changed_context_does_not_trigger_an_image_build(tmp_path: Path) -> None:
    _write_two_image_repo(tmp_path)

    with pytest.raises(SelectionError, match="no configured Dockerfile changed"):
        select_images(tmp_path, changed_files=["docker/npu-a5/setup.sh"])


def test_config_change_alone_does_not_trigger_an_image_build(tmp_path: Path) -> None:
    _write_two_image_repo(tmp_path)

    with pytest.raises(SelectionError, match="no configured Dockerfile changed"):
        select_images(tmp_path, changed_files=["scripts/ci/config/ci.yml"])


def test_named_images_can_select_all_or_explicit_names(repo_root: Path) -> None:
    assert [item.name for item in select_images(repo_root, names="all")] == [
        "npu-default"
    ]
    assert [item.name for item in select_images(repo_root, names="npu-default")] == [
        "npu-default"
    ]
    with pytest.raises(SelectionError, match="unknown logical image"):
        select_images(repo_root, names="missing")


def test_multiple_changed_dockerfiles_produce_only_the_affected_matrix(
    tmp_path: Path,
) -> None:
    _write_two_image_repo(tmp_path)

    both = select_images(
        tmp_path,
        changed_files=[
            "docker/npu/Dockerfile_aarch64",
            "docker/npu-a5/Dockerfile_a5",
        ],
    )

    assert [item.name for item in both] == ["npu-a5-default", "npu-default"]
    assert {item.name: item.target for item in both} == {
        "npu-a5-default": "npu-a5",
        "npu-default": "npu-a3",
    }
    assert set(matrix_payload(both)["include"][0]) == {
        "image", "target", "device", "hardware"
    }


def test_new_image_name_with_new_dockerfile_selects_the_new_image(
    tmp_path: Path,
) -> None:
    _write_two_image_repo(tmp_path)
    config = tmp_path / "scripts/ci/config/ci.yml"
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-new-default"] = {
        "target": "npu-a3",
        "kind": "container",
        "default": False,
        "recipe": {
            "ref": "registry.example/team/image:cann9.0.0-a3-new",
            "dockerfile": "docker/npu-new/Dockerfile_new",
            "context": "docker/npu-new",
            "platform": "linux/arm64",
            "cann_version": "9.0.0",
        },
    }
    config.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    context = tmp_path / "docker/npu-new"
    context.mkdir(parents=True)
    (context / "Dockerfile_new").write_text("FROM base:new\n", encoding="utf-8")

    changed_files = [
        "scripts/ci/config/ci.yml",
        "docker/npu-new/Dockerfile_new",
    ]

    assert route_changes(changed_files) == ChangeRoute.MIXED
    selections = select_images(tmp_path, changed_files=changed_files)

    assert [item.name for item in selections] == ["npu-new-default"]


def test_plan_cli_publishes_the_image_jobs_output(repo_root: Path, tmp_path: Path) -> None:
    output = tmp_path / "image-jobs.json"
    github_output = tmp_path / "github-output"

    assert main([
        "plan",
        "--root", str(repo_root),
        "--images", "all",
        "--output", str(output),
        "--github-output", str(github_output),
    ]) == 0

    values = github_output.read_text(encoding="utf-8")
    assert "image_jobs={\"include\":" in values
    assert "image_count=1" in values
