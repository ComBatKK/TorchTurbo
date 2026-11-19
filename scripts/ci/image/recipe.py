"""Image recipe loading and validation.

This module is the only place that interprets the ``recipe`` object stored in
``scripts/ci/config/ci.yml``. Other image modules consume ``ImageSpec`` values
and do not parse the YAML document themselves.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

CI_CONFIG_PATH = Path("scripts/ci/config/ci.yml")
BUILD_INPUT_ROOT = "docker"


class ImageError(ValueError):
    """Raised when image configuration or identity inputs are invalid."""


class ImageConfigMissing(ImageError):
    """Raised when a Git revision predates the image recipe configuration."""


@dataclass(frozen=True)
class ImageSpec:
    name: str
    target: str
    device: str
    hardware: str
    repository: str
    alias: str
    dockerfile: str
    context: str
    platform: str
    cann_version: str
    build_args: Mapping[str, str] = field(default_factory=dict)
    revision: int = 1

    def matrix_item(self) -> dict[str, str]:
        return {
            "image": self.name,
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


def _integer(value: Any, where: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ImageError(f"{where} must be a positive integer")
    return value


def _relative(value: Any, where: str) -> str:
    text = _text(value, where)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ImageError(f"{where} must be a repository-relative path: {text!r}")
    return path.as_posix()


def _build_input(value: Any, where: str) -> str:
    path = _relative(value, where)
    if not path.startswith(BUILD_INPUT_ROOT + "/"):
        raise ImageError(
            f"{where} must be under {BUILD_INPUT_ROOT}/ so image CI triggers cover it: "
            f"{path!r}"
        )
    return path


def _git(root: Path, *args: str) -> bytes:
    process = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise ImageError(f"git {' '.join(args)} failed: {detail}")
    return process.stdout


def _git_has_file(root: Path, revision: str, relative: str) -> bool:
    process = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{revision}:{relative}"],
        capture_output=True,
    )
    return process.returncode == 0


def _git_names(root: Path, revision: str, relative: str) -> list[str]:
    output = _git(root, "ls-tree", "-r", "-z", "--name-only", revision, "--", relative)
    return sorted(value.decode("utf-8") for value in output.split(b"\0") if value)


def _read_file(root: Path, relative: str, revision: str | None) -> bytes:
    if revision is None:
        try:
            return (root / relative).read_bytes()
        except OSError as exc:
            raise ImageError(f"cannot read {relative}: {exc}") from exc
    try:
        return _git(root, "show", f"{revision}:{relative}")
    except ImageError as exc:
        raise ImageError(f"cannot read {relative} at {revision}: {exc}") from exc


def _load_yaml(
    root: Path,
    relative: Path,
    revision: str | None,
    label: str,
) -> dict[str, Any]:
    path = relative.as_posix()
    if revision is not None and not _git_has_file(root, revision, path):
        raise ImageConfigMissing(f"{path} is absent at revision {revision}")
    try:
        value = yaml.safe_load(_read_file(root, path, revision))
    except yaml.YAMLError as exc:
        raise ImageError(f"cannot parse {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ImageError(f"{label} must contain a mapping")
    return value


def _spec_from(
    name: str,
    target: str,
    device: str,
    hardware: str,
    recipe: Mapping[str, Any],
) -> ImageSpec:
    where = f"images.{name}"
    repository, alias = split_reference(_text(recipe.get("ref"), f"{where}.ref"))
    cann_version = _text(recipe.get("cann_version"), f"{where}.cann_version")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cann_version):
        raise ImageError(f"{where}.cann_version must be Docker-tag-safe")
    alias_prefix = f"cann{cann_version}-{hardware}-"
    if not alias.startswith(alias_prefix):
        raise ImageError(
            f"{where}.ref alias must start with {alias_prefix!r} so multiple "
            "CANN versions remain addressable"
        )
    args_raw = _mapping(recipe.get("build_args") or {}, f"{where}.build_args")
    build_args: dict[str, str] = {}
    for key, value in args_raw.items():
        if isinstance(value, (dict, list)) or value is None:
            raise ImageError(f"{where}.build_args.{key} must be a scalar")
        build_args[str(key)] = str(value)
    return ImageSpec(
        name=name,
        target=target,
        device=device,
        hardware=hardware,
        repository=repository,
        alias=alias,
        dockerfile=_build_input(recipe.get("dockerfile"), f"{where}.dockerfile"),
        context=_build_input(recipe.get("context"), f"{where}.context"),
        platform=_text(recipe.get("platform"), f"{where}.platform"),
        cann_version=cann_version,
        build_args=build_args,
        revision=_integer(recipe.get("revision"), f"{where}.revision", 1),
    )


def load_recipes(
    root: Path,
    revision: str | None = None,
) -> dict[str, Mapping[str, Any]]:
    """Load raw container recipes keyed by logical image name."""
    document = _load_yaml(root, CI_CONFIG_PATH, revision, "CI configuration")
    resources = _mapping(document.get("resources"), "resources")
    images = _mapping(resources.get("images"), "resources.images")
    recipes: dict[str, Mapping[str, Any]] = {}
    for name, value in images.items():
        image = _mapping(value, f"resources.images.{name}")
        if image.get("kind") != "container" or image.get("recipe") is None:
            continue
        recipes[name] = _mapping(
            image.get("recipe"), f"resources.images.{name}.recipe"
        )
    return recipes


def load_specs(root: Path, revision: str | None = None) -> list[ImageSpec]:
    """Load and cross-check logical images with their target and recipe data."""
    ci_document = _load_yaml(root, CI_CONFIG_PATH, revision, "CI configuration")
    resources = _mapping(ci_document.get("resources"), "resources")
    targets = _mapping(resources.get("targets"), "resources.targets")
    images = _mapping(resources.get("images"), "resources.images")

    specs: list[ImageSpec] = []
    for name, value in images.items():
        image = _mapping(value, f"resources.images.{name}")
        if image.get("kind") != "container":
            if image.get("recipe") is not None:
                raise ImageError(
                    f"resources.images.{name}.recipe is only valid for container images"
                )
            continue
        target_name = _text(image.get("target"), f"resources.images.{name}.target")
        target = _mapping(targets.get(target_name), f"resources.targets.{target_name}")
        recipe = _mapping(
            image.get("recipe"), f"resources.images.{name}.recipe"
        )
        specs.append(
            _spec_from(
                name,
                target_name,
                _text(target.get("device"), f"resources.targets.{target_name}.device"),
                _text(target.get("hardware"), f"resources.targets.{target_name}.hardware"),
                recipe,
            )
        )
    return sorted(specs, key=lambda item: item.name)


def validate_recipes(root: Path) -> None:
    """Validate the worktree recipe configuration and local build inputs."""
    for spec in load_specs(root):
        dockerfile = root / spec.dockerfile
        if not dockerfile.is_file():
            raise ImageError(f"dockerfile does not exist: {spec.dockerfile}")
        if not (root / spec.context).is_dir():
            raise ImageError(f"build context is not a directory: {spec.context}")
