from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scripts.ci.contracts.base import SCHEMA_VERSION, ContractError
from scripts.ci.contracts.executor_contract import (
    Failure,
    JobResult,
    TaskEvidence,
    TaskResult,
    load_job_result,
)
from scripts.ci.contracts.planner_contract import (
    IntegrationSpec,
    load_planner_output,
)
from scripts.ci.execution.producer.planner import build_planner_output
from scripts.ci.tools.json_io import write_json


def test_plan_round_trip(tmp_path: Path, pr_plan) -> None:
    output = build_planner_output(plan=pr_plan)
    path = tmp_path / "plan.json"
    write_json(path, output.to_dict())
    loaded = load_planner_output(path)
    assert loaded.plan is not None
    assert loaded.plan.to_dict() == pr_plan.to_dict()
    assert loaded.plan.ci_type == "pull_request"


def test_execution_plan_has_only_routing_metadata(pr_plan) -> None:
    assert set(pr_plan.to_dict()) == {
        "plan_id",
        "ci_type",
        "change_mode",
        "git_source",
        "jobs",
    }


def test_planner_output_owns_the_schema_version(pr_plan) -> None:
    output = build_planner_output(plan=pr_plan)

    payload = output.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert "schema_version" not in payload["plan"]
    assert set(payload) == {
        "schema_version",
        "status",
        "errors",
        "plan",
    }


def test_plan_rejects_unknown_field(tmp_path: Path, pr_plan) -> None:
    output = build_planner_output(plan=pr_plan)
    value = output.to_dict()
    value["plan"]["unexpected"] = True
    path = tmp_path / "plan.json"
    write_json(path, value)
    with pytest.raises(ContractError, match="plan has an invalid value"):
        load_planner_output(path)


def test_plan_rejects_tampered_plan_id(tmp_path: Path, pr_plan) -> None:
    broken = replace(pr_plan, plan_id="sha256:bad")
    output = build_planner_output(plan=broken)
    path = tmp_path / "plan.json"
    write_json(path, output.to_dict())
    with pytest.raises(ContractError, match="plan_id does not match"):
        load_planner_output(path)


def _pytest_result(plan) -> JobResult:
    job = next(item for item in plan.jobs if item.kind == "pytest")
    tasks = tuple(
        TaskResult(
            id=task.id,
            status="passed",
            exit_code=0,
            evidence=TaskEvidence(tests_total=1),
        )
        for task in job.tasks
    )
    return JobResult(
        schema_version=SCHEMA_VERSION,
        plan_id=plan.plan_id,
        job_id=job.id,
        kind=job.kind,
        status="passed",
        duration_seconds=60,
        runtime=job.runtime,
        tasks=tasks,
        artifact_paths={"log": "turbo-ci.log", "junit": "pytest.xml"},
    )


def test_job_result_round_trip(tmp_path: Path, pr_plan) -> None:
    result = _pytest_result(pr_plan)
    path = tmp_path / "result.json"
    write_json(path, result.to_dict())
    loaded = load_job_result(path)
    assert loaded.to_dict() == result.to_dict()


def test_result_rejects_unknown_status(tmp_path: Path, pr_plan) -> None:
    value = _pytest_result(pr_plan).to_dict()
    value["status"] = "not_started"
    path = tmp_path / "result.json"
    write_json(path, value)
    with pytest.raises(ContractError, match="unknown status"):
        load_job_result(path)


def test_skipped_result_requires_reason(tmp_path: Path, pr_plan) -> None:
    value = _pytest_result(pr_plan).to_dict()
    value["status"] = "skipped"
    path = tmp_path / "result.json"
    write_json(path, value)
    with pytest.raises(ContractError, match="requires skip.reason"):
        load_job_result(path)


def test_integration_result_requires_case_evidence(tmp_path: Path, pr_plan) -> None:
    job = next(item for item in pr_plan.jobs if item.kind == "integration")
    spec = job.tasks[0].spec
    assert isinstance(spec, IntegrationSpec)
    result = JobResult(
        schema_version=SCHEMA_VERSION,
        plan_id=pr_plan.plan_id,
        job_id=job.id,
        kind=job.kind,
        status="passed",
        duration_seconds=60,
        runtime=job.runtime,
        tasks=(TaskResult(
            id=job.tasks[0].id,
            status="passed",
            exit_code=0,
        ),),
    )
    path = tmp_path / "result.json"
    write_json(path, result.to_dict())
    assert load_job_result(path).to_dict() == result.to_dict()

    bad = result.to_dict()
    bad["tasks"][0]["evidence"] = {"tests_total": -1}
    write_json(path, bad)
    with pytest.raises(ContractError, match="must be >= 0"):
        load_job_result(path)


def test_passed_pytest_task_requires_evidence(
    tmp_path: Path, pr_plan
) -> None:
    broken = _pytest_result(pr_plan).to_dict()
    broken["tasks"][0]["evidence"] = None
    path = tmp_path / "result.json"
    write_json(path, broken)
    with pytest.raises(ContractError, match="passed pytest tasks require evidence"):
        load_job_result(path)


def test_failed_result_supports_structured_failure(
    tmp_path: Path, pr_plan
) -> None:
    result = replace(
        _pytest_result(pr_plan),
        status="failed",
        failure=Failure(code="task-failed", message="exit 1"),
    )
    path = tmp_path / "result.json"
    write_json(path, result.to_dict())
    loaded = load_job_result(path)
    assert loaded.failure is not None
    assert loaded.failure.code == "task-failed"


def test_non_passed_task_requires_failure(tmp_path: Path, pr_plan) -> None:
    value = _pytest_result(pr_plan).to_dict()
    value["tasks"][0]["status"] = "failed"
    value["tasks"][0]["failure"] = None
    path = tmp_path / "result.json"
    write_json(path, value)
    with pytest.raises(ContractError, match="non-passed tasks require failure"):
        load_job_result(path)


def test_failure_requires_code_and_message(tmp_path: Path, pr_plan) -> None:
    value = _pytest_result(pr_plan).to_dict()
    value["tasks"][0]["status"] = "failed"
    value["tasks"][0]["failure"] = {"code": "", "message": "failed"}
    path = tmp_path / "result.json"
    write_json(path, value)
    with pytest.raises(ContractError, match="failure.code must not be empty"):
        load_job_result(path)


def test_result_rejects_non_integer_duration(tmp_path: Path, pr_plan) -> None:
    value = _pytest_result(pr_plan).to_dict()
    value["duration_seconds"] = "60"
    path = tmp_path / "result.json"
    write_json(path, value)
    with pytest.raises(ContractError, match="duration_seconds must be an integer"):
        load_job_result(path)


def test_result_rejects_non_string_artifact_path(
    tmp_path: Path, pr_plan
) -> None:
    value = _pytest_result(pr_plan).to_dict()
    value["artifact_paths"]["junit"] = 1
    path = tmp_path / "result.json"
    write_json(path, value)
    with pytest.raises(ContractError, match="must be a non-empty string"):
        load_job_result(path)
