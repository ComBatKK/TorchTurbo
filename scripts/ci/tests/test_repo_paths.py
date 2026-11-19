from __future__ import annotations

from scripts.ci.tools.repo_paths import (
    is_dockerfile_path,
    is_docs_path,
    normalize_repo_path,
)


def test_normalize_repo_path_uses_posix_separators() -> None:
    assert normalize_repo_path("./docker\\npu\\Dockerfile_aarch64") == (
        "docker/npu/Dockerfile_aarch64"
    )
    assert normalize_repo_path("") == ""


def test_docs_paths_are_documentation_only() -> None:
    assert is_docs_path("docs/design.md")
    assert is_docs_path("docs/assets/overview.png")
    assert is_docs_path("README.md")
    assert is_docs_path("CHANGELOG.rst")
    assert is_docs_path("LICENSE")
    assert is_docs_path("NOTICE")


def test_code_and_config_paths_are_not_documentation_only() -> None:
    assert not is_docs_path("requirements.txt")
    assert not is_docs_path("pyproject.toml")
    assert not is_docs_path(".github/workflows/ascend-pr.yml")
    assert not is_docs_path("scripts/ci/execution/producer/planner.py")
    assert not is_docs_path("pytest.ini")


def test_only_dockerfiles_are_image_inputs() -> None:
    assert is_dockerfile_path("docker/Dockerfile")
    assert is_dockerfile_path("docker/npu/Dockerfile_aarch64")
    assert not is_dockerfile_path("docker/npu/setup.sh")
    assert not is_dockerfile_path("docker/npu/README.md")
    assert not is_dockerfile_path("scripts/ci/config/ci.yml")
    assert not is_dockerfile_path("scripts/ci/image/selection.py")
    assert not is_dockerfile_path("src/model.py")
