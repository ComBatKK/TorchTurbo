from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.image import release
from scripts.ci.image.identity import image_plan
from scripts.ci.image.registry import (
    DOCKER_V2,
    Manifest,
    Registry,
    RegistryError,
    verify_manifest,
)
from scripts.ci.image.release import (
    BuildError,
    _run_build,
    build_command,
    ensure_image,
    format_refs,
    max_jobs,
    resolve_images,
    temporary_build_dockerfile,
)


class FakeRegistry:
    def __init__(self, manifests: dict[str, Manifest]) -> None:
        self.manifests = manifests
        self.promotions: list[tuple[str, str]] = []

    def manifest(self, reference: str) -> Manifest | None:
        return self.manifests.get(reference)

    def require(self, reference: str) -> Manifest:
        value = self.manifest(reference)
        if value is None:
            raise RegistryError(f"missing {reference}")
        return value

    def promote(self, alias: str, digest: str) -> None:
        self.promotions.append((alias, digest))


def _manifest(
    reference: str,
    digest: str = "sha256:abc",
    media_type: str = DOCKER_V2,
) -> Manifest:
    return Manifest(reference=reference, digest=digest, media_type=media_type, raw={})


def test_build_command_uses_content_tag_not_alias(repo_root: Path) -> None:
    plan = image_plan(repo_root, "npu-default")
    command = build_command(plan, should_push=True, use_cache=False, jobs=4)

    assert "--tag" in command
    assert plan.content_ref in command
    assert not any(value.endswith(f":{plan.spec.alias}") for value in command)
    assert "--output=type=image,push=true,oci-mediatypes=false" in command
    assert "--build-arg=MAX_JOBS=4" in command
    assert "--build-arg=GIT_HTTP_USER_AGENT=git/2.56.1" in command


def test_temporary_dockerfile_injects_git_user_agent(repo_root: Path) -> None:
    plan = image_plan(repo_root, "npu-default")

    dockerfile, temporary = temporary_build_dockerfile(plan, repo_root)

    try:
        text = Path(dockerfile).read_text(encoding="utf-8")
        assert text.count("ARG GIT_HTTP_USER_AGENT") == text.count("FROM ")
        assert text.count("ENV GIT_HTTP_USER_AGENT=${GIT_HTTP_USER_AGENT}") == text.count(
            "FROM "
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def test_max_jobs_respects_memory_limit() -> None:
    assert max_jobs("auto", cores=8, mem_gb=15) == 5
    assert max_jobs("auto", cores=16, mem_gb=0) == 1
    assert max_jobs("6", cores=1, mem_gb=1) == 6


def test_verify_manifest_rejects_oci() -> None:
    with pytest.raises(RegistryError, match="Docker v2s2"):
        verify_manifest(
            _manifest("x", media_type="application/vnd.oci.image.manifest.v1+json")
        )


def test_resolve_images_returns_immutable_refs(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DigestRegistry:
        def __init__(self, repository: str) -> None:
            self.repository = repository

        def require(self, reference: str) -> Manifest:
            return _manifest(reference)

    monkeypatch.setattr(release, "Registry", DigestRegistry)

    results, warning = resolve_images(
        repo_root,
        overrides={"npu-default": "registry.example/team/image:cann9.0.0-a3-cabc"},
    )

    assert warning == ""
    assert format_refs(results) == (
        "npu-default=registry.example/team/image@sha256:abc\n"
    )


def test_existing_candidate_does_not_build(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = image_plan(repo_root, "npu-default")
    registry = FakeRegistry({plan.content_tag: _manifest(plan.content_tag)})
    monkeypatch.setattr(release, "Registry", lambda repository: registry)

    def fail_build(*args, **kwargs):
        raise AssertionError("build should not run")

    monkeypatch.setattr(release, "build_candidate_image", fail_build)
    result = ensure_image(repo_root, "npu-default")

    assert result.built is False
    assert result.digest == "sha256:abc"


def test_build_without_push_and_missing_candidate_fails(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release, "Registry", lambda repository: FakeRegistry({}))

    with pytest.raises(BuildError, match="pushing is disabled"):
        ensure_image(repo_root, "npu-default", should_push=False)


def test_missing_candidate_is_built_and_verified(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifests: dict[str, Manifest] = {}
    registry = FakeRegistry(manifests)
    monkeypatch.setattr(release, "Registry", lambda repository: registry)
    monkeypatch.setattr(release, "check_envs", lambda: [])

    def build(plan, root, **kwargs):
        manifests[plan.content_tag] = _manifest(plan.content_tag, "sha256:built")
        return "sha256:built", 7

    monkeypatch.setattr(release, "build_candidate_image", build)
    result = ensure_image(repo_root, "npu-default")

    assert result.built is True
    assert result.elapsed_seconds == 7
    assert result.ref.endswith("@sha256:built")


def test_promote_moves_alias_to_the_candidate(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = image_plan(repo_root, "npu-default")
    registry = FakeRegistry({plan.content_tag: _manifest(plan.content_tag, "sha256:abc")})

    def promote(alias: str, digest: str) -> None:
        registry.promotions.append((alias, digest))
        registry.manifests[alias] = _manifest(alias, digest)

    registry.promote = promote
    monkeypatch.setattr(release, "Registry", lambda repository: registry)
    monkeypatch.setattr(release, "check_envs", lambda: [])

    result = release.promote_image(repo_root, "npu-default")

    assert registry.promotions == [(plan.spec.alias, "sha256:abc")]
    assert result.promoted is True
    assert result.alias == plan.spec.alias


def test_registry_promote_builds_retag_command() -> None:
    observed: list[list[str]] = []

    def fake_run(command, **kwargs):
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    registry = Registry("registry.example/team/image")
    registry.promote("a3-base", "sha256:abc", run=fake_run)
    assert observed == [[
        "docker",
        "buildx",
        "imagetools",
        "create",
        "-t",
        "registry.example/team/image:a3-base",
        "registry.example/team/image@sha256:abc",
    ]]


def test_run_build_streams_and_captures_output(tmp_path: Path, capsys) -> None:
    result = _run_build(
        [sys.executable, "-c", "print('line one'); print('line two')"],
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["line one", "line two"]
    assert capsys.readouterr().out.splitlines() == ["line one", "line two"]
