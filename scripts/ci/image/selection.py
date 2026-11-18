#!/usr/bin/env python3
"""Select logical images from changed Dockerfiles or an explicit image list."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .. import repository_root
from .plan import ImageLane, ImageSpec, load_lanes, load_specs


class SelectionError(ValueError):
    """Raised when image selection cannot produce a valid execution matrix."""


@dataclass(frozen=True)
class ImageSelection:
    image: str
    target: str
    device: str
    hardware: str
    dockerfile: str
    smoke_runner_variable: str
    smoke_runner: str

    def matrix_item(self) -> dict[str, str]:
        return {
            "image": self.image,
            "target": self.target,
            "device": self.device,
            "hardware": self.hardware,
            "dockerfile": self.dockerfile,
            "smoke_runner_variable": self.smoke_runner_variable,
            "smoke_runner": self.smoke_runner,
        }


def _normalized(path: str) -> str:
    value = path.strip().replace("\\", "/")
    return value[2:] if value.startswith("./") else value


def _smoke_lane(spec: ImageSpec, lanes: dict[str, ImageLane]) -> ImageLane:
    candidates = [
        lane for lane in lanes.values()
        if lane.target == spec.target and lane.capacity_slots > 0
    ]
    if not candidates:
        raise SelectionError(f"image {spec.name!r} has no runner lane for target {spec.target!r}")
    return min(candidates, key=lambda lane: (lane.capacity_slots, lane.name))


def _selection(spec: ImageSpec, lanes: dict[str, ImageLane]) -> ImageSelection:
    lane = _smoke_lane(spec, lanes)
    return ImageSelection(
        image=spec.name,
        target=spec.target,
        device=spec.device,
        hardware=spec.hardware,
        dockerfile=spec.dockerfile,
        smoke_runner_variable=lane.runner_variable,
        smoke_runner=lane.runner,
    )


def _selected(specs: Iterable[ImageSpec], lanes: dict[str, ImageLane]) -> list[ImageSelection]:
    return [_selection(spec, lanes) for spec in sorted(specs, key=lambda item: item.name)]


def changed_images(root: Path, paths: Iterable[str]) -> list[ImageSelection]:
    """Return images whose configured Dockerfile is in the changed-file set."""
    specs = load_specs(root)
    lanes = load_lanes(root)
    by_dockerfile: dict[str, list[ImageSpec]] = {}
    for spec in specs:
        by_dockerfile.setdefault(spec.dockerfile, []).append(spec)

    matched: dict[str, ImageSpec] = {}
    unmatched: list[str] = []
    changed_dockerfiles = 0
    for value in paths:
        path = _normalized(value)
        if not path:
            continue
        selected = by_dockerfile.get(path, [])
        if selected:
            changed_dockerfiles += 1
            for spec in selected:
                matched[spec.name] = spec
        elif path.startswith("docker/") and Path(path).name.startswith("Dockerfile"):
            unmatched.append(path)
    if unmatched:
        raise SelectionError(
            "changed Dockerfiles are not mapped to resources.images.*.build.dockerfile: "
            + ", ".join(sorted(unmatched))
        )
    if not changed_dockerfiles:
        raise SelectionError("no configured Dockerfile changed; image pipeline has no work")
    return _selected(matched.values(), lanes)


def named_images(root: Path, value: str) -> list[ImageSelection]:
    """Select all container images or a comma/space separated logical image list."""
    specs = load_specs(root)
    lanes = load_lanes(root)
    if value.strip().lower() == "all":
        return _selected(specs, lanes)
    names = tuple(dict.fromkeys(
        part for part in re.split(r"[,\s]+", value.strip()) if part
    ))
    if not names:
        raise SelectionError("image selection is empty; use 'all' or image names")
    by_name = {spec.name: spec for spec in specs}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise SelectionError(f"unknown logical image(s): {', '.join(unknown)}")
    return _selected((by_name[name] for name in names), lanes)


def matrix_payload(selections: Iterable[ImageSelection]) -> dict[str, list[dict[str, str]]]:
    return {"include": [selection.matrix_item() for selection in selections]}


def _write_outputs(path: str, values: dict[str, object]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _read_paths(path: str) -> list[str]:
    if path == "-":
        return sys.stdin.read().splitlines()
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SelectionError(f"cannot read changed-file list: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None)
    parser.add_argument("--changed-files", default="", help="newline-separated paths; '-' reads stdin")
    parser.add_argument("--images", default="", help="'all' or comma/space separated image names")
    parser.add_argument("--output", default="")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    args = parser.parse_args(argv)
    if bool(args.changed_files) == bool(args.images):
        parser.error("select exactly one of --changed-files or --images")
    root = Path(args.root).resolve() if args.root else repository_root()
    try:
        if args.images:
            selections = named_images(root, args.images)
        else:
            selections = changed_images(root, _read_paths(args.changed_files))
    except SelectionError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    matrix = matrix_payload(selections)
    compact = json.dumps(matrix, ensure_ascii=False, separators=(",", ":"))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(compact + "\n", encoding="utf-8")
    else:
        print(compact)
    _write_outputs(args.github_output, {
        "matrix": compact,
        "image_count": len(selections),
        "images": ",".join(item.image for item in selections),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
