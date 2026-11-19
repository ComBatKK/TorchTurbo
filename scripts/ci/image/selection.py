"""Select logical images from names or changed Dockerfiles."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

from .recipe import (
    ImageError,
    ImageSpec,
    load_specs,
)


class SelectionError(ImageError):
    """Raised when logical image selection cannot produce a valid matrix."""


def _selected(specs: Iterable[ImageSpec]) -> list[ImageSpec]:
    return sorted(specs, key=lambda item: item.name)


def select_named(specs: Sequence[ImageSpec], names: str) -> list[ImageSpec]:
    """Select loaded specs by name; ``all`` selects every spec."""
    if names.strip().lower() == "all":
        return _selected(specs)
    requested = tuple(
        dict.fromkeys(part for part in re.split(r"[,\s]+", names.strip()) if part)
    )
    if not requested:
        raise SelectionError("image selection is empty; use 'all' or image names")
    by_name = {spec.name: spec for spec in specs}
    unknown = [name for name in requested if name not in by_name]
    if unknown:
        raise SelectionError(f"unknown logical image(s): {', '.join(unknown)}")
    return _selected(by_name[name] for name in requested)


def _normalized(path: str) -> str:
    value = path.strip().replace("\\", "/")
    return value[2:] if value.startswith("./") else value


def _is_dockerfile(path: str) -> bool:
    parsed = Path(path)
    return parsed.parts[:1] == ("docker",) and parsed.name.startswith("Dockerfile")


def _changed(
    specs: Sequence[ImageSpec],
    paths: Iterable[str],
) -> list[ImageSpec]:
    by_dockerfile: dict[str, list[ImageSpec]] = {}
    for spec in specs:
        by_dockerfile.setdefault(spec.dockerfile, []).append(spec)
    matched: dict[str, ImageSpec] = {}
    unmatched: list[str] = []
    for value in paths:
        path = _normalized(value)
        if not path:
            continue
        selected = by_dockerfile.get(path, [])
        if selected:
            matched.update({spec.name: spec for spec in selected})
        elif _is_dockerfile(path):
            unmatched.append(path)
    if unmatched:
        raise SelectionError(
            "changed Dockerfiles are not mapped to resources.images.*.recipe.dockerfile "
            "in scripts/ci/config/ci.yml: "
            + ", ".join(sorted(unmatched))
        )
    if not matched:
        raise SelectionError("no configured Dockerfile changed; image pipeline has no work")
    return _selected(matched.values())


def select_images(
    root: Path,
    *,
    names: str = "",
    changed_files: Iterable[str] | None = None,
) -> list[ImageSpec]:
    """Select image recipes from a name list or a changed-file list."""
    if names and changed_files is not None:
        raise SelectionError("use either image names or changed files, not both")
    specs = load_specs(root)
    if names:
        return select_named(specs, names)
    if changed_files is None:
        raise SelectionError("image selection requires names or changed files")
    return _changed(specs, changed_files)


def matrix_payload(specs: Iterable[ImageSpec]) -> dict[str, list[dict[str, str]]]:
    """Return the public GitHub Actions matrix payload."""
    return {"include": [spec.matrix_item() for spec in specs]}
