#!/usr/bin/env python3
"""Classify a changed-file list for CI execution."""

from __future__ import annotations

import argparse
import sys
from enum import Enum
from typing import Iterable

from .cli import read_lines
from .repo_paths import is_dockerfile_path, is_docs_path


class ChangeKind(str, Enum):
    """Change kind selected from repository changes."""

    CODE = "code"
    DOCS_ONLY = "docs_only"
    IMAGE_ONLY = "image_only"
    MIXED = "mixed"


def classify_changes(paths: Iterable[str]) -> ChangeKind:
    """Select docs, code, image-only or mixed changes from paths."""
    values = [path.strip() for path in paths if path.strip()]
    if not values:
        return ChangeKind.CODE

    has_image = False
    has_code = False
    for value in values:
        if is_dockerfile_path(value):
            has_image = True
        elif not is_docs_path(value):
            has_code = True

    if has_image:
        return ChangeKind.MIXED if has_code else ChangeKind.IMAGE_ONLY
    return ChangeKind.CODE if has_code else ChangeKind.DOCS_ONLY


def main(argv: list[str] | None = None) -> int:
    """Print one change kind for workflow consumption."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-files",
        default="-",
        help="newline-separated changed paths; '-' reads stdin",
    )
    args = parser.parse_args(argv)
    try:
        paths = read_lines(args.changed_files)
    except OSError as exc:
        print(f"cannot read changed-file list: {exc}", file=sys.stderr)
        return 2
    print(classify_changes(paths).value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
