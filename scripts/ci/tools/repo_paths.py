"""Normalize and classify repository-relative paths for CI."""

from __future__ import annotations

from pathlib import PurePosixPath

DOC_DIRS = frozenset({"docs"})
DOC_NAMES = frozenset({"LICENSE", "NOTICE"})
DOC_SUFFIXES = frozenset({".adoc", ".md", ".rst"})
IMAGE_ROOT = "docker"


def normalize_repo_path(path: str) -> str:
    """Return a repository-relative path with POSIX separators."""
    return path.strip().replace("\\", "/").removeprefix("./")


def _parsed_path(path: str) -> PurePosixPath | None:
    normalized = normalize_repo_path(path)
    return PurePosixPath(normalized) if normalized else None


def is_docs_path(path: str) -> bool:
    """Return whether one repository-relative path is documentation-only."""
    parsed = _parsed_path(path)
    if parsed is None:
        return False
    in_docs = bool(parsed.parts) and parsed.parts[0] in DOC_DIRS
    return in_docs or parsed.name in DOC_NAMES or parsed.suffix.lower() in DOC_SUFFIXES


def is_dockerfile_path(path: str) -> bool:
    """Return whether one path is a Dockerfile that triggers an image build."""
    parsed = _parsed_path(path)
    if parsed is None or not parsed.parts:
        return False
    return parsed.parts[0] == IMAGE_ROOT and parsed.name.startswith("Dockerfile")
