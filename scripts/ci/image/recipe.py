"""Load container image definitions from the single CI configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..tools.repo_paths import is_dockerfile_path, normalize_repo_path

CI_CONFIG_PATH = Path("scripts/ci/config/ci.yml")
SCHEMA_VERSION = 5


class ImageError(ValueError):
    """Raised when image configuration or identity inputs are invalid."""


@dataclass(frozen=True)
class ImageSpec:
    image_id: str
    target: str
    device: str
    hardware: str
    repository: str
    alias: str
    dockerfile: str
    platform: str
    revision: int = 1
    ci_build_args: Mapping[str, str] = field(default_factory=dict)

    @property
    def context(self) -> str:
        return Path(self.dockerfile).parent.as_posix()

    @property
    def publish_ref(self) -> str:
        return f"{self.repository}:{self.alias}"

    def matrix_item(self) -> dict[str, str]:
        return {
            "image_id": self.image_id,
            "target": self.target,
            "device": self.device,
            "hardware": self.hardware,
        }


def split_reference(ref: str) -> tuple[str, str]:
    """Split ``registry/repository:tag`` without breaking registry ports."""
    if "@" in ref:
        raise ImageError("image reference must be a tag reference, not a digest")
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon <= slash:
        raise ImageError(f"image reference needs an explicit tag: {ref!r}")
    repository, alias = ref[:colon], ref[colon + 1 :]
    if not repository or not alias:
        raise ImageError(f"invalid image reference: {ref!r}")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", alias):
        raise ImageError(f"invalid image tag in reference: {alias!r}")
    return repository, alias


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImageError(f"{where} must be a mapping")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ImageError(f"{where} must be a non-empty string")
    return value


def _revision(value: Any, where: str) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ImageError(f"{where} must be a positive integer")
    return value


def _build_args(value: Any, where: str) -> dict[str, str]:
    rows = _mapping(value or {}, where)
    result: dict[str, str] = {}
    for key, item in rows.items():
        if isinstance(item, (dict, list)) or item is None:
            raise ImageError(f"{where}.{key} must be a scalar")
        result[str(key)] = str(item)
    return result


def _dockerfile(value: Any, where: str) -> str:
    path = normalize_repo_path(_text(value, where))
    if not is_dockerfile_path(path):
        raise ImageError(
            f"{where} must be docker/**/Dockerfile* so image CI can trigger it: "
            f"{path!r}"
        )
    return path


def _unknown_keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ImageError(f"{where} has unknown field(s): {', '.join(unknown)}")


def _spec_from(
    root: Path,
    image_id: str,
    image: Mapping[str, Any],
    targets: Mapping[str, Any],
) -> ImageSpec:
    where = f"resources.images.{image_id}"
    _unknown_keys(image, {"target", "kind", "ref", "build", "publish"}, where)
    target_name = _text(image.get("target"), f"{where}.target")
    target = _mapping(targets.get(target_name), f"resources.targets.{target_name}")
    build = _mapping(image.get("build"), f"{where}.build")
    _unknown_keys(
        build,
        {"dockerfile", "platform", "revision", "ci_build_args"},
        f"{where}.build",
    )
    publish = _mapping(image.get("publish"), f"{where}.publish")
    _unknown_keys(publish, {"ref"}, f"{where}.publish")
    if image.get("ref"):
        raise ImageError(f"{where}.ref is only valid for host images")

    repository, alias = split_reference(_text(publish.get("ref"), f"{where}.publish.ref"))
    dockerfile = _dockerfile(build.get("dockerfile"), f"{where}.build.dockerfile")
    if not (root / dockerfile).is_file():
        raise ImageError(f"{where}.build.dockerfile is missing: {dockerfile}")
    return ImageSpec(
        image_id=image_id,
        target=target_name,
        device=_text(target.get("device"), f"resources.targets.{target_name}.device"),
        hardware=str(target.get("hardware") or ""),
        repository=repository,
        alias=alias,
        dockerfile=dockerfile,
        platform=_text(build.get("platform"), f"{where}.build.platform"),
        revision=_revision(build.get("revision"), f"{where}.build.revision"),
        ci_build_args=_build_args(
            build.get("ci_build_args"), f"{where}.build.ci_build_args"
        ),
    )


def load_specs(root: Path) -> list[ImageSpec]:
    """Load all container images and reject ambiguous Dockerfile mappings."""
    try:
        document = yaml.safe_load(
            (root / CI_CONFIG_PATH).read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError) as exc:
        raise ImageError(f"cannot read {CI_CONFIG_PATH}: {exc}") from exc
    config = _mapping(document, "ci")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ImageError(f"schema_version must be {SCHEMA_VERSION}")
    resources = _mapping(config.get("resources"), "resources")
    targets = _mapping(resources.get("targets"), "resources.targets")
    images = _mapping(resources.get("images"), "resources.images")

    specs: list[ImageSpec] = []
    by_dockerfile: dict[str, str] = {}
    for image_id, value in images.items():
        image = _mapping(value, f"resources.images.{image_id}")
        kind = image.get("kind")
        if kind == "host":
            if image.get("build") is not None or image.get("publish") is not None:
                raise ImageError(
                    f"resources.images.{image_id} has build/publish for a host image"
                )
            continue
        if kind != "container":
            raise ImageError(
                f"resources.images.{image_id}.kind must be host or container"
            )
        spec = _spec_from(root, image_id, image, targets)
        previous = by_dockerfile.get(spec.dockerfile)
        if previous is not None:
            raise ImageError(
                f"dockerfile {spec.dockerfile!r} maps to both {previous!r} and "
                f"{image_id!r}"
            )
        by_dockerfile[spec.dockerfile] = image_id
        specs.append(spec)
    return sorted(specs, key=lambda item: item.image_id)
