from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.image import release
from scripts.ci.image.identity import image_plan
from scripts.ci.image.registry import DOCKER_V2, Manifest, RegistryError, verify_manifest
from scripts.ci.image.release import (
    BuildError,
    build_command,
    ensure_image,
    format_refs,
    max_jobs,
    resolve_images,
    temporary_build_dockerfile,
)

DIGEST = "sha256:" + "a" * 64
BUILT_DIGEST = "sha256:" + "b" * 64


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
        self.manifests[alias] = _manifest(alias, digest)


def _manifest(
    reference: str,
    digest: str = DIGEST,
    media_type: str = DOCKER_V2,
) -> Manifest:
    return Manifest(reference=reference, digest=digest, media_type=media_type, raw={})


def test_build_command_uses_candidate_tag_and_ci_build_args(repo_root: Path) -> None:
    plan = image_plan(repo_root, "npu-a3-cann90")
    command = build_command(plan, should_push=True, use_cache=False, jobs=4)

    assert "--tag" in command
    assert plan.candidate_ref in command
    assert plan.spec.publish_ref not in command
    assert "--output=type=image,push=true,oci-mediatypes=false" in command
    assert "--build-arg=MAX_JOBS=4" in command
    assert "--build-arg=GIT_HTTP_USER_AGENT=git/2.56.1" in command
    assert command[-1] == "docker/npu"


def test_temporary_dockerfile_injects_git_user_agent(repo_root: Path) -> None:
    plan = image_plan(repo_root, "npu-a3-cann90")
    dockerfile, temporary = temporary_build_dockerfile(plan, repo_root)
    try:
        text = Path(dockerfile).read_text(encoding="utf-8")
        assert text.count("ARG GIT_HTTP_USER_AGENT") == text.count("FROM ")
        assert text.count("ENV GIT_HTTP_USER_AGENT=${GIT_HTTP_USER_AGENT}") == (
            text.count("FROM ")
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
    results = resolve_images(
        repo_root,
        names="npu-a3-cann90",
        overrides={"npu-a3-cann90": "registry.example/team/image:candidate"},
    )

    assert format_refs(results) == (
        f"npu-a3-cann90=registry.example/team/image@{DIGEST}\n"
    )
    assert results[0].build.binding.kind == "container"
    assert results[0].build.built is False


def test_resolve_images_defaults_to_published_alias(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []

    class DigestRegistry:
        def __init__(self, repository: str) -> None:
            self.repository = repository

        def require(self, reference: str) -> Manifest:
            requested.append(reference)
            return _manifest(reference)

    monkeypatch.setattr(release, "Registry", DigestRegistry)
    results = resolve_images(repo_root, names="npu-a3-cann90")

    assert requested == ["cann9.0.0-a3-base"]
    assert format_refs(results) == (
        "npu-a3-cann90="
        f"swr.cn-north-12.myhuaweicloud.com/ascend/torchturbo_npu@{DIGEST}\n"
    )


def test_existing_candidate_does_not_build(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = image_plan(repo_root, "npu-a3-cann90")
    registry = FakeRegistry({plan.candidate_tag: _manifest(plan.candidate_tag)})
    monkeypatch.setattr(release, "Registry", lambda repository: registry)

    def fail_build(*args, **kwargs):
        raise AssertionError("build should not run")

    monkeypatch.setattr(release, "build_candidate_image", fail_build)
    result = ensure_image(repo_root, "npu-a3-cann90")

    assert result.built is False
    assert result.digest == DIGEST
    assert result.build.build_input_hash == plan.build_input_hash


def test_build_without_push_and_missing_candidate_fails(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release, "Registry", lambda repository: FakeRegistry({}))
    with pytest.raises(BuildError, match="pushing is disabled"):
        ensure_image(repo_root, "npu-a3-cann90", should_push=False)


def test_missing_candidate_is_built_and_verified(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifests: dict[str, Manifest] = {}
    registry = FakeRegistry(manifests)
    monkeypatch.setattr(release, "Registry", lambda repository: registry)
    monkeypatch.setattr(release, "check_envs", lambda: [])

    def build(plan, root, **kwargs):
        manifests[plan.candidate_tag] = _manifest(plan.candidate_tag, BUILT_DIGEST)
        return BUILT_DIGEST, 7

    monkeypatch.setattr(release, "build_candidate_image", build)
    result = ensure_image(repo_root, "npu-a3-cann90")

    assert result.built is True
    assert result.digest == BUILT_DIGEST
    assert result.elapsed_seconds == 7
    assert result.build.source_sha


def test_promote_requires_the_tested_candidate(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = image_plan(repo_root, "npu-a3-cann90")
    registry = FakeRegistry({
        plan.candidate_tag: _manifest(plan.candidate_tag, BUILT_DIGEST),
    })
    monkeypatch.setattr(release, "Registry", lambda repository: registry)
    monkeypatch.setattr(release, "check_envs", lambda: [])

    result = release.promote_image(repo_root, "npu-a3-cann90")

    assert result.promoted is True
    assert result.alias == plan.spec.alias
    assert registry.promotions == [(plan.spec.alias, BUILT_DIGEST)]
    assert result.ref == f"{plan.spec.repository}@{BUILT_DIGEST}"
