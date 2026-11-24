"""Deterministic content and file hashing for CI contracts."""

from __future__ import annotations

import hashlib

from .json_io import canonical_json


def content_digest(value: object) -> str:
    """Return a sha256 digest over deterministic JSON content."""
    payload = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
