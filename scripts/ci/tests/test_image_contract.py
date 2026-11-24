from __future__ import annotations

import pytest

from scripts.ci.contracts.base import ContractError
from scripts.ci.contracts.image_contract import (
    ResolvedImage,
    ImageBuildResult,
)

DIGEST = "sha256:" + "a" * 64
REF = f"registry.example/team/image@{DIGEST}"


def _container_image() -> ResolvedImage:
    return ResolvedImage(
        image_id="npu-a3-cann90",
        target="npu-a3",
        kind="container",
        ref=REF,
    )


def test_container_image_derives_digest_from_ref() -> None:
    image = _container_image()

    assert image.digest == DIGEST
    assert image.to_dict() == {
        "image_id": "npu-a3-cann90",
        "target": "npu-a3",
        "kind": "container",
        "ref": REF,
    }


def test_host_image_has_no_digest() -> None:
    image = ResolvedImage(
        image_id="cpu-python311",
        target="cpu",
        kind="host",
        ref="python:3.11",
    )

    assert image.digest == ""


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
def test_resolved_image_rejects_invalid_fields(field: str, value: str) -> None:
    values = {
        "image_id": "npu-a3-cann90",
        "target": "npu-a3",
        "kind": "container",
        "ref": REF,
    }
    values[field] = value

    with pytest.raises(ContractError):
        ResolvedImage(**values)


def test_build_result_serializes_nested_image() -> None:
    result = ImageBuildResult(
        image=_container_image(),
        content_hash=DIGEST,
        built=True,
        elapsed_seconds=7,
        alias="cann9.0.0-a3-base",
    )

    assert result.to_dict()["image"]["ref"] == REF
    assert result.to_dict()["content_hash"] == DIGEST
    assert result.to_dict()["elapsed_seconds"] == 7


def test_build_result_requires_container_image() -> None:
    host = ResolvedImage(
        image_id="cpu-python311",
        target="cpu",
        kind="host",
        ref="python:3.11",
    )

    with pytest.raises(ContractError, match="container image"):
        ImageBuildResult(
            image=host,
            content_hash=DIGEST,
            built=True,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content_hash", "sha256:short"),
        ("built", 1),
    ],
)
def test_build_result_rejects_invalid_fields(field: str, value: object) -> None:
    values: dict[str, object] = {
        "image": _container_image(),
        "content_hash": DIGEST,
        "built": True,
    }
    values[field] = value

    with pytest.raises(ContractError):
        ImageBuildResult(**values)  # type: ignore[arg-type]
