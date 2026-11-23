from __future__ import annotations

import pytest

from scripts.ci.contracts.base import ContractError
from scripts.ci.contracts.image_contract import (
    ImageBinding,
    ImageBuildResult,
)

DIGEST = "sha256:" + "a" * 64
REF = f"registry.example/team/image@{DIGEST}"


def _container_binding() -> ImageBinding:
    return ImageBinding(
        image_id="npu-a3-cann90",
        target="npu-a3",
        kind="container",
        ref=REF,
    )


def test_container_binding_derives_digest_from_ref() -> None:
    binding = _container_binding()

    assert binding.digest == DIGEST
    assert binding.to_dict() == {
        "image_id": "npu-a3-cann90",
        "target": "npu-a3",
        "kind": "container",
        "ref": REF,
    }


def test_host_binding_has_no_digest() -> None:
    binding = ImageBinding(
        image_id="cpu-python311",
        target="cpu",
        kind="host",
        ref="python:3.11",
    )

    assert binding.digest == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_id", ""),
        ("target", " "),
        ("ref", ""),
        ("kind", "vm"),
        ("ref", "registry.example/team/image:cann90"),
        ("ref", "registry.example/team/image@sha256:short"),
    ],
)
def test_binding_rejects_invalid_fields(field: str, value: str) -> None:
    values = {
        "image_id": "npu-a3-cann90",
        "target": "npu-a3",
        "kind": "container",
        "ref": REF,
    }
    values[field] = value

    with pytest.raises(ContractError):
        ImageBinding(**values)


def test_build_result_serializes_nested_binding() -> None:
    result = ImageBuildResult(
        binding=_container_binding(),
        source_sha="e6c7a8a44ca4",
        build_input_hash=DIGEST,
        platform="linux/arm64",
        built=True,
    )

    assert result.to_dict()["binding"]["ref"] == REF
    assert result.to_dict()["build_input_hash"] == DIGEST


def test_build_result_requires_container_binding() -> None:
    host = ImageBinding(
        image_id="cpu-python311",
        target="cpu",
        kind="host",
        ref="python:3.11",
    )

    with pytest.raises(ContractError, match="container binding"):
        ImageBuildResult(
            binding=host,
            source_sha="e6c7a8a44ca4",
            build_input_hash=DIGEST,
            platform="linux/arm64",
            built=True,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha", ""),
        ("build_input_hash", "sha256:short"),
        ("platform", ""),
        ("built", 1),
    ],
)
def test_build_result_rejects_invalid_fields(field: str, value: object) -> None:
    values: dict[str, object] = {
        "binding": _container_binding(),
        "source_sha": "e6c7a8a44ca4",
        "build_input_hash": DIGEST,
        "platform": "linux/arm64",
        "built": True,
    }
    values[field] = value

    with pytest.raises(ContractError):
        ImageBuildResult(**values)  # type: ignore[arg-type]
