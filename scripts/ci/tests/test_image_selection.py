from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from scripts.ci.image.selection import (
    SelectionError,
    changed_images,
    matrix_payload,
    named_images,
)


def test_changed_images_selects_only_modified_dockerfile(repo_root: Path) -> None:
    selections = changed_images(
        repo_root,
        ["docs/ci.md", "docker/npu/Dockerfile_aarch64"],
    )

    assert [item.image for item in selections] == ["npu-default"]
    assert matrix_payload(selections) == {
        "include": [{
            "image": "npu-default",
            "target": "npu-a3",
            "device": "npu",
            "hardware": "a3",
            "dockerfile": "docker/npu/Dockerfile_aarch64",
            "smoke_runner_variable": "CI_NPU_2_RUNNER",
            "smoke_runner": "linux-aarch64-a3-800i-2",
        }]
    }


def test_changed_images_rejects_unmapped_dockerfile(repo_root: Path) -> None:
    with pytest.raises(SelectionError, match="not mapped"):
        changed_images(repo_root, ["docker/npu/Dockerfile_unknown"])


def test_changed_images_requires_a_dockerfile(repo_root: Path) -> None:
    with pytest.raises(SelectionError, match="no configured Dockerfile changed"):
        changed_images(repo_root, ["docs/ci.md"])


def test_named_images_can_select_all_or_explicit_names(repo_root: Path) -> None:
    assert [item.image for item in named_images(repo_root, "all")] == ["npu-default"]
    assert [item.image for item in named_images(repo_root, "npu-default")] == [
        "npu-default"
    ]
    with pytest.raises(SelectionError, match="unknown logical image"):
        named_images(repo_root, "missing")


def test_multiple_changed_dockerfiles_produce_only_the_affected_matrix(
    repo_root: Path, tmp_path: Path
) -> None:
    document = yaml.safe_load(
        (repo_root / "scripts/ci/config/ci.yml").read_text(encoding="utf-8")
    )
    document = copy.deepcopy(document)
    document["resources"]["targets"]["npu-a5"] = {
        "device": "npu",
        "hardware": "a5",
    }
    document["resources"]["images"]["npu-a5-default"] = {
        "target": "npu-a5",
        "kind": "container",
        "ref": "swr.example/ascend/torchturbo_npu:cann9.0.0-a5-base",
        "build": {
            "dockerfile": "docker/npu/Dockerfile_a5",
            "context": "docker/npu-a5",
            "platform": "linux/arm64",
            "cann_version": "9.0.0",
            "revision": 1,
        },
    }
    document["resources"]["lanes"]["npu/a5-2"] = {
        "target": "npu-a5",
        "capacity_slots": 2,
        "runner_variable": "CI_NPU_A5_2_RUNNER",
        "runner": "linux-aarch64-a5-2",
    }
    config = tmp_path / "scripts/ci/config/ci.yml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    only_a3 = changed_images(tmp_path, ["docker/npu/Dockerfile_aarch64"])
    assert [item.image for item in only_a3] == ["npu-default"]

    both = changed_images(
        tmp_path,
        ["docker/npu/Dockerfile_aarch64", "docker/npu/Dockerfile_a5"],
    )
    assert [item.image for item in both] == ["npu-a5-default", "npu-default"]
    assert {item.image: item.target for item in both} == {
        "npu-a5-default": "npu-a5",
        "npu-default": "npu-a3",
    }
