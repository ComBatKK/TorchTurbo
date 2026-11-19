from __future__ import annotations

from pathlib import Path

from scripts.ci.routing.change_router import (
    ChangeRoute,
    is_dockerfile_path,
    is_docs_path,
    main,
    route_changes,
)


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


def test_router_handles_docs_code_and_image_changes() -> None:
    assert route_changes([]) == ChangeRoute.CODE
    assert route_changes(["docs/design.md", "README.md"]) == ChangeRoute.DOCS_ONLY
    assert route_changes([
        "docs/design.md",
        "scripts/ci/execution/producer/planner.py",
    ]) == ChangeRoute.CODE
    assert route_changes(["requirements.txt"]) == ChangeRoute.CODE
    assert route_changes(["docker/npu/Dockerfile_aarch64"]) == ChangeRoute.IMAGE_ONLY
    assert route_changes(["docker/npu/setup.sh"]) == ChangeRoute.CODE
    assert route_changes(["scripts/ci/config/ci.yml"]) == ChangeRoute.CODE
    assert route_changes(["scripts/ci/image/selection.py"]) == ChangeRoute.CODE
    assert route_changes([
        "docker/npu/Dockerfile_aarch64",
        "docs/design.md",
    ]) == ChangeRoute.IMAGE_ONLY
    assert route_changes([
        "docker/npu/Dockerfile_aarch64",
        "scripts/ci/execution/producer/planner.py",
    ]) == ChangeRoute.MIXED
    assert route_changes([
        "docker/npu-new/Dockerfile_new",
        "scripts/ci/config/ci.yml",
    ]) == ChangeRoute.MIXED
    assert route_changes([
        "docker/npu/setup.sh",
        "scripts/ci/execution/producer/planner.py",
    ]) == ChangeRoute.CODE


def test_cli_reads_changed_files(tmp_path: Path, capsys) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("docs/design.md\nREADME.md\n", encoding="utf-8")
    assert main(["--changed-files", str(changed)]) == 0
    assert capsys.readouterr().out.strip() == "change_route=docs_only"


def test_cli_missing_file_fails_open(tmp_path: Path, capsys) -> None:
    assert main(["--changed-files", str(tmp_path / "missing.txt")]) == 0
    assert capsys.readouterr().out.strip() == "change_route=code"
