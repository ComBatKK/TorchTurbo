#!/usr/bin/env python3
"""Command line entry point for the CI planner."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

from ... import repository_root
from ...contracts.base import Diagnostic, GitSource
from ...contracts.planner_contract import CHANGE_MODES, PlannerOutput
from ...tools.json_io import canonical_json, write_json
from .config import CiConfigError, load as load_config
from .facts import FactsError, load as load_facts
from .matrix import github_outputs
from .planner import PlanError, planner_output_for, requested_images, source_for


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--root", default="")
    plan.add_argument("--scope", default="pull_request")
    plan.add_argument("--change-mode", choices=CHANGE_MODES, default="code")
    plan.add_argument("--output", default="")
    plan.add_argument("--image-ref", action="append", default=[], metavar="NAME=REF")
    plan.add_argument("--image", default="")
    plan.add_argument("--target-job", default="")
    plan.add_argument("--targets", default="")

    images = commands.add_parser("images")
    images.add_argument("--root", default="")
    images.add_argument("--scope", default="pull_request")
    images.add_argument("--image", default="")
    return parser


def _root(value: str) -> Path:
    return Path(value).resolve() if value else repository_root()


def _image_refs(values: list[str]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for value in values:
        name, separator, ref = value.partition("=")
        if not separator or not name or not ref:
            raise ValueError(f"bad --image-ref {value!r}; expected NAME=REF")
        refs[name] = ref
    return refs


def _source(root: Path) -> GitSource:
    return source_for(
        root,
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        ref=os.environ.get("CI_SOURCE_REF") or os.environ.get("GITHUB_REF", ""),
        sha=os.environ.get("CI_SOURCE_SHA", ""),
    )


def _print_diagnostics(issues: Iterable[Diagnostic]) -> None:
    for issue in issues:
        print(f"{issue.location}: {issue.message}", file=sys.stderr)


def _append_github_output(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _write_output(args: argparse.Namespace, output: PlannerOutput) -> None:
    if args.output:
        write_json(Path(args.output), output.to_dict())


def _fail_plan(
    args: argparse.Namespace, source: GitSource, issues: Iterable[Diagnostic]
) -> int:
    output = planner_output_for(
        None,
        None,
        ci_type=args.scope,
        change_mode=args.change_mode,
        git_source=source,
        issues=issues,
    )
    _write_output(args, output)
    _print_diagnostics(output.errors)
    return 1


def _images(args: argparse.Namespace) -> int:
    config = load_config(_root(args.root))
    images = ",".join(
        requested_images(config, args.scope, image_override=args.image)
    )
    _append_github_output({"images": images})
    print(images)
    return 0


def _plan(args: argparse.Namespace, source: GitSource) -> int:
    if args.target_job and args.targets:
        raise PlanError([Diagnostic(
            code="invalid-selection",
            location="selection",
            message="--target-job cannot be combined with --targets",
        )])
    root = _root(args.root)
    config = load_config(root, image_refs=_image_refs(args.image_ref))
    facts = load_facts(root, config)
    output = planner_output_for(
        config,
        facts,
        ci_type=args.scope,
        change_mode=args.change_mode,
        git_source=source,
        target_job=args.target_job,
        targets=args.targets,
        image_override=args.image,
    )
    _write_output(args, output)
    if output.status != "ready" or output.plan is None:
        _print_diagnostics(output.errors)
        return 1
    _append_github_output(github_outputs(output.plan, config))
    if not args.output and not os.environ.get("GITHUB_OUTPUT"):
        print(canonical_json(output.plan.to_dict()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "images":
        try:
            return _images(args)
        except (CiConfigError, PlanError) as exc:
            _print_diagnostics(exc.issues)
            return 1

    source = _source(_root(args.root))
    try:
        return _plan(args, source)
    except ValueError as exc:
        return _fail_plan(args, source, [Diagnostic(
            code="invalid-cli",
            location="plan",
            message=str(exc),
        )])
    except (CiConfigError, FactsError, PlanError) as exc:
        return _fail_plan(args, source, exc.issues)


if __name__ == "__main__":
    sys.exit(main())
