"""Command-line adapter for the image domain."""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..tools.cli import (
    append_github_output,
    read_lines,
    repository_path,
    write_json_output,
)
from .build_image import BuildError, get_or_build_image, promote_image
from .image_config import ImageError
from .select_images import ImageSelectionError, select_images
from .registry import RegistryError


def _read_paths(path: str) -> list[str]:
    try:
        return read_lines(path)
    except OSError as exc:
        raise ImageSelectionError(f"cannot read changed-file list: {exc}") from exc


def cmd_plan(args: argparse.Namespace) -> int:
    changed = _read_paths(args.changed_files) if args.changed_files else None
    specs = select_images(
        repository_path(args.root), image_ids=args.images, changed_files=changed
    )
    matrix = {
        "include": [
            {"image_id": spec.image_id, "target": spec.target} for spec in specs
        ]
    }
    write_json_output(args.output, matrix)
    append_github_output(
        {
            "image_jobs": json.dumps(matrix, ensure_ascii=False, separators=(",", ":")),
            "image_count": len(specs),
            "images": ",".join(spec.image_id for spec in specs),
        },
        args.github_output,
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    outcome = get_or_build_image(
        repository_path(args.root),
        args.image,
        use_cache=not args.no_cache,
        builder=args.builder,
        jobs=args.max_jobs,
    )
    build = outcome.build
    image = build.image
    write_json_output(args.output, outcome.to_dict())
    append_github_output(
        {
            "ref": image.ref,
            "digest": image.digest,
            "built": str(build.built).lower(),
            "cache_hit": str(not build.built).lower(),
            "elapsed_seconds": outcome.elapsed_seconds,
            "image_id": image.image_id,
            "content_hash": build.content_hash,
        },
        args.github_output,
    )
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    outcome = promote_image(repository_path(args.root), args.image)
    image = outcome.build.image
    write_json_output(args.output, outcome.to_dict())
    append_github_output(
        {
            "alias": outcome.alias,
            "ref": image.ref,
            "digest": image.digest,
            "promoted": "true",
        },
        args.github_output,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None)
    common.add_argument("--output", default="")
    common.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", parents=[common])
    plan.add_argument("--changed-files", default="")
    plan.add_argument("--images", default="")
    plan.set_defaults(handler=cmd_plan)

    build = commands.add_parser("build", parents=[common])
    build.add_argument("--image", required=True)
    build.add_argument("--max-jobs", default="auto")
    build.add_argument("--builder", default="")
    build.add_argument("--no-cache", action="store_true")
    build.set_defaults(handler=cmd_build)

    promote = commands.add_parser("promote", parents=[common])
    promote.add_argument("--image", required=True)
    promote.set_defaults(handler=cmd_promote)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ImageError, RegistryError, BuildError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
