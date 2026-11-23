from __future__ import annotations

from pathlib import Path

from scripts.ci.execution.producer.facts import _pytest_facts


def _scan(tmp_path: Path, source: str):
    path = tmp_path / "tests" / "unit_tests" / "ops" / "test_example.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    facts, errors = _pytest_facts(tmp_path)
    return facts[0], errors


def test_unmarked_test_defaults_to_cpu(tmp_path: Path) -> None:
    fact, errors = _scan(
        tmp_path,
        "def test_plain():\n    assert 1 + 1 == 2\n",
    )

    assert errors == []
    assert fact.devices == ("cpu",)
    assert fact.ranks_by_device == {"cpu": 1}


def test_explicit_multi_marker_sets_device_and_minimum_rank(tmp_path: Path) -> None:
    fact, errors = _scan(
        tmp_path,
        "import pytest\n\n@pytest.mark.multi_npu\n"
        "def test_npu():\n    pass\n",
    )

    assert errors == []
    assert fact.devices == ("npu",)
    assert fact.ranks_by_device == {"npu": 2}


def test_unmarked_cpu_multiprocess_rank_is_detected(tmp_path: Path) -> None:
    fact, errors = _scan(
        tmp_path,
        "def test_plain():\n    mp.spawn(worker, nprocs=4)\n",
    )

    assert errors == []
    assert fact.devices == ("cpu",)
    assert fact.ranks_by_device == {"cpu": 4}
