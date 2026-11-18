"""Deterministic image identity derived from image metadata and build inputs."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..contracts.artifacts import canonical_json

IMAGE_SCHEMA_VERSION = 1
HASH_LENGTH = 12
POLICIES = ("auto", "resolve_only", "resolve_candidate", "build_if_missing", "blocked")
BUILDER_SOURCE_ROOT = Path("scripts/ci/image")
API_CONFIG = Path("scripts/ci/config/ci.yml")
IGNORED_PARTS = {"__pycache__"}


class ImageError(ValueError):
    """Raised when image metadata or build inputs are invalid."""


@dataclass(frozen=True)
class ImageSpec:
    name: str
    target: str
    hardware: str
    ref: str
    repository: str
    alias: str
    dockerfile: str
    context: str
    platform: str
    cann_version: str
    revision: int
    build_args: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ImagePlan:
    schema_version: int
    logical_image: str
    target: str
    hardware: str
    repository: str
    alias: str
    cann_version: str
    platform: str
    dockerfile: str
    context: str
    build_args: dict[str, str]
    revision: int
    content_hash: str
    content_tag: str
    head_hash: str
    base_hash: str | None
    image_changed: bool | None
    action: str

    @property
    def content_ref(self) -> str:
        return f"{self.repository}:{self.content_tag}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "logical_image": self.logical_image,
            "target": self.target,
            "hardware": self.hardware,
            "repository": self.repository,
            "alias": self.alias,
            "cann_version": self.cann_version,
            "platform": self.platform,
            "dockerfile": self.dockerfile,
            "context": self.context,
            "build_args": dict(sorted(self.build_args.items())),
            "revision": self.revision,
            "content_hash": self.content_hash,
            "content_tag": self.content_tag,
            "content_ref": self.content_ref,
            "head_hash": self.head_hash,
            "base_hash": self.base_hash,
            "image_changed": self.image_changed,
            "action": self.action,
        }


def split_reference(ref: str) -> tuple[str, str]:
    """Split ``registry/repository:tag`` without breaking registry ports."""
    if "@" in ref:
        raise ImageError("image build ref must be a tag reference, not a digest")
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon <= slash:
        raise ImageError(f"image ref needs an explicit tag: {ref!r}")
    repository, alias = ref[:colon], ref[colon + 1 :]
    if not repository or not alias:
        raise ImageError(f"invalid image ref: {ref!r}")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", alias):
        raise ImageError(f"invalid image tag in ref: {alias!r}")
    return repository, alias


def _relative(root: Path, value: str, field_name: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ImageError(f"{field_name} must be a repository-relative path: {value!r}")
    return root / path


def _read_worktree_file(root: Path, relative: str) -> bytes:
    try:
        return (root / relative).read_bytes()
    except OSError as exc:
        raise ImageError(f"cannot read image input {relative}: {exc}") from exc


def _worktree_files(root: Path, spec: ImageSpec) -> dict[str, str]:
    context = _relative(root, spec.context, "context")
    dockerfile = _relative(root, spec.dockerfile, "dockerfile")
    if not context.is_dir():
        raise ImageError(f"image context is not a directory: {spec.context}")
    if not dockerfile.is_file():
        raise ImageError(f"dockerfile does not exist: {spec.dockerfile}")
    paths = {
        path.relative_to(root).as_posix()
        for path in context.rglob("*")
        if path.is_file() and not (set(path.parts) & IGNORED_PARTS)
    }
    paths.add(dockerfile.relative_to(root).as_posix())
    builder = root / BUILDER_SOURCE_ROOT
    if builder.is_dir():
        paths.update(
            path.relative_to(root).as_posix()
            for path in builder.glob("*.py")
            if path.is_file()
        )
    return {
        path: hashlib.sha256(_read_worktree_file(root, path)).hexdigest()
        for path in sorted(paths)
    }


def _git(root: Path, *args: str) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise ImageError(f"git {' '.join(args)} failed: {detail}")
    return process.stdout


def _git_names(root: Path, sha: str, relative: str) -> list[str]:
    output = _git(root, "ls-tree", "-r", "-z", "--name-only", sha, "--", relative)
    return sorted(
        value.decode("utf-8")
        for value in output.split(b"\0")
        if value
    )


def _git_file(root: Path, sha: str, relative: str) -> bytes:
    return _git(root, "show", f"{sha}:{relative}")


def _git_files(root: Path, sha: str, spec: ImageSpec) -> dict[str, str]:
    paths = set(_git_names(root, sha, spec.context))
    paths.add(spec.dockerfile)
    paths.update(_git_names(root, sha, BUILDER_SOURCE_ROOT.as_posix()))
    paths = {
        path for path in paths
        if path.endswith(".py") or path.startswith(spec.context.rstrip("/") + "/")
        or path == spec.dockerfile
    }
    if spec.dockerfile not in paths:
        raise ImageError(f"dockerfile is absent at {sha}: {spec.dockerfile}")
    return {
        path: hashlib.sha256(_git_file(root, sha, path)).hexdigest()
        for path in sorted(paths)
    }


def _hash_from_files(
    spec: ImageSpec,
    files: Mapping[str, str],
) -> str:
    payload = {
        "schema": IMAGE_SCHEMA_VERSION,
        "logical_image": spec.name,
        "target": spec.target,
        "hardware": spec.hardware,
        "platform": spec.platform,
        "cann_version": spec.cann_version,
        "revision": spec.revision,
        "files": dict(sorted(files.items())),
        "build_args": dict(sorted(spec.build_args.items())),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:HASH_LENGTH]


def content_tag(spec: ImageSpec, content_hash: str) -> str:
    return f"cann{spec.cann_version}-{spec.hardware}-c{content_hash}"


def _load_yaml(root: Path, path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load((root / path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ImageError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ImageError(f"{label} must contain a mapping")
    return value


def _ci_document(root: Path, revision: str | None) -> tuple[dict[str, Any], str]:
    if revision is None:
        return _load_yaml(root, API_CONFIG, API_CONFIG.as_posix()), "worktree"
    try:
        document = yaml.safe_load(_git_file(root, revision, API_CONFIG.as_posix()))
    except (ImageError, ValueError, yaml.YAMLError) as exc:
        raise ImageError(f"cannot read {API_CONFIG} at {revision}: {exc}") from exc
    if not isinstance(document, dict):
        raise ImageError(f"{API_CONFIG} at {revision} must contain a mapping")
    return document, f"revision {revision}"


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


def _spec_from_documents(
    image_name: str,
    ci_document: Mapping[str, Any],
    source: str,
) -> ImageSpec:
    resources = _mapping(ci_document.get("resources"), f"{source}: resources")
    images = _mapping(resources.get("images"), f"{source}: resources.images")
    targets = _mapping(resources.get("targets"), f"{source}: resources.targets")
    image = _mapping(images.get(image_name), f"{source}: image {image_name!r}")
    if image.get("kind") != "container":
        raise ImageError(f"image {image_name!r} is not a container image")
    target_name = _text(image.get("target"), f"{source}: images.{image_name}.target")
    target = _mapping(targets.get(target_name), f"{source}: target {target_name!r}")
    hardware = _text(target.get("hardware"), f"{source}: targets.{target_name}.hardware")
    ref = _text(image.get("ref"), f"{source}: images.{image_name}.ref")
    repository, alias = split_reference(ref)

    build = _mapping(
        image.get("build"),
        f"{source}: image build {image_name!r}",
    )
    args_raw = _mapping(build.get("args") or {}, f"{source}: images.{image_name}.args")
    args: dict[str, str] = {}
    for key, value in args_raw.items():
        if isinstance(value, (dict, list)) or value is None:
            raise ImageError(f"{source}: images.{image_name}.args.{key} must be a scalar")
        args[str(key)] = str(value)
    cann_version = _text(
        build.get("cann_version"),
        f"images.{image_name}.cann_version",
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cann_version):
        raise ImageError(
            f"images.{image_name}.cann_version must be Docker-tag-safe"
        )
    alias_prefix = f"cann{cann_version}-{hardware}-"
    if not alias.startswith(alias_prefix):
        raise ImageError(
            f"images.{image_name}.ref alias must start with {alias_prefix!r} "
            "so multiple CANN versions remain addressable"
        )
    return ImageSpec(
        name=image_name,
        target=target_name,
        hardware=hardware,
        ref=ref,
        repository=repository,
        alias=alias,
        dockerfile=_text(build.get("dockerfile"), f"images.{image_name}.dockerfile"),
        context=_text(build.get("context"), f"images.{image_name}.context"),
        platform=_text(build.get("platform"), f"images.{image_name}.platform"),
        cann_version=cann_version,
        revision=_integer(build.get("revision"), f"images.{image_name}.revision", 1),
        build_args=args,
    )


def load_spec(root: Path, image_name: str, revision: str | None = None) -> ImageSpec:
    ci_document, source = _ci_document(root, revision)
    return _spec_from_documents(image_name, ci_document, source)


def container_image_names(root: Path, revision: str | None = None) -> list[str]:
    ci_document, source = _ci_document(root, revision)
    resources = _mapping(ci_document.get("resources"), f"{source}: resources")
    images = _mapping(resources.get("images"), f"{source}: resources.images")
    return sorted(
        name
        for name, value in images.items()
        if isinstance(value, dict) and value.get("kind") == "container"
    )


def _base_spec(root: Path, sha: str, image_name: str) -> ImageSpec | None:
    try:
        return load_spec(root, image_name, revision=sha)
    except (ImageError, ValueError, yaml.YAMLError):
        return None


def plan_image(
    root: Path,
    image_name: str,
    *,
    revision: str | None = None,
    base_sha: str | None = None,
    policy: str = "auto",
    build_allowed: bool = True,
) -> ImagePlan:
    if policy not in POLICIES:
        raise ImageError(f"unknown image policy {policy!r}")
    spec = load_spec(root, image_name, revision=revision)
    head_files = (
        _worktree_files(root, spec)
        if revision is None
        else _git_files(root, revision, spec)
    )
    head_hash = _hash_from_files(spec, head_files)
    base_hash: str | None = None
    image_changed: bool | None = None
    if base_sha:
        base_spec = _base_spec(root, base_sha, image_name)
        if base_spec is None:
            image_changed = True
        else:
            base_hash = _hash_from_files(base_spec, _git_files(root, base_sha, base_spec))
            image_changed = base_hash != head_hash
    if policy == "blocked":
        action = "blocked"
    elif policy == "resolve_only":
        action = "resolve"
    elif policy == "resolve_candidate":
        action = "resolve_candidate"
    elif policy == "build_if_missing" or image_changed is True:
        action = "build_if_missing"
    else:
        action = "resolve"
    if action == "build_if_missing" and not build_allowed:
        action = "blocked"
    return ImagePlan(
        schema_version=IMAGE_SCHEMA_VERSION,
        logical_image=spec.name,
        target=spec.target,
        hardware=spec.hardware,
        repository=spec.repository,
        alias=spec.alias,
        cann_version=spec.cann_version,
        platform=spec.platform,
        dockerfile=spec.dockerfile,
        context=spec.context,
        build_args=dict(sorted(spec.build_args.items())),
        revision=spec.revision,
        content_hash=head_hash,
        content_tag=content_tag(spec, head_hash),
        head_hash=head_hash,
        base_hash=base_hash,
        image_changed=image_changed,
        action=action,
    )
