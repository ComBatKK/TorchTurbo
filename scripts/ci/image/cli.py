"""Command-line adapter for the image domain."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import repository_root
from ..tools.json_io import write_json
from .recipe import ImageError
from .release import (
    BuildError,
    check_envs,
    ensure_image,
    format_refs,
    promote_image,
    resolve_images,
)
from .registry import RegistryError
from .selection import SelectionError, matrix_payload, select_images


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repository_root()


def _read_paths(path: str) -> list[str]:
    if path == "-":
        return sys.stdin.read().splitlines()
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SelectionError(f"cannot read changed-file list: {exc}") from exc


def _write_lines(path: str, lines: list[str]) -> None:
    text = "".join(line + "\n" for line in lines)
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _write_json(path: str, value: object) -> None:
    if path:
        write_json(Path(path), value)
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _write_outputs(path: str, values: dict[str, object]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _image_refs(values: list[str]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for value in values:
        name, separator, ref = value.partition("=")
        if not separator or not name or not ref:
            raise ImageError(f"invalid --image-ref {value!r}; expected NAME=REF")
        refs[name] = ref
    return refs


def cmd_plan(args: argparse.Namespace) -> int:
    changed = _read_paths(args.changed_files) if args.changed_files else None
    specs = select_images(
        _root(args.root),
        names=args.images,
        changed_files=changed,
    )
    matrix = matrix_payload(specs)
    compact = json.dumps(matrix, ensure_ascii=False, separators=(",", ":"))
    _write_json(args.output, matrix)
    _write_outputs(
        args.github_output,
        {
            "image_jobs": compact,
            "image_count": len(specs),
            "images": ",".join(spec.image_id for spec in specs),
        },
    )
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    results = resolve_images(
        _root(args.root),
        names=args.images,
        overrides=_image_refs(args.image_ref),
    )
    _write_lines(args.output, format_refs(results).splitlines())
    _write_outputs(
        args.github_output,
        {
            "image_count": len(results),
            "images": ",".join(result.image_id for result in results),
        },
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    result = ensure_image(
        _root(args.root),
        args.image,
        should_push=not args.no_push,
        use_cache=not args.no_cache,
        builder=args.builder,
        jobs=args.max_jobs,
    )
    _write_json(args.output, result.to_dict())
    _write_outputs(
        args.github_output,
        {
            "ref": result.ref,
            "digest": result.digest,
            "built": str(result.built).lower(),
            "cache_hit": str(not result.built).lower(),
            "elapsed_seconds": result.elapsed_seconds,
            "image_id": result.image_id,
            "build_input_hash": result.build.build_input_hash,
        },
    )
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    result = promote_image(_root(args.root), args.image, digest=args.digest)
    _write_json(args.output, result.to_dict())
    _write_outputs(
        args.github_output,
        {
            "alias": result.alias,
            "ref": result.ref,
            "digest": result.digest,
            "promoted": "true",
        },
    )
    return 0


def cmd_check_envs(args: argparse.Namespace) -> int:
    _write_lines(args.output, check_envs())
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None)
    common.add_argument("--output", default="")
    common.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", parents=[common])
    plan.add_argument("--changed-files", default="")
    plan.add_argument("--images", default="")
    plan.set_defaults(handler=cmd_plan)

    resolve = subparsers.add_parser("resolve", parents=[common])
    resolve.add_argument("--images", default="")
    resolve.add_argument("--image-ref", action="append", default=[], metavar="NAME=REF")
    resolve.set_defaults(handler=cmd_resolve)

    build = subparsers.add_parser("build", parents=[common])
    build.add_argument("--image", required=True)
    build.add_argument("--max-jobs", default="auto")
    build.add_argument("--builder", default="")
    build.add_argument("--no-cache", action="store_true")
    build.add_argument("--no-push", action="store_true")
    build.set_defaults(handler=cmd_build)

    promote = subparsers.add_parser("promote", parents=[common])
    promote.add_argument("--image", required=True)
    promote.add_argument("--digest", default="")
    promote.set_defaults(handler=cmd_promote)

    check = subparsers.add_parser("check-envs", parents=[common])
    check.set_defaults(handler=cmd_check_envs)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ImageError, SelectionError, RegistryError, BuildError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
