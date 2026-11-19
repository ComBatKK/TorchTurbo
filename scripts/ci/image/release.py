"""Image release lifecycle: resolve, build, verify, and promote."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .identity import ImagePlan, image_plan, image_plans
from .recipe import ImageConfigMissing, ImageError, split_reference
from .registry import Manifest, Registry, RegistryError, verify_manifest

BUILD_LOG = Path("/tmp/build.log")


class BuildError(RuntimeError):
    """Raised when an image cannot be built or verified."""


@dataclass(frozen=True)
class ImageResult:
    name: str
    target: str
    ref: str
    digest: str
    built: bool
    elapsed_seconds: int = 0
    alias: str = ""
    promoted: bool = False

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 2,
            "name": self.name,
            "target": self.target,
            "ref": self.ref,
            "digest": self.digest,
            "built": self.built,
            "elapsed_seconds": self.elapsed_seconds,
        }
        if self.alias:
            value["alias"] = self.alias
            value["promoted"] = self.promoted
        return value


def _manifest_for_reference(
    reference: str,
    registries: dict[str, Registry],
) -> tuple[str, Manifest]:
    if "@" in reference:
        repository, digest = reference.rsplit("@", 1)
        if not repository or not digest.startswith("sha256:"):
            raise ImageError(f"invalid digest reference: {reference!r}")
        registry = registries.setdefault(repository, Registry(repository))
        manifest = registry.require(digest)
    else:
        repository, tag = split_reference(reference)
        registry = registries.setdefault(repository, Registry(repository))
        manifest = registry.require(tag)
    verify_manifest(manifest)
    return repository, manifest


def resolve_images(
    root: Path,
    *,
    revision: str | None = None,
    names: str = "",
    overrides: Mapping[str, str] | None = None,
) -> tuple[list[ImageResult], str]:
    """Resolve logical images to immutable digest references."""
    warning = ""
    try:
        plans = image_plans(root, names=names, revision=revision)
    except ImageConfigMissing as exc:
        if revision is None:
            raise
        warning = f"{exc}; using the checked-out CI configuration"
        plans = image_plans(root, names=names, revision=None)

    by_name = {plan.name: plan for plan in plans}
    refs = dict(overrides or {})
    unknown = sorted(set(refs) - set(by_name))
    if unknown:
        raise ImageError(f"unknown image ref override(s): {', '.join(unknown)}")

    registries: dict[str, Registry] = {}
    results: list[ImageResult] = []
    for plan in plans:
        reference = refs.get(plan.name, plan.content_ref)
        repository, manifest = _manifest_for_reference(reference, registries)
        results.append(
            ImageResult(
                name=plan.name,
                target=plan.spec.target,
                ref=f"{repository}@{manifest.digest}",
                digest=manifest.digest,
                built=False,
            )
        )
    return results, warning


def format_refs(results: list[ImageResult]) -> str:
    return "".join(f"{result.name}={result.ref}\n" for result in results)


def check_envs() -> list[str]:
    """Check the container toolchain used by build and promotion."""
    if not shutil.which("docker"):
        raise BuildError("docker is not available")
    version = subprocess.run(
        ["docker", "version", "--format", "client={{.Client.Version}}"],
        capture_output=True,
        text=True,
    )
    if version.returncode != 0:
        raise BuildError((version.stderr or version.stdout).strip() or "docker is not usable")
    buildx = subprocess.run(["docker", "buildx", "version"], capture_output=True, text=True)
    if buildx.returncode != 0:
        raise BuildError("docker buildx is not available")
    return [
        f"host={platform.node()} arch={platform.machine()}",
        version.stdout.strip(),
        buildx.stdout.strip(),
    ]


def max_jobs(spec: str = "auto", *, cores: int | None = None, mem_gb: int | None = None) -> int:
    if spec and spec != "auto":
        return max(1, int(spec))
    cores = cores if cores is not None else (os.cpu_count() or 2)
    if mem_gb is None:
        mem_gb = 0
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    mem_gb = int(int(line.split()[1]) / 1048576)
                    break
        except OSError:
            pass
    return max(1, min(cores, (mem_gb or 1) // 3 or 1))


def build_command(
    plan: ImagePlan,
    *,
    should_push: bool,
    use_cache: bool,
    builder: str = "",
    jobs: int = 1,
    metadata_file: str = "/tmp/build-meta.json",
    dockerfile: str | None = None,
) -> list[str]:
    spec = plan.spec
    command = ["docker", "buildx", "build"]
    if builder:
        command += ["--builder", builder]
    command += [
        "--progress=plain",
        f"--platform={spec.platform}",
        f"--file={dockerfile or spec.dockerfile}",
        f"--build-arg=MAX_JOBS={jobs}",
    ]
    command += [f"--build-arg={key}={value}" for key, value in sorted(spec.build_args.items())]
    command += ["--provenance=false", "--sbom=false"]
    if use_cache:
        scope = f"torchturbo-{spec.alias}"
        command += [
            f"--cache-from=type=gha,scope={scope}",
            f"--cache-to=type=gha,mode=min,scope={scope}",
        ]
    if should_push:
        command += [
            "--tag",
            plan.content_ref,
            f"--metadata-file={metadata_file}",
            "--output=type=image,push=true,oci-mediatypes=false",
        ]
    else:
        command.append("--output=type=cacheonly")
    command.append(spec.context)
    return command


def temporary_build_dockerfile(plan: ImagePlan, root: Path) -> tuple[str, Path | None]:
    """Inject the GitCode UA workaround into a temporary Dockerfile."""
    path = root / plan.spec.dockerfile
    source = path.read_text(encoding="utf-8")
    user_agent = plan.spec.build_args.get("GIT_HTTP_USER_AGENT", "")
    if not user_agent or "GIT_HTTP_USER_AGENT" in source:
        return plan.spec.dockerfile, None
    injected: list[str] = []
    found_from = False
    for line in source.splitlines(keepends=True):
        injected.append(line)
        if line.lstrip().upper().startswith("FROM "):
            injected += [
                "ARG GIT_HTTP_USER_AGENT\n",
                "ENV GIT_HTTP_USER_AGENT=${GIT_HTTP_USER_AGENT}\n",
            ]
            found_from = True
    if not found_from:
        raise BuildError(f"dockerfile has no FROM instruction: {plan.spec.dockerfile}")
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="torchturbo-ci-Dockerfile-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write("".join(injected))
        temporary = Path(handle.name)
    return str(temporary), temporary


def _metadata_digest(path: str) -> str:
    try:
        return str(
            json.loads(Path(path).read_text(encoding="utf-8")).get("containerimage.digest", "")
        )
    except (OSError, ValueError):
        return ""


def _run_build(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run docker buildx while streaming every line to the caller's log."""
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    if process.stdout is None:  # pragma: no cover - Popen requested a pipe
        raise BuildError("failed to capture docker buildx output")
    lines: list[str] = []
    for line in process.stdout:
        lines.append(line)
        print(line, end="", flush=True)
    return subprocess.CompletedProcess(command, process.wait(), "".join(lines), "")


