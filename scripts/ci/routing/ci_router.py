#!/usr/bin/env python3
"""Route a changed-file list to the CI domains that must run."""

from __future__ import annotations

import argparse
import sys
from enum import Enum
from pathlib import Path
from typing import Iterable

from ..tools.repo_paths import is_dockerfile_path, is_docs_path


class ChangeRoute(str, Enum):
    """Execution route selected from repository changes."""

    CODE = "code"
    DOCS_ONLY = "docs_only"
    IMAGE_ONLY = "image_only"
    MIXED = "mixed"


def route_changes(paths: Iterable[str]) -> ChangeRoute:
    """Select docs, code, image-only or mixed routing for changed paths."""
    values = [path.strip() for path in paths if path.strip()]
    if not values:
        return ChangeRoute.CODE

    has_image = False
    has_code = False
    for value in values:
        if is_dockerfile_path(value):
            has_image = True
        elif not is_docs_path(value):
            has_code = True

    if has_image:
        return ChangeRoute.MIXED if has_code else ChangeRoute.IMAGE_ONLY
    return ChangeRoute.CODE if has_code else ChangeRoute.DOCS_ONLY


def _read_paths(path: str) -> list[str]:
    if path == "-":
        return sys.stdin.read().splitlines()
    return Path(path).read_text(encoding="utf-8").splitlines()


def main(argv: list[str] | None = None) -> int:
    """Print one routing decision for workflow consumption."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-files",
        default="-",
        help="newline-separated changed paths; '-' reads stdin",
    )
    args = parser.parse_args(argv)
    try:
        paths = _read_paths(args.changed_files)
    except OSError as exc:
        print(f"cannot read changed-file list: {exc}", file=sys.stderr)
        return 2
    print(route_changes(paths).value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
