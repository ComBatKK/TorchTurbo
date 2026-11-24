#!/usr/bin/env python3
"""Finalize an ExecutionPlan against JobResults and write a GateDecision."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ...contracts.base import Contract, ContractError, SCHEMA_VERSION
from ...contracts.executor_contract import (
    JOB_RESULT_FILENAME,
    JobResult,
    TaskResult,
    load_job_result,
)
from ...contracts.planner_contract import (
    ExecutionPlan,
    Job,
    PlannerOutput,
    PytestSpec,
    load_planner_output,
)
from ...tools.json_io import canonical_json, write_json


@dataclass(frozen=True)
class GateDecision(Contract):
    schema_version: int
    plan_id: str
    status: str
    summary: dict[str, int]
    checks: tuple[dict[str, Any], ...]
    failures: tuple[dict[str, Any], ...]


def _failure(
    code: str,
    detail: str,
    *,
    job_id: str = "",
    task_id: str = "",
    path: str = "",
) -> dict[str, Any]:
    value: dict[str, Any] = {"code": code, "detail": detail}
    if job_id:
        value["job_id"] = job_id
    if task_id:
        value["task_id"] = task_id
    if path:
        value["path"] = path
    return value


def _plan_failures(plan: ExecutionPlan) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    test_routes: set[tuple[str, str, str]] = set()
    for job in plan.jobs:
        for task in job.tasks:
            if not isinstance(task.spec, PytestSpec):
                continue
            for path in task.spec.test_files:
                route = (path, job.runtime.target, job.runtime.device)
                if route in test_routes:
                    failures.append(_failure(
                        "duplicate-route",
                        f"duplicate route {route}",
                        task_id=task.id,
                    ))
                test_routes.add(route)
    return failures


def _task_results(
    job: Job, result: JobResult
) -> tuple[dict[str, TaskResult], set[str], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    expected = {task.id for task in job.tasks}
    actual: dict[str, TaskResult] = {}
    for task in result.tasks:
        if task.id in actual:
            failures.append(_failure(
                "duplicate-task-result",
                "duplicate task result",
                job_id=job.id,
                task_id=task.id,
            ))
        elif task.id not in expected:
            failures.append(_failure(
                "unknown-task-result",
                "task result is not in plan",
                job_id=job.id,
                task_id=task.id,
            ))
        actual[task.id] = task
    return actual, expected, failures


def _passed_failures(
    job: Job,
    result: JobResult,
    actual: dict[str, TaskResult],
    expected: set[str],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if set(actual) != expected:
        failures.append(_failure(
            "result-tasks",
            "passed result does not cover all planned tasks",
            job_id=job.id,
        ))
    if any(task.status != "passed" or task.exit_code != 0 for task in actual.values()):
        failures.append(_failure(
            "result-task-status",
            "passed job has a non-passing task",
            job_id=job.id,
        ))
    if job.kind == "pytest":
        for task in actual.values():
            evidence = task.evidence
            if evidence is None:
                failures.append(_failure(
                    "pytest-no-evidence",
                    "pytest task has no execution evidence",
                    job_id=job.id,
                    task_id=task.id,
                ))
                continue
            if evidence.tests_total == 0:
                failures.append(_failure(
                    "pytest-no-tests",
                    "pytest collected no tests",
                    job_id=job.id,
                    task_id=task.id,
                ))
            if evidence.tests_failed or evidence.tests_errors:
                failures.append(_failure(
                    "pytest-failures",
                    "pytest evidence contains failures or errors",
                    job_id=job.id,
                    task_id=task.id,
                ))
    elif job.kind == "integration" and not result.artifact_paths.get("integration"):
        failures.append(_failure(
            "integration-artifact",
            "integration result has no artifact directory",
            job_id=job.id,
        ))
    return failures


def _result_failures(
    plan: ExecutionPlan, job: Job, result: JobResult
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if result.plan_id != plan.plan_id:
        failures.append(_failure("result-plan-id", "result plan_id differs from Plan", job_id=job.id))
    if result.kind != job.kind:
        failures.append(_failure("result-kind", "result kind differs from Plan", job_id=job.id))
    if result.runtime.to_dict() != job.runtime.to_dict():
        failures.append(_failure("result-runtime", "result runtime differs from Plan", job_id=job.id))
    actual, expected, task_failures = _task_results(job, result)
    failures.extend(task_failures)
    if result.status == "passed":
        failures.extend(_passed_failures(job, result, actual, expected))
    elif result.status == "skipped":
        if result.skip_reason not in job.allowed_skip_reasons:
            failures.append(_failure("skip-reason", "skip reason is not allowed by Plan", job_id=job.id))
        if result.tasks:
            failures.append(_failure(
                "skip-tasks",
                "skipped result contains task results",
                job_id=job.id,
            ))
    elif result.status == "failed":
        if result.failure is None and not any(
            task.status == "failed" and task.failure is not None
            for task in result.tasks
        ):
            failures.append(_failure("failed-detail", "failed result has no failure evidence", job_id=job.id))
    return failures


def _check(identifier: str, failures: Iterable[dict[str, Any]], detail: str = "") -> dict[str, Any]:
    values = list(failures)
    value: dict[str, Any] = {
        "id": identifier,
        "status": "failed" if values else "passed",
    }
    if values:
        value["detail"] = detail or "; ".join(item["detail"] for item in values[:3])
    return value


def decide_plan(
    plan: ExecutionPlan, results: list[JobResult]
) -> GateDecision:
    """Judge one ExecutionPlan and its JobResults."""
    failures: list[dict[str, Any]] = []
    plan_failures = _plan_failures(plan)
    failures.extend(plan_failures)

    by_id: dict[str, list[JobResult]] = {}
    for result in results:
        by_id.setdefault(result.job_id, []).append(result)
    expected = {job.id: job for job in plan.jobs}
    for job_id, values in sorted(by_id.items()):
        if len(values) > 1:
            failures.append(_failure("duplicate-result", "multiple results for one job", job_id=job_id))
        if job_id not in expected:
            failures.append(_failure("unexpected-result", "result job is not in Plan", job_id=job_id))

    missing = sorted(set(expected) - set(by_id))
    for job_id in missing:
        failures.append(_failure("missing-result", "planned job has no result", job_id=job_id))

    job_contract_failures: list[dict[str, Any]] = []
    status_failures: list[dict[str, Any]] = []
    for job_id in sorted(set(expected) & set(by_id)):
        values = by_id[job_id]
        if len(values) != 1:
            continue
        job = expected[job_id]
        result = values[0]
        current = _result_failures(plan, job, result)
        job_contract_failures.extend(
            item for item in current
            if item["code"] not in {"pytest-no-tests", "pytest-failures"}
        )
        status_failures.extend(
            item for item in current
            if item["code"] in {"pytest-no-tests", "pytest-failures"}
        )
        if result.status in {"failed", "infra_error", "cancelled"}:
            status_failures.append(_failure(
                f"job-{result.status}",
                f"job finished with status {result.status}",
                job_id=job_id,
            ))

    failures.extend(job_contract_failures)
    failures.extend(status_failures)
    status_counts = Counter(
        values[0].status for job_id, values in by_id.items()
        if job_id in expected and len(values) == 1
    )
    summary = {
        "expected_jobs": len(plan.jobs),
        "reported_jobs": len(results),
        "passed": status_counts["passed"],
        "failed": status_counts["failed"] + status_counts["infra_error"] + status_counts["cancelled"],
        "skipped": status_counts["skipped"],
        "missing": len(missing),
        "unexpected": sum(1 for job_id in by_id if job_id not in expected),
    }
    checks = (
        _check("plan.contract", plan_failures, "Plan structure is invalid"),
        _check(
            "results.complete",
            [
                item for item in failures
                if item["code"] in {
                    "missing-result", "unexpected-result", "duplicate-result",
                    "result-plan-id",
                }
            ],
            "Result set is incomplete or inconsistent",
        ),
        _check("jobs.contract", job_contract_failures, "Job results do not match Plan"),
        _check("jobs.status", status_failures, "One or more jobs did not pass"),
    )
    return GateDecision(
        schema_version=SCHEMA_VERSION,
        plan_id=plan.plan_id,
        status="passed" if not failures else "failed",
        summary=summary,
        checks=checks,
        failures=tuple(failures),
    )


def decide_planner_output(output: PlannerOutput) -> GateDecision:
    """Turn a failed PlannerOutput into a failed GateDecision."""
    if output.status == "ready" and output.plan is not None:
        return decide_plan(output.plan, [])
    failures = tuple(
        _failure(
            f"planner-output-{issue.code}",
            issue.message,
            path=issue.location,
        )
        for issue in output.errors
    )
    if not failures:
        failures = (
            _failure(
                "planner-output-not-ready",
                f"planner output status is {output.status!r}, not 'ready'",
            ),
        )
    return GateDecision(
        schema_version=SCHEMA_VERSION,
        plan_id=output.plan.plan_id if output.plan is not None else "",
        status="failed",
        summary={
            "expected_jobs": 0,
            "reported_jobs": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "missing": 0,
            "unexpected": 0,
        },
        checks=({
            "id": "planner.output",
            "status": "failed",
            "detail": "Planner did not produce a ready ExecutionPlan",
        },),
        failures=failures,
    )


def _load_results(path: Path | None) -> list[JobResult]:
    if path is None:
        return []
    if path.is_file():
        return [load_job_result(path)]
    if path.is_dir():
        return [
            load_job_result(item)
            for item in sorted(path.rglob(JOB_RESULT_FILENAME))
        ]
    raise ContractError(f"results path does not exist: {path}")


def render_summary(decision: GateDecision) -> str:
    lines = [
        f"## CI Gate: {decision.status}",
        "",
        "| expected | reported | passed | failed | skipped | missing | unexpected |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        "| {expected_jobs} | {reported_jobs} | {passed} | {failed} | {skipped} | "
        "{missing} | {unexpected} |".format(**decision.summary),
    ]
    if decision.failures:
        lines += ["", "### Failures", ""]
        for failure in decision.failures:
            target = (
                failure.get("job_id")
                or failure.get("task_id")
                or failure.get("path")
                or ""
            )
            prefix = f"`{target}`: " if target else ""
            lines.append(f"- `{failure['code']}`: {prefix}{failure['detail']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        required=True,
        help="ExecutionPlan or ready PlannerOutput JSON",
    )
    parser.add_argument("--results", default="", help="JobResult file or directory")
    parser.add_argument("--output", default="")
    parser.add_argument("--summary", default="")
    args = parser.parse_args(argv)
    try:
        output = load_planner_output(Path(args.plan))
        if output.status != "ready":
            decision = decide_planner_output(output)
        else:
            if output.plan is None:
                raise ContractError("ready planner output has no plan")
            decision = decide_plan(
                output.plan,
                _load_results(Path(args.results) if args.results else None),
            )
    except (ContractError, OSError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    if args.output:
        write_json(Path(args.output), decision.to_dict())
    else:
        print(canonical_json(decision.to_dict()))
    if args.summary:
        with Path(args.summary).open("a", encoding="utf-8") as output:
            output.write(render_summary(decision))
    return 0 if decision.status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
