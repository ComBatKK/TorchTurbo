from __future__ import annotations

from pathlib import Path

from scripts.ci.tools.changed_files import (
    ChangeKind,
    classify_changes,
    main,
)


def test_classify_changes_handles_docs_code_and_image_changes() -> None:
    assert classify_changes([]) == ChangeKind.CODE
    assert classify_changes(["docs/design.md", "README.md"]) == ChangeKind.DOCS_ONLY
    assert classify_changes([
        "docs/design.md",
        "scripts/ci/execution/producer/planner.py",
    ]) == ChangeKind.CODE
    assert classify_changes(["requirements.txt"]) == ChangeKind.CODE
    assert classify_changes(["docker/npu/Dockerfile_aarch64"]) == ChangeKind.IMAGE_ONLY
    assert classify_changes(["docker/npu/setup.sh"]) == ChangeKind.CODE
    assert classify_changes(["scripts/ci/config/ci.yml"]) == ChangeKind.CODE
    assert classify_changes(["scripts/ci/image/select_images.py"]) == ChangeKind.CODE
    assert classify_changes([
        "docker/npu/Dockerfile_aarch64",
        "docs/design.md",
    ]) == ChangeKind.IMAGE_ONLY
    assert classify_changes([
        "docker/npu/Dockerfile_aarch64",
        "scripts/ci/execution/producer/planner.py",
    ]) == ChangeKind.MIXED
    assert classify_changes([
        "docker/npu-new/Dockerfile_new",
        "scripts/ci/config/ci.yml",
    ]) == ChangeKind.MIXED
    assert classify_changes([
        "docker/npu/setup.sh",
        "scripts/ci/execution/producer/planner.py",
    ]) == ChangeKind.CODE


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
