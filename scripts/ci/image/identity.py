"""Compute the build identity and candidate reference for a container image."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..tools.hashing import content_digest
from .recipe import ImageError, ImageSpec, load_specs
from .selection import select_named


@dataclass(frozen=True)
class ImagePlan:
    spec: ImageSpec
    build_input_hash: str

    @property
    def image_id(self) -> str:
        return self.spec.image_id

    @property
    def candidate_tag(self) -> str:
        suffix = f"-{self.build_input_hash.removeprefix('sha256:')[:12]}"
        return self.spec.alias[: 128 - len(suffix)] + suffix

    @property
    def candidate_ref(self) -> str:
        return f"{self.spec.repository}:{self.candidate_tag}"


def build_input_hash(root: Path, spec: ImageSpec) -> str:
    """Hash the Dockerfile, platform and manual revision of one image."""
    path = root / spec.dockerfile
    try:
        dockerfile = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImageError(f"cannot read dockerfile {spec.dockerfile}: {exc}") from exc
    return content_digest({
        "dockerfile": dockerfile,
        "platform": spec.platform,
        "revision": spec.revision,
    })


def plan_image(root: Path, spec: ImageSpec) -> ImagePlan:
    return ImagePlan(spec=spec, build_input_hash=build_input_hash(root, spec))


def image_plans(root: Path, *, names: str = "") -> list[ImagePlan]:
    """Return plans for all container images or a comma-separated selection."""
    specs = select_named(load_specs(root), names or "all")
    return [plan_image(root, spec) for spec in specs]


def image_plan(root: Path, image_id: str) -> ImagePlan:
    plans = image_plans(root, names=image_id)
    if len(plans) != 1:
        raise ImageError(f"expected one image plan for {image_id!r}")
    return plans[0]
