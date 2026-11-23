from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.image.cli import main
from scripts.ci.image.identity import image_plan, image_plans
from scripts.ci.image.recipe import ImageError, load_specs, split_reference


def _config(name: str = "npu-a3-cann90") -> dict:
    return {
        "schema_version": 5,
        "resources": {
            "targets": {
                "cpu": {"device": "cpu"},
                "npu-a3": {"device": "npu", "hardware": "a3"},
            },
            "images": {
                "cpu-default": {
                    "target": "cpu",
                    "kind": "host",
                    "ref": "python:3.11",
                },
                name: {
                    "target": "npu-a3",
                    "kind": "container",
                    "build": {
                        "dockerfile": "docker/npu/Dockerfile_aarch64",
                        "platform": "linux/arm64",
                        "revision": 1,
                        "ci_build_args": {
                            "GIT_HTTP_USER_AGENT": "git/2.56.1",
                        },
                    },
                    "publish": {
                        "ref": "registry.example/team/image:cann9.0.0-a3-base",
                    },
                },
            },
        },
    }


def _write_repo(root: Path, name: str = "npu-a3-cann90") -> Path:
    config = root / "scripts/ci/config/ci.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump(_config(name), sort_keys=False),
        encoding="utf-8",
    )
    context = root / "docker/npu"
    context.mkdir(parents=True)
    (context / "Dockerfile_aarch64").write_text(
        "FROM base:one\n", encoding="utf-8"
    )
    return config


def test_build_hash_tracks_only_dockerfile_platform_and_revision(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    first = image_plan(tmp_path, "npu-a3-cann90")

    (tmp_path / "docker/npu/setup.sh").write_text("echo ignored\n", encoding="utf-8")
    assert image_plan(tmp_path, "npu-a3-cann90").build_input_hash == first.build_input_hash

    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    image = document["resources"]["images"]["npu-a3-cann90"]
    image["build"]["ci_build_args"]["PIP_INDEX_URL"] = "https://example.invalid"
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert image_plan(tmp_path, "npu-a3-cann90").build_input_hash == first.build_input_hash

    image["build"]["revision"] = 2
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    revision_change = image_plan(tmp_path, "npu-a3-cann90")
    assert revision_change.build_input_hash != first.build_input_hash

    image["build"]["platform"] = "linux/amd64"
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert image_plan(tmp_path, "npu-a3-cann90").build_input_hash != (
        revision_change.build_input_hash
    )

    (tmp_path / "docker/npu/Dockerfile_aarch64").write_text(
        "FROM base:two\n", encoding="utf-8"
    )
    assert image_plan(tmp_path, "npu-a3-cann90").build_input_hash != (
        revision_change.build_input_hash
    )


def test_build_hash_is_a_full_sha256_and_context_is_derived(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    plan = image_plan(tmp_path, "npu-a3-cann90")

    assert plan.build_input_hash.startswith("sha256:")
    assert len(plan.build_input_hash) == len("sha256:") + 64
    assert plan.spec.context == "docker/npu"
    assert plan.candidate_tag.startswith("cann9.0.0-a3-base-")
    assert plan.candidate_ref.startswith("registry.example/team/image:cann9.0.0-a3-base-")


def test_publish_alias_does_not_change_the_build_hash(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    first = image_plan(tmp_path, "npu-a3-cann90")
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-a3-cann90"]["publish"]["ref"] = (
        "registry.example/team/image:cann9.0.0-a3-next"
    )
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    changed = image_plan(tmp_path, "npu-a3-cann90")
    assert changed.build_input_hash == first.build_input_hash
    assert changed.candidate_ref != first.candidate_ref


def test_one_dockerfile_cannot_map_to_two_images(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-a3-cann92"] = {
        "target": "npu-a3",
        "kind": "container",
        "build": {
            "dockerfile": "docker/npu/Dockerfile_aarch64",
            "platform": "linux/arm64",
        },
        "publish": {
            "ref": "registry.example/team/image:cann9.2.0-a3-base",
        },
    }
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ImageError, match="maps to both"):
        load_specs(tmp_path)


def test_dockerfile_must_stay_under_the_image_trigger_root(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-a3-cann90"]["build"]["dockerfile"] = (
        "scripts/Dockerfile_bad"
    )
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ImageError, match="docker/\\*\\*/Dockerfile"):
        load_specs(tmp_path)


def test_unknown_container_recipe_fields_are_rejected(tmp_path: Path) -> None:
    config = _write_repo(tmp_path)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["resources"]["images"]["npu-a3-cann90"]["build"]["context"] = "docker/npu"
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ImageError, match="unknown field"):
        load_specs(tmp_path)


def test_named_image_selection_and_cli_matrix(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    plans = image_plans(tmp_path, names="npu-a3-cann90")
    assert [plan.image_id for plan in plans] == ["npu-a3-cann90"]

    output = tmp_path / "matrix.json"
    github_output = tmp_path / "github-output"
    assert main([
        "plan",
        "--root", str(tmp_path),
        "--images", "all",
        "--output", str(output),
        "--github-output", str(github_output),
    ]) == 0
    assert '"image_id": "npu-a3-cann90"' in output.read_text(encoding="utf-8")
    assert "image_count=1" in github_output.read_text(encoding="utf-8")


def test_split_reference_supports_registry_ports() -> None:
    assert split_reference("registry.example:5000/team/image:tag") == (
        "registry.example:5000/team/image", "tag"
    )
    with pytest.raises(ImageError, match="explicit tag"):
        split_reference("registry.example/team/image")