def _inspect_digest(ref: str) -> str:
    process = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{.Manifest.Digest}}"],
        capture_output=True,
        text=True,
    )
    return process.stdout.strip() if process.returncode == 0 else ""


def build_candidate_image(
    plan: ImagePlan,
    root: Path,
    *,
    use_cache: bool = True,
    builder: str = "",
    jobs: int = 1,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run_build,
) -> tuple[str, int]:
    with tempfile.NamedTemporaryFile(prefix="torchturbo-build-meta-", suffix=".json") as handle:
        metadata_file = handle.name
    dockerfile, temporary = temporary_build_dockerfile(plan, root)
    try:
        command = build_command(
            plan,
            should_push=True,
            use_cache=use_cache,
            builder=builder,
            jobs=jobs,
            metadata_file=metadata_file,
            dockerfile=dockerfile,
        )
        started = time.monotonic()
        process = run(command, cwd=root)
        elapsed = int(time.monotonic() - started)
        log = process.stdout or ""
        BUILD_LOG.write_text(log, encoding="utf-8")
        if process.returncode != 0:
            raise BuildError(
                f"image build failed (exit {process.returncode}); see {BUILD_LOG}"
            )
        digest = _metadata_digest(metadata_file) or _inspect_digest(plan.content_ref)
        if not digest.startswith("sha256:"):
            raise BuildError(f"build did not publish a digest for {plan.content_ref}")
        return digest, elapsed
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        Path(metadata_file).unlink(missing_ok=True)


def _result(plan: ImagePlan, digest: str, *, built: bool, elapsed: int = 0) -> ImageResult:
    return ImageResult(
        name=plan.name,
        target=plan.spec.target,
        ref=f"{plan.spec.repository}@{digest}",
        digest=digest,
        built=built,
        elapsed_seconds=elapsed,
    )


def ensure_image(
    root: Path,
    image: str,
    *,
    should_push: bool = True,
    use_cache: bool = True,
    builder: str = "",
    jobs: str = "auto",
) -> ImageResult:
    """Return an immutable candidate, building it only when it is absent."""
    plan = image_plan(root, image)
    registry = Registry(plan.spec.repository)
    existing = registry.manifest(plan.content_tag)
    if existing is not None:
        verify_manifest(existing)
        return _result(plan, existing.digest, built=False)
    if not should_push:
        raise BuildError(
            f"candidate {plan.content_ref} is absent and pushing is disabled"
        )
    check_envs()
    digest, elapsed = build_candidate_image(
        plan,
        root,
        use_cache=use_cache,
        builder=builder,
        jobs=max_jobs(jobs),
    )
    manifest = registry.require(plan.content_tag)
    verify_manifest(manifest)
    if manifest.digest != digest:
        raise BuildError(
            f"built digest {digest} differs from registry {manifest.digest} "
            f"for {plan.content_tag}"
        )
    return _result(plan, manifest.digest, built=True, elapsed=elapsed)


def promote_image(root: Path, image: str, *, digest: str = "") -> ImageResult:
    """Move a stable alias to the tested content candidate."""
    plan = image_plan(root, image)
    registry = Registry(plan.spec.repository)
    content = registry.require(plan.content_tag)
    verify_manifest(content)
    target = digest or content.digest
    if target != content.digest:
        raise RegistryError(
            f"requested digest {target} does not match {plan.content_tag}={content.digest}"
        )
    check_envs()
    registry.promote(plan.spec.alias, target)
    alias = registry.require(plan.spec.alias)
    if alias.digest != target:
        raise RegistryError(
            f"promote verification failed: {plan.spec.alias}={alias.digest}, "
            f"expected {target}"
        )
    return ImageResult(
        name=plan.name,
        target=plan.spec.target,
        ref=f"{plan.spec.repository}@{target}",
        digest=target,
        built=False,
        alias=plan.spec.alias,
        promoted=True,
    )
