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
from .plan import ImageError, ImageSpec, POLICIES, load_specs, plan_image


class SelectionError(ValueError):
    """Raised when image selection cannot produce a valid execution matrix."""


@dataclass(frozen=True)
class ImageSelection:
    image: str
    target: str
    device: str
    hardware: str
    dockerfile: str

    def matrix_item(self) -> dict[str, str]:
        return {
            "image": self.image,
            "target": self.target,
            "device": self.device,
            "hardware": self.hardware,
            "dockerfile": self.dockerfile,
        }


def _normalized(path: str) -> str:
    value = path.strip().replace("\\", "/")
    return value[2:] if value.startswith("./") else value


def _selection(spec: ImageSpec) -> ImageSelection:
    return ImageSelection(
        image=spec.name,
        target=spec.target,
        device=spec.device,
        hardware=spec.hardware,
        dockerfile=spec.dockerfile,
    )


def _selected(specs: Iterable[ImageSpec]) -> list[ImageSelection]:
    return [_selection(spec) for spec in sorted(specs, key=lambda item: item.name)]


def changed_images(root: Path, paths: Iterable[str]) -> list[ImageSelection]:
    """Return images whose configured Dockerfile is in the changed-file set."""
    specs = load_specs(root)
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
    return _selected(matched.values())


def named_images(root: Path, value: str) -> list[ImageSelection]:
    """Select all container images or a comma/space separated logical image list."""
    specs = load_specs(root)
    if value.strip().lower() == "all":
        return _selected(specs)
    names = tuple(dict.fromkeys(
        part for part in re.split(r"[,\s]+", value.strip()) if part
    ))
    if not names:
        raise SelectionError("image selection is empty; use 'all' or image names")
    by_name = {spec.name: spec for spec in specs}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise SelectionError(f"unknown logical image(s): {', '.join(unknown)}")
    return _selected((by_name[name] for name in names))


def matrix_payload(selections: Iterable[ImageSelection]) -> dict[str, list[dict[str, str]]]:
    return {"include": [selection.matrix_item() for selection in selections]}


def planned_matrix(
    root: Path,
    selections: Iterable[ImageSelection],
    *,
    policy: str,
    base_sha: str = "",
) -> tuple[dict[str, list[dict[str, str]]], dict[str, object]]:
    """Attach one precomputed image plan to every selected matrix entry."""
    include: list[dict[str, str]] = []
    plans: list[dict[str, object]] = []
    for selection in selections:
        plan = plan_image(
            root,
            selection.image,
            base_sha=base_sha or None,
            policy=policy,
        )
        entry = plan.to_dict()
        plans.append(entry)
        item = selection.matrix_item()
        item.update({
            "image_mode": plan.action,
            "content_tag": plan.content_tag,
            "image_plan": json.dumps(
                entry,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        })
        include.append(item)
    return {"include": include}, {
        "schema_version": 1,
        "images": plans,
    }


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
    parser.add_argument("--plan-policy", choices=POLICIES, default="")
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--plans-output", default="")
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
    except (SelectionError, ImageError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    if args.plan_policy:
        matrix, plans = planned_matrix(
            root,
            selections,
            policy=args.plan_policy,
            base_sha=args.base_sha,
        )
        if args.plans_output:
            Path(args.plans_output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.plans_output).write_text(
                json.dumps(plans, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    else:
        matrix = matrix_payload(selections)
    compact = json.dumps(matrix, ensure_ascii=False, separators=(",", ":"))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(compact + "\n", encoding="utf-8")
    else:
        print(compact)
    _write_outputs(args.github_output, {
        "image_jobs": compact,
        "image_count": len(selections),
        "images": ",".join(item.image for item in selections),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
