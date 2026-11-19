from __future__ import annotations

from pathlib import Path

from scripts.ci.routing.ci_router import (
    ChangeRoute,
    main,
    route_changes,
)


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
    assert capsys.readouterr().out.strip() == "docs_only"


def test_cli_missing_file_fails_closed(tmp_path: Path, capsys) -> None:
    assert main(["--changed-files", str(tmp_path / "missing.txt")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot read changed-file list" in captured.err
