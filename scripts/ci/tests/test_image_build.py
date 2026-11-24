from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.image import build_image as image_build
from scripts.ci.image.registry import (
    DOCKER_V2,
    Manifest,
    Registry,
    RegistryError,
    verify_manifest,
)
from scripts.ci.image.build_image import (
    BuildError,
    build_command,
    get_or_build_image,
    get_image_plan,
    max_jobs,
    resolve_image,
    resolved_image_for_content_tag,
    patch_dockerfile,
)

DIGEST = "sha256:" + "a" * 64
BUILT_DIGEST = "sha256:" + "b" * 64


class FakeRegistry:
    def __init__(self, manifests: dict[str, Manifest]) -> None:
        self.manifests = manifests
        self.alias_updates: list[tuple[str, str]] = []

    def find_manifest(self, reference: str) -> Manifest | None:
        return self.manifests.get(reference)

    def require_manifest(self, reference: str) -> Manifest:
        value = self.find_manifest(reference)
        if value is None:
            raise RegistryError(f"missing {reference}")
        return value

    def promote_to_alias(self, alias: str, digest: str) -> None:
        self.alias_updates.append((alias, digest))
        self.manifests[alias] = _manifest(alias, digest)


def _manifest(
    reference: str,
    digest: str = DIGEST,
    media_type: str = DOCKER_V2,
) -> Manifest:
    return Manifest(reference=reference, digest=digest, media_type=media_type, raw={})


def test_build_command_uses_content_tag_and_ci_build_args(repo_root: Path) -> None:
    plan = get_image_plan(repo_root, "npu-a3-cann90")
    command = build_command(plan, use_cache=False, jobs=4)

    assert "--tag" in command
    assert plan.content_ref in command
    assert f"{plan.spec.repository}:{plan.spec.alias}" not in command
    assert "--output=type=image,push=true,oci-mediatypes=false" in command
    assert "--build-arg=MAX_JOBS=4" in command
    assert "--build-arg=GIT_HTTP_USER_AGENT=git/2.56.1" in command
    assert command[-1] == "docker/npu"


def test_patch_dockerfile_injects_git_user_agent(repo_root: Path) -> None:
    plan = get_image_plan(repo_root, "npu-a3-cann90")
    dockerfile, temporary = patch_dockerfile(plan, repo_root)
    try:
        text = dockerfile.read_text(encoding="utf-8")
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


def test_max_jobs_rejects_invalid_value() -> None:
    with pytest.raises(BuildError, match="invalid max jobs"):
        max_jobs("invalid")


def test_verify_manifest_rejects_oci() -> None:
    with pytest.raises(RegistryError, match="Docker v2 manifest"):
        verify_manifest(
            _manifest("x", media_type="application/vnd.oci.image.manifest.v1+json")
        )


def test_registry_rejects_repository_without_path() -> None:
    with pytest.raises(RegistryError, match="invalid repository"):
        Registry("registry.example")


def test_registry_promote_rejects_invalid_digest() -> None:
    registry = Registry("registry.example/team/image")

    with pytest.raises(RegistryError, match="sha256"):
        registry.promote_to_alias("stable", "latest")


def test_content_tag_uses_the_current_content_hash(repo_root: Path) -> None:
    plan = get_image_plan(repo_root, "npu-a3-cann90")
    short_hash = plan.content_hash.removeprefix("sha256:")[:12]

    assert plan.content_tag == f"cann9.0.0-a3-{short_hash}"


def test_resolved_image_for_content_tag_resolves_an_immutable_ref(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = get_image_plan(repo_root, "npu-a3-cann90")
    registry = FakeRegistry({plan.content_tag: _manifest(plan.content_tag)})
    monkeypatch.setattr(image_build, "Registry", lambda repository: registry)

    image = resolved_image_for_content_tag(plan, plan.content_tag)

    assert image.image_id == "npu-a3-cann90"
    assert image.ref == f"{plan.spec.repository}@{DIGEST}"


def test_resolve_image_uses_the_current_content_tag(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = get_image_plan(repo_root, "npu-a3-cann90")
    registry = FakeRegistry({plan.content_tag: _manifest(plan.content_tag)})
    monkeypatch.setattr(image_build, "Registry", lambda repository: registry)

    image = resolve_image(repo_root, "npu-a3-cann90")

    assert image.digest == DIGEST
    assert image.ref == f"{plan.spec.repository}@{DIGEST}"


def test_existing_content_image_does_not_build(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = get_image_plan(repo_root, "npu-a3-cann90")
    registry = FakeRegistry({plan.content_tag: _manifest(plan.content_tag)})
    monkeypatch.setattr(image_build, "Registry", lambda repository: registry)

    def fail_build(*args, **kwargs):
        raise AssertionError("build should not run")

    monkeypatch.setattr(image_build, "build_image", fail_build)
    result = get_or_build_image(repo_root, "npu-a3-cann90")

    assert result.build.built is False
    assert result.build.image.digest == DIGEST
    assert result.build.content_hash == plan.content_hash


def test_missing_content_image_is_built_and_verified(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifests: dict[str, Manifest] = {}
    registry = FakeRegistry(manifests)
    monkeypatch.setattr(image_build, "Registry", lambda repository: registry)
    monkeypatch.setattr(image_build, "check_build_environment", lambda: None)

    def build(plan, root, **kwargs):
        manifests[plan.content_tag] = _manifest(plan.content_tag, BUILT_DIGEST)
        return 7

    monkeypatch.setattr(image_build, "build_image", build)
    result = get_or_build_image(repo_root, "npu-a3-cann90")

    assert result.build.built is True
    assert result.build.image.digest == BUILT_DIGEST
    assert result.elapsed_seconds == 7
    assert result.build.git_commit_sha


def test_promote_requires_the_tested_content_image(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = get_image_plan(repo_root, "npu-a3-cann90")
    registry = FakeRegistry({
        plan.content_tag: _manifest(plan.content_tag, BUILT_DIGEST),
    })
    monkeypatch.setattr(image_build, "Registry", lambda repository: registry)
    monkeypatch.setattr(image_build, "check_build_environment", lambda: None)

    result = image_build.promote_image(repo_root, "npu-a3-cann90")

    assert result.promoted is True
    assert result.alias == plan.spec.alias
    assert registry.alias_updates == [(plan.spec.alias, BUILT_DIGEST)]
    assert result.build.image.ref == f"{plan.spec.repository}@{BUILT_DIGEST}"
