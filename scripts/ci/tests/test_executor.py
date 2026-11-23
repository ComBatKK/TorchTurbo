from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scripts.ci.execution.consumer import executor
from scripts.ci.execution.consumer import process
from scripts.ci.execution.consumer import integration_job
from scripts.ci.execution.consumer import pytest_job
from scripts.ci.contracts.executor_contract import (
    load_job_result,
)
from scripts.ci.contracts.planner_contract import IntegrationSpec, PytestSpec
from scripts.ci.execution.producer.planner import (
    build_execution_plan,
    build_planner_output,
)
from scripts.ci.execution.consumer.runtime.venv_manager import Venv
from scripts.ci.tools.json_io import write_json


def _fake_venv(tmp_path: Path, job) -> Venv:
    path = tmp_path / "venv"
    return Venv(path=path, python=path / "bin" / "python", spec=job.runtime.venv)


def _patch_execution(monkeypatch, tmp_path: Path, job) -> list[tuple]:
    class FakeManager:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def ensure(self, env_name: str) -> Venv:
            return _fake_venv(tmp_path, job)

    monkeypatch.setattr(executor, "VenvManager", FakeManager)
    monkeypatch.setattr(executor, "_write_versions", lambda *args: None)
    monkeypatch.setattr(executor, "_probe_environment", lambda *args, **kwargs: (0, False))
    cleanup_calls: list[tuple] = []
    monkeypatch.setattr(
        executor,
        "_cleanup_environment",
        lambda *args: cleanup_calls.append(args),
    )
    return cleanup_calls


def _write_junit(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="1"></testsuite>\n',
        encoding="utf-8",
    )


def _plan_with_allowed_skip(plan, job_id: str, reason: str):
    jobs = tuple(
        replace(job, allowed_skip_reasons=(reason,)) if job.id == job_id else job
        for job in plan.jobs
    )
    return build_execution_plan(
        ci_type=plan.ci_type,
        change_mode=plan.change_mode,
        git_source=plan.git_source,
        jobs=jobs,
    )


def test_execute_pytest_job_writes_passing_result(
    tmp_path: Path, monkeypatch, pr_plan
) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "pytest")
    _patch_execution(monkeypatch, tmp_path, job)
    calls = 0

    def fake_run(command, *, cwd, environment, log_path, timeout_seconds):
        nonlocal calls
        calls += 1
        junit = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml="))
        _write_junit(Path(junit))
        log_path.write_text("pytest output\n", encoding="utf-8")
        return 0, False

    monkeypatch.setattr(executor, "run_stream", fake_run)
    output_dir = tmp_path / "result"
    result = executor.execute(pr_plan, job.id, output_dir=output_dir, root=tmp_path)

    assert result.status == "passed"
    assert result.job_id == job.id
    assert calls == len(job.tasks)
    assert len(result.tasks) == len(job.tasks)
    assert all(task.status == "passed" for task in result.tasks)
    assert sum(
        task.evidence.tests_total
        for task in result.tasks
        if task.evidence is not None
    ) == 2 * len(job.tasks)
    loaded = load_job_result(output_dir / "job-result.json")
    assert loaded.to_dict() == result.to_dict()


def test_execute_integration_job_writes_evidence(
    tmp_path: Path, monkeypatch, pr_plan
) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "integration")
    _patch_execution(monkeypatch, tmp_path, job)
    observed: list[list[str]] = []

    def fake_run(command, *, cwd, environment, log_path, timeout_seconds):
        observed.append(command)
        output = Path(command[command.index("--output-dir") + 1])
        run_dir = output / "case" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "training.log").write_text("integration output\n", encoding="utf-8")
        (run_dir / "actual.json").write_text("{}\n", encoding="utf-8")
        (run_dir / "training" / "checkpoint-4").mkdir(parents=True)
        (run_dir / "initial_checkpoint").mkdir()
        (run_dir / "tensorboard").mkdir()
        return 0, False

    monkeypatch.setattr(executor, "run_stream", fake_run)
    output_dir = tmp_path / "result"
    result = executor.execute(pr_plan, job.id, output_dir=output_dir, root=tmp_path)
    spec = job.tasks[0].spec
    assert isinstance(spec, IntegrationSpec)
    assert result.status == "passed"
    assert result.tasks[0].evidence is None
    assert result.artifact_paths["integration"] == "integration"
    run_dir = output_dir / "integration" / "case" / "run"
    assert (run_dir / "actual.json").is_file()
    assert (run_dir / "training.log").is_file()
    assert not (run_dir / "training").exists()
    assert not (run_dir / "initial_checkpoint").exists()
    assert not (run_dir / "tensorboard").exists()
    assert "--skip-install" in observed[0]
    assert "--forbid-update-golden" in observed[0]
    assert "--test_name" in observed[0]


