#!/usr/bin/env python3
"""Route a changed-file list to the CI domains that must run."""

from __future__ import annotations

import argparse
import sys
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Iterable

DOC_DIRS = frozenset({"docs"})
DOC_NAMES = frozenset({"LICENSE", "NOTICE"})
DOC_SUFFIXES = frozenset({".adoc", ".md", ".rst"})
IMAGE_ROOT = "docker"


class ChangeRoute(str, Enum):
    """Execution route selected from repository changes."""

    DOCS_ONLY = "docs_only"
    CODE = "code"
    IMAGE_ONLY = "image_only"
    MIXED = "mixed"


def _normalized(path: str) -> str:
    value = path.strip().replace("\\", "/")
    return value[2:] if value.startswith("./") else value


def is_docs_path(path: str) -> bool:
    """Return whether one repository-relative path is documentation-only."""
    normalized = _normalized(path)
    if not normalized:
        return False
    parsed = PurePosixPath(normalized)
    if parsed.parts and parsed.parts[0] in DOC_DIRS:
        return True
    if parsed.name in DOC_NAMES:
        return True
    return parsed.suffix.lower() in DOC_SUFFIXES


def is_dockerfile_path(path: str) -> bool:
    """Return whether one path is a Dockerfile that triggers an image build."""
    normalized = _normalized(path)
    if not normalized:
        return False
    parsed = PurePosixPath(normalized)
    return bool(
        parsed.parts
        and parsed.parts[0] == IMAGE_ROOT
        and parsed.name.startswith("Dockerfile")
    )


def route_changes(paths: Iterable[str]) -> ChangeRoute:
    """Select docs, code, image-only or mixed routing for changed paths."""
    values = [path.strip() for path in paths if path.strip()]
    if not values:
        return ChangeRoute.CODE

    dockerfiles = [path for path in values if is_dockerfile_path(path)]
    other_paths = [path for path in values if not is_dockerfile_path(path)]
    if dockerfiles:
        if other_paths and not all(is_docs_path(path) for path in other_paths):
            return ChangeRoute.MIXED
        return ChangeRoute.IMAGE_ONLY
    if all(is_docs_path(path) for path in values):
        return ChangeRoute.DOCS_ONLY
    return ChangeRoute.CODE


def _read_paths(path: str) -> list[str] | None:
    if path == "-":
        return sys.stdin.read().splitlines()
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"cannot read changed-file list: {exc}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    """Print one routing decision for workflow consumption."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-files",
        default="-",
        help="newline-separated changed paths; '-' reads stdin",
    )
    args = parser.parse_args(argv)
    paths = _read_paths(args.changed_files)
    route = ChangeRoute.CODE if paths is None else route_changes(paths)
    print(f"change_route={route.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
