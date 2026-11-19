"""Compute immutable image identity from recipes and build inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..contracts.artifacts import canonical_json
from .recipe import (
    ImageError,
    ImageSpec,
    _git_has_file,
    _git_names,
    _read_file,
    load_specs,
)
from .selection import select_named

HASH_LENGTH = 12


@dataclass(frozen=True)
class ImagePlan:
    spec: ImageSpec
    content_tag: str

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def content_ref(self) -> str:
        return f"{self.spec.repository}:{self.content_tag}"


def _ignored(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        any(part in {".git", "__pycache__"} for part in parts)
        or relative.endswith((".pyc", ".pyo"))
        or relative.endswith(".DS_Store")
    )


def _identity_files(root: Path, spec: ImageSpec, revision: str | None) -> list[str]:
    paths = {spec.dockerfile}
    if revision is None:
        context = root / spec.context
        if not context.exists():
            raise ImageError(f"build context does not exist: {spec.context}")
        if context.is_dir():
            for path in context.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                if not _ignored(relative):
                    paths.add(relative)
        else:
            paths.add(spec.context)
    else:
        if not _git_has_file(root, revision, spec.context):
            raise ImageError(
                f"build context is absent at revision {revision}: {spec.context}"
            )
        paths.update(_git_names(root, revision, spec.context))
        if not _git_has_file(root, revision, spec.dockerfile):
            raise ImageError(
                f"dockerfile is absent at revision {revision}: {spec.dockerfile}"
            )
    return sorted(path for path in paths if not _ignored(path))


def identity_digest(root: Path, spec: ImageSpec, revision: str | None = None) -> str:
    """Hash every input that changes the built candidate."""
    files: dict[str, str] = {}
    for relative in _identity_files(root, spec, revision):
        files[relative] = hashlib.sha256(_read_file(root, relative, revision)).hexdigest()
    payload = {
        "schema_version": 1,
        "dockerfile": spec.dockerfile,
        "context": spec.context,
        "platform": spec.platform,
        "build_args": dict(sorted(spec.build_args.items())),
        "revision": spec.revision,
        "files": files,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:HASH_LENGTH]


def content_tag(root: Path, spec: ImageSpec, revision: str | None = None) -> str:
    """Return the immutable candidate tag for a recipe."""
    digest = identity_digest(root, spec, revision)
    return f"{spec.version_prefix}{spec.version}-{spec.hardware}-c{digest}"


def image_plans(
    root: Path,
    *,
    names: str = "",
    revision: str | None = None,
) -> list[ImagePlan]:
    """Build plans for selected images at a worktree or Git revision."""
    specs = load_specs(root, revision)
    selected = select_named(specs, names or "all")
    return [
        ImagePlan(spec=spec, content_tag=content_tag(root, spec, revision))
        for spec in selected
    ]


def image_plan(root: Path, name: str, *, revision: str | None = None) -> ImagePlan:
    """Build the single plan for a logical image name."""
    plans = image_plans(root, names=name, revision=revision)
    if len(plans) != 1:
        raise ImageError(f"expected one image plan for {name!r}")
    return plans[0]