def test_execute_leaves_provisional_result_if_terminated(
    tmp_path: Path, monkeypatch, pr_plan
) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "pytest")
    _patch_execution(monkeypatch, tmp_path, job)
    output_dir = tmp_path / "result"
    observed: list[tuple[str, str]] = []

    def fake_run(command, *, cwd, environment, log_path, timeout_seconds):
        provisional = load_job_result(output_dir / "job-result.json")
        assert provisional.failure is not None
        observed.append((provisional.status, provisional.failure.code))
        raise SystemExit(137)

    monkeypatch.setattr(executor, "run_stream", fake_run)
    with pytest.raises(SystemExit, match="137"):
        executor.execute(pr_plan, job.id, output_dir=output_dir, root=tmp_path)

    assert observed == [("infra_error", "executor-incomplete")]
    result = load_job_result(output_dir / "job-result.json")
    assert result.status == "infra_error"
    assert result.failure is not None
    assert result.failure.code == "executor-incomplete"
    assert result.tasks == ()


def test_execute_stops_after_first_failed_step(tmp_path: Path, monkeypatch, pr_plan) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "pytest")
    _patch_execution(monkeypatch, tmp_path, job)
    calls = 0

    def fake_run(command, *, cwd, environment, log_path, timeout_seconds):
        nonlocal calls
        calls += 1
        return 1, False

    monkeypatch.setattr(executor, "run_stream", fake_run)
    result = executor.execute(pr_plan, job.id, output_dir=tmp_path / "result", root=tmp_path)

    assert calls == 1
    assert result.status == "failed"
    assert result.failure is None
    assert len(result.tasks) == 1
    assert result.tasks[0].status == "failed"
    assert result.tasks[0].failure is not None
    assert result.tasks[0].failure.code == "task-failed"


def test_execute_records_timeout(tmp_path: Path, monkeypatch, pr_plan) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "pytest")
    _patch_execution(monkeypatch, tmp_path, job)
    monkeypatch.setattr(executor, "run_stream", lambda *args, **kwargs: (124, True))
    result = executor.execute(pr_plan, job.id, output_dir=tmp_path / "result", root=tmp_path)

    assert result.status == "failed"
    assert result.failure is None
    assert result.tasks[0].exit_code == 124
    assert result.tasks[0].failure is not None
    assert result.tasks[0].failure.code == "task-timeout"


def test_environment_failure_still_writes_result(tmp_path: Path, monkeypatch, pr_plan) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "pytest")

    class FakeManager:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def ensure(self, env_name: str) -> Venv:
            raise RuntimeError("venv install failed")

    monkeypatch.setattr(executor, "VenvManager", FakeManager)
    result = executor.execute(pr_plan, job.id, output_dir=tmp_path / "result", root=tmp_path)

    assert result.status == "infra_error"
    assert result.failure is not None
    assert result.failure.code == "environment-setup"
    assert load_job_result(tmp_path / "result" / "job-result.json").status == "infra_error"


def test_record_skip_enforces_allowed_reason(tmp_path: Path, pr_plan) -> None:
    npu = next(
        item for item in pr_plan.jobs
        if item.kind == "pytest" and item.runtime.device == "npu"
    )
    plan = _plan_with_allowed_skip(pr_plan, npu.id, "maintenance")
    result = executor.record_skip(
        plan,
        npu.id,
        "maintenance",
        output_dir=tmp_path / "skip",
    )
    assert result.status == "skipped"
    assert result.skip_reason == "maintenance"
    assert result.tasks == ()

    with pytest.raises(executor.ExecutorError, match="does not allow"):
        executor.record_skip(
            plan,
            npu.id,
            "free-form-reason",
            output_dir=tmp_path / "skip",
        )


