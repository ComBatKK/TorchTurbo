"""CLI for the TorchTurbo platform image domain."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .. import repository_root
from ..contracts.artifacts import write_json
from .build import (
    BuildError,
    ImageResult,
    ensure_candidate,
    max_jobs,
    resolve_candidate,
    resolve_formal,
    verify_manifest,
)
from .plan import (
    ImageConfigMissing,
    ImageError,
    POLICIES,
    container_image_names,
    plan_image,
)
from .registry import Registry, RegistryError


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repository_root()


def _write_outputs(path: str, values: dict[str, object]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _print_result(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _write_result(path: str, value: object) -> None:
    if path:
        write_json(Path(path), value)


def _registry(plan) -> Registry:
    return Registry(plan.repository)


def _env(root: Path) -> int:
    if not shutil.which("docker"):
        raise ImageError("docker is not available")
    version = subprocess.run(
        ["docker", "version", "--format", "client={{.Client.Version}}"],
        capture_output=True,
        text=True,
    )
    if version.returncode != 0:
        raise ImageError((version.stderr or version.stdout).strip() or "docker is not usable")
    buildx = subprocess.run(
        ["docker", "buildx", "version"],
        capture_output=True,
        text=True,
    )
    if buildx.returncode != 0:
        raise ImageError("docker buildx is not available")
    print(f"host={platform.node()} arch={platform.machine()}")
    print((version.stdout or "").strip())
    print((buildx.stdout or "").strip())
    return 0


def _prepare(args: argparse.Namespace):
    return plan_image(
        _root(args.root),
        args.image,
        revision=args.revision or None,
        base_sha=args.base_sha or None,
        policy=args.policy,
        build_allowed=not args.block_build,
    )


def cmd_plan(args: argparse.Namespace) -> int:
    plan = _prepare(args)
    payload = plan.to_dict()
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        _print_result(payload)
    _write_outputs(args.github_output, {
        "image_mode": plan.action,
        "image_changed": (
            "unknown" if plan.image_changed is None
            else str(plan.image_changed).lower()
        ),
        "content_tag": plan.content_tag,
        "content_hash": plan.content_hash,
        "content_ref": plan.content_ref,
        "cann_version": plan.cann_version,
        "alias": plan.alias,
        "repository": plan.repository,
        "platform": plan.platform,
        "dockerfile": plan.dockerfile,
        "context": plan.context,
    })
    return 0


def cmd_identity(args: argparse.Namespace) -> int:
    plan = _prepare(args)
    payload = {
        "schema_version": plan.schema_version,
        "logical_image": plan.logical_image,
        "target": plan.target,
        "hardware": plan.hardware,
        "repository": plan.repository,
        "alias": plan.alias,
        "cann_version": plan.cann_version,
        "platform": plan.platform,
        "content_hash": plan.content_hash,
        "content_tag": plan.content_tag,
        "content_ref": plan.content_ref,
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        _print_result(payload)
    _write_outputs(args.github_output, {
        "content_tag": plan.content_tag,
        "content_hash": plan.content_hash,
        "content_ref": plan.content_ref,
        "repository": plan.repository,
        "alias": plan.alias,
    })
    return 0


def cmd_refs(args: argparse.Namespace) -> int:
    root = _root(args.root)
    revision = args.revision or None
    try:
        names = container_image_names(root, revision)
    except ImageConfigMissing as exc:
        if revision is None:
            raise
        print(f"::warning::{exc}; using the checked-out CI config", file=sys.stderr)
        revision = None
        names = container_image_names(root, revision)
    lines = []
    for image_name in names:
        plan = plan_image(
            root,
            image_name,
            revision=revision,
            policy="resolve_candidate",
        )
        lines.append(f"{image_name}={plan.content_ref}")
    text = "\n".join(lines)
    if text:
        text += "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    _write_outputs(args.github_output, {"image_count": len(lines)})
    return 0


def _write_resolved_outputs(path: str, result: ImageResult) -> None:
    _write_outputs(path, {
        "ref": result.ref,
        "digest": result.digest,
        "content_tag": result.content_tag,
        "built": str(result.built).lower(),
        "cache_hit": str(result.cache_hit).lower(),
    })


def cmd_resolve(args: argparse.Namespace) -> int:
    plan = _prepare(args)
    if plan.action == "blocked":
        raise ImageError("image resolution is blocked for this source")
    result = resolve_formal(plan, _registry(plan))
    _write_result(args.output, result.to_dict())
    if not args.output:
        _print_result(result.to_dict())
    _write_resolved_outputs(args.github_output, result)
    return 0


def cmd_resolve_candidate(args: argparse.Namespace) -> int:
    plan = _prepare(args)
    result = resolve_candidate(plan, _registry(plan))
    _write_result(args.output, result.to_dict())
    if not args.output:
        _print_result(result.to_dict())
    _write_resolved_outputs(args.github_output, result)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    plan = _prepare(args)
    if plan.action == "blocked" and not args.no_push:
        raise ImageError("blocked image plan cannot push a candidate")
    if plan.action not in {"build_if_missing", "resolve", "blocked"}:
        raise ImageError(f"image build is not allowed in mode {plan.action!r}")
    jobs = max_jobs(args.max_jobs)
    result = ensure_candidate(
        plan,
        _registry(plan),
        _root(args.root),
        should_push=not args.no_push,
        use_cache=not args.no_cache,
        builder=args.builder,
        jobs=jobs,
    )
    _write_result(args.output, result.to_dict())
    if not args.output:
        _print_result(result.to_dict())
    _write_resolved_outputs(args.github_output, result)
    _write_outputs(args.github_output, {"elapsed_seconds": result.elapsed_seconds})
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    plan = _prepare(args)
    if plan.action == "blocked":
        raise ImageError("image promotion is blocked for this source")
    registry = _registry(plan)
    content = registry.require(plan.content_tag)
    verify_manifest(content)
    digest = args.digest or content.digest
    if digest != content.digest:
        raise RegistryError(
            f"requested digest {digest} does not match {plan.content_tag}={content.digest}"
        )
    registry.promote(plan.alias, digest)
    alias = registry.require(plan.alias)
    if alias.digest != digest:
        raise RegistryError(
            f"promote verification failed: {plan.alias}={alias.digest}, expected {digest}"
        )
    result = {
        "logical_image": plan.logical_image,
        "alias": plan.alias,
        "content_tag": plan.content_tag,
        "digest": digest,
        "ref": f"{plan.repository}@{digest}",
        "promoted": True,
    }
    _write_result(args.output, result)
    if not args.output:
        _print_result(result)
    _write_outputs(args.github_output, {
        "alias": plan.alias,
        "ref": result["ref"],
        "digest": digest,
        "content_tag": plan.content_tag,
        "promoted": "true",
    })
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    plan = _prepare(args)
    registry = _registry(plan)
    reference = args.tag or plan.content_tag
    manifest = registry.require(reference)
    verify_manifest(manifest)
    _print_result({
        "reference": reference,
        "digest": manifest.digest,
        "media_type": manifest.media_type,
        "platform": plan.platform,
        "ref": f"{plan.repository}@{manifest.digest}",
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "env",
            "identity",
            "refs",
            "plan",
            "resolve",
            "resolve-candidate",
            "build",
            "promote",
            "verify",
        ),
    )
    parser.add_argument("--root", default=None)
    parser.add_argument("--image", default="npu-default")
    parser.add_argument("--revision", default="")
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--policy", choices=POLICIES, default="auto")
    parser.add_argument("--output", default="")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    parser.add_argument("--max-jobs", default="auto")
    parser.add_argument("--builder", default="")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--block-build", action="store_true")
    parser.add_argument("--tag", default="")
    parser.add_argument("--digest", default="")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "env":
            return _env(_root(args.root))
        if args.command == "identity":
            return cmd_identity(args)
        if args.command == "refs":
            return cmd_refs(args)
        if args.command == "plan":
            return cmd_plan(args)
        if args.command == "resolve":
            return cmd_resolve(args)
        if args.command == "resolve-candidate":
            return cmd_resolve_candidate(args)
        if args.command == "build":
            return cmd_build(args)
        if args.command == "promote":
            return cmd_promote(args)
        return cmd_verify(args)
    except (ImageError, RegistryError, BuildError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
