"""Deterministic content and file hashing for CI contracts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .json_io import canonical_json


def content_digest(value: object) -> str:
    """Return a sha256 digest over deterministic JSON content."""
    payload = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    """Return a sha256 digest over file bytes."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
