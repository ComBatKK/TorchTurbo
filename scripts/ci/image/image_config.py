"""Load and validate image definitions from CI configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..tools.changed_files import is_dockerfile_path, normalize_repo_path

CI_CONFIG_PATH = Path("scripts/ci/config/ci.yml")
_IMAGE_TAG_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_CONTAINER_IMAGE_KEYS = {"target", "kind", "ref", "build", "publish"}
_HOST_IMAGE_KEYS = {"target", "kind", "ref"}
_BUILD_KEYS = {"dockerfile", "platform", "revision", "ci_build_args"}
_PUBLISH_KEYS = {"ref"}


class ImageError(ValueError):
    """Raised when image configuration is invalid."""


@dataclass(frozen=True)
class RuntimeImage:
    """Image metadata consumed by the execution domain."""

    image_id: str
    target: str
    kind: str
    ref: str = ""


@dataclass(frozen=True)
class ImageSpec:
    image_id: str
    target: str
    repository: str
    alias: str
    dockerfile: str
    platform: str
    revision: int = 1
    ci_build_args: Mapping[str, str] = field(default_factory=dict)

    @property
    def context(self) -> str:
        return Path(self.dockerfile).parent.as_posix()


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
    if not _IMAGE_TAG_PATTERN.fullmatch(alias):
        raise ImageError(f"invalid image tag in reference: {alias!r}")
    return repository, alias


def _as_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImageError(f"{where} must be a mapping")
    return value


def _as_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ImageError(f"{where} must be a non-empty string")
    return value


def _as_dockerfile(value: Any, where: str) -> str:
    path = normalize_repo_path(_as_text(value, where))
    if not is_dockerfile_path(path):
        raise ImageError(
            f"{where} must be docker/**/Dockerfile* so image CI can trigger it: "
            f"{path!r}"
        )
    return path


def _reject_unknown_keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ImageError(f"{where} has unknown field(s): {', '.join(unknown)}")


def _target_name(image: Mapping[str, Any], where: str, targets: Mapping[str, Any]) -> str:
    target = _as_text(image.get("target"), f"{where}.target")
    if not isinstance(targets.get(target), dict):
        raise ImageError(f"resources.targets.{target} must be a mapping")
    return target


def _image_spec(
    root: Path,
    image_id: str,
    image: Mapping[str, Any],
    targets: Mapping[str, Any],
) -> ImageSpec:
    where = f"resources.images.{image_id}"
    _reject_unknown_keys(image, _CONTAINER_IMAGE_KEYS, where)
    target_name = _target_name(image, where, targets)
    build_where = f"{where}.build"
    build = _as_mapping(image.get("build"), build_where)
    _reject_unknown_keys(build, _BUILD_KEYS, build_where)
    publish_where = f"{where}.publish"
    publish = _as_mapping(image.get("publish"), publish_where)
    _reject_unknown_keys(publish, _PUBLISH_KEYS, publish_where)
    if image.get("ref"):
        raise ImageError(f"{where}.ref is only valid for host images")

    repository, alias = split_reference(_as_text(publish.get("ref"), f"{publish_where}.ref"))
    dockerfile = _as_dockerfile(build.get("dockerfile"), f"{build_where}.dockerfile")
    if not (root / dockerfile).is_file():
        raise ImageError(f"{build_where}.dockerfile is missing: {dockerfile}")
    revision = build.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ImageError(f"{build_where}.revision must be a positive integer")
    build_args = _as_mapping(
        build.get("ci_build_args") or {}, f"{build_where}.ci_build_args"
    )
    return ImageSpec(
        image_id=image_id,
        target=target_name,
        repository=repository,
        alias=alias,
        dockerfile=dockerfile,
        platform=_as_text(build.get("platform"), f"{build_where}.platform"),
        revision=revision,
        ci_build_args={str(key): str(value) for key, value in build_args.items()},
    )


def _read_resources(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        document = yaml.safe_load((root / CI_CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ImageError(f"cannot read {CI_CONFIG_PATH}: {exc}") from exc
    config = _as_mapping(document, "ci")
    resources = _as_mapping(config.get("resources"), "resources")
    targets = _as_mapping(resources.get("targets"), "resources.targets")
    images = _as_mapping(resources.get("images"), "resources.images")
    return targets, images


def _load_entries(root: Path) -> list[tuple[RuntimeImage, ImageSpec | None]]:
    targets, images = _read_resources(root)
    entries: list[tuple[RuntimeImage, ImageSpec | None]] = []
    by_dockerfile: dict[str, str] = {}
    for image_id, raw_image in images.items():
        where = f"resources.images.{image_id}"
        image = _as_mapping(raw_image, where)
        if image.get("kind") == "host":
            _reject_unknown_keys(image, _HOST_IMAGE_KEYS, where)
            entries.append((RuntimeImage(
                image_id, _target_name(image, where, targets), "host",
                _as_text(image.get("ref"), f"{where}.ref"),
            ), None))
            continue
        if image.get("kind") != "container":
            raise ImageError(f"{where}.kind must be host or container")
        spec = _image_spec(root, image_id, image, targets)
        previous = by_dockerfile.get(spec.dockerfile)
        if previous is not None:
            raise ImageError(f"dockerfile {spec.dockerfile!r} maps to both {previous!r} and {image_id!r}")
        by_dockerfile[spec.dockerfile] = image_id
        entries.append((RuntimeImage(image_id, spec.target, "container"), spec))
    return entries


def load_specs(root: Path) -> list[ImageSpec]:
    """Load all container images and reject ambiguous Dockerfile mappings."""
    return sorted(
        (spec for _, spec in _load_entries(root) if spec is not None),
        key=lambda item: item.image_id,
    )


def load_runtime_images(root: Path) -> dict[str, RuntimeImage]:
    """Load runtime image metadata without exposing build settings."""
    return {runtime.image_id: runtime for runtime, _ in _load_entries(root)}


def load_image(root: Path, image_id: str) -> ImageSpec:
    """Load one container image by logical ID."""
    images = {spec.image_id: spec for spec in load_specs(root)}
    image = images.get(image_id)
    if image is None:
        raise ImageError(f"unknown image ID: {image_id!r}")
    return image