def test_integration_command_comes_from_plan(
    tmp_path: Path, pr_plan
) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "integration")
    spec = job.tasks[0].spec
    assert isinstance(spec, IntegrationSpec)
    command = integration_job.command(
        _fake_venv(tmp_path, job),
        spec,
        job,
        job.tasks[0],
        root=tmp_path,
        output_dir=tmp_path / "result",
    )
    assert command[1] == str(tmp_path / spec.runner_script)
    assert command[command.index("--suite") + 1] == spec.suite
    assert command[command.index("--platform") + 1] == job.runtime.device
    assert command[command.index("--ngpu") + 1] == str(job.tasks[0].ranks)
    assert command[command.index("--test_name") + 1] == spec.case_id
    assert command[command.index("--output-dir") + 1] == str(
        tmp_path / "result" / "integration"
    )


def test_npu_job_cleans_up_environment(tmp_path: Path, monkeypatch, pr_plan) -> None:
    job = next(
        item for item in pr_plan.jobs
        if item.runtime.device == "npu" and item.kind == "pytest"
    )
    cleanup_calls = _patch_execution(monkeypatch, tmp_path, job)

    def fake_run(command, *, cwd, environment, log_path, timeout_seconds):
        junit = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml="))
        _write_junit(Path(junit))
        return 0, False

    monkeypatch.setattr(executor, "run_stream", fake_run)
    result = executor.execute(pr_plan, job.id, output_dir=tmp_path / "result", root=tmp_path)

    assert result.status == "passed"
    assert len(cleanup_calls) == 1


def test_timeout_budget_caps_at_job_deadline(monkeypatch) -> None:
    monkeypatch.setattr(process.time, "monotonic", lambda: 100.0)
    assert process.timeout_budget(110.0, 30) == (10, True)
    assert process.timeout_budget(90.0, 30) == (0, True)
    assert process.timeout_budget(200.0, 30) == (30, False)


def test_merge_junit_ignores_truncated_xml(tmp_path: Path) -> None:
    good = tmp_path / "good.xml"
    _write_junit(good)
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite", encoding="utf-8")
    output = tmp_path / "merged.xml"
    pytest_job.merge_reports([bad, good], output)
    assert "<testsuite" in output.read_text(encoding="utf-8")


def test_pytest_command_uses_only_plan_evidence(tmp_path: Path, pr_plan) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "pytest")
    spec = job.tasks[0].spec
    assert isinstance(spec, PytestSpec)
    command = pytest_job.command(
        _fake_venv(tmp_path, job), spec, tmp_path / "junit.xml"
    )
    assert command[-len(spec.test_files):] == list(spec.test_files)
    assert "-m" in command
    assert spec.device_marker in command


def test_cli_record_skip_writes_result_file(tmp_path: Path, pr_plan) -> None:
    plan_path = tmp_path / "plan.json"
    output_path = tmp_path / "artifacts" / "job-result.json"
    summary_path = tmp_path / "summary.md"
    job = next(
        item for item in pr_plan.jobs
        if item.runtime.device == "npu" and item.kind == "pytest"
    )
    plan = _plan_with_allowed_skip(pr_plan, job.id, "maintenance")
    write_json(plan_path, build_planner_output(
        plan=plan,
    ).to_dict())

    assert executor.main([
        "--plan", str(plan_path),
        "--job-id", job.id,
        "--record-skip", "maintenance",
        "--skip-message", "NPU maintenance window",
        "--output", str(output_path),
        "--summary", str(summary_path),
    ]) == 0

    result = load_job_result(output_path)
    assert result.status == "skipped"
    assert result.skip_reason == "maintenance"
    assert result.skip_message == "NPU maintenance window"
    assert (output_path.parent / "job-result.json").is_file()
    assert "## CI Job: skipped" in summary_path.read_text(encoding="utf-8")
