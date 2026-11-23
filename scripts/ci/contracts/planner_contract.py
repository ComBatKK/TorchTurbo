"""Execution plan contracts emitted by the CI planner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..tools.hashing import content_digest
from ..tools.json_io import JsonDecodeError, decode, read_json
from .base import Contract, ContractError, Diagnostic, GitSource, SCHEMA_VERSION
from .image_contract import ResolvedImage

SUITES = ("core", "model", "adapter")
JOB_KINDS = ("pytest", "integration")
DEVICES = ("cpu", "npu", "gpu")
PLANNER_OUTPUT_STATUSES = ("ready", "failed")
CHANGE_MODES = ("code", "docs_only", "image_only", "mixed")


@dataclass(frozen=True)
class VenvSpec(Contract):
    name: str
    python: str
    requirements: tuple[str, ...]
    overrides: tuple[str, ...]
    extends: str | None
    constraints: str


@dataclass(frozen=True)
class Runtime(Contract):
    device: str
    target: str
    ranks: int
    runner: str
    image: ResolvedImage
    venv: VenvSpec


@dataclass(frozen=True)
class PytestSpec(Contract):
    suite: str  # Test suite: core, model, or adapter.
    test_files: tuple[str, ...]  # Test files executed by this step.
    device_marker: str  # Pytest marker expression for the target device.


@dataclass(frozen=True)
class IntegrationSpec(Contract):
    suite: str  # Integration suite name.
    case_id: str  # Integration case identifier.
    framework: str  # Framework used for venv selection.
    runner_script: str  # Script that executes the integration case.
    config_sha256: str  # SHA-256 of the case config file.
    golden_sha256: str  # SHA-256 of the golden file.
    runner_args: tuple[str, ...] = ()  # Extra runner arguments.


@dataclass(frozen=True)
class TestTask(Contract):
    id: str  # Unique test task ID.
    timeout_minutes: int  # Maximum runtime for this task.
    spec: PytestSpec | IntegrationSpec  # Test execution specification.
    kind: str  # Reserved task kind for future scheduling policies.
    ranks: int  # Reserved rank requirement for future task scheduling.


@dataclass(frozen=True)
class Job(Contract):
    id: str  # Unique job ID within the execution plan.
    kind: str  # Job type: pytest or integration.
    allowed_skip_reasons: tuple[str, ...]  # Skip reasons accepted by the gate.
    timeout_minutes: int  # Maximum runtime for the whole job.
    runtime: Runtime  # Shared runner, image, device, and venv configuration.
    tasks: tuple[TestTask, ...]  # Ordered tasks executed inside this job.


@dataclass(frozen=True)
class ExecutionPlan(Contract):
    plan_id: str  # Content-addressed ID of this execution plan.
    ci_type: str  # CI run type: pull_request, nightly, or manual.
    change_mode: str  # Change mode: code, docs_only, image_only, or mixed.
    git_source: GitSource  # Git repository, ref, and commit.
    jobs: tuple[Job, ...]  # Jobs scheduled by this plan.


@dataclass(frozen=True)
class PlannerOutput(Contract):
    schema_version: int  # Contract schema version.
    status: str  # Planner status: ready or failed.
    errors: tuple[Diagnostic, ...] = ()  # Structured planner errors.
    plan: ExecutionPlan | None = None  # Ready plan, absent on failure.


def plan_digest(plan: ExecutionPlan) -> str:
    """Return the digest that must match ``plan.plan_id``."""
    payload = plan.to_dict()
    payload.pop("plan_id")
    return content_digest(payload)


def _fail(path: str, message: str) -> None:
    raise ContractError(f"{path} {message}")


def _validate_plan(plan: ExecutionPlan) -> None:
    expected = plan_digest(plan)
    if plan.plan_id != expected:
        _fail("plan.plan_id", f"does not match contents; expected {expected}")
    if plan.change_mode not in CHANGE_MODES:
        _fail("plan.change_mode", f"has unknown mode {plan.change_mode!r}")
    for index, job in enumerate(plan.jobs):
        if job.kind not in JOB_KINDS:
            _fail(f"plan.jobs[{index}].kind", f"has unknown kind {job.kind!r}")
        if job.timeout_minutes < 1:
            _fail(f"plan.jobs[{index}].timeout_minutes", "must be >= 1")
        if job.runtime.device not in DEVICES:
            _fail(
                f"plan.jobs[{index}].runtime.device",
                f"has unknown device {job.runtime.device!r}",
            )
        if job.runtime.image.target != job.runtime.target:
            _fail(
                f"plan.jobs[{index}].runtime.image.target",
                "must match runtime target",
            )
        for task_index, task in enumerate(job.tasks):
            path = f"plan.jobs[{index}].tasks[{task_index}]"
            if task.kind != job.kind:
                _fail(path, "contains a task with a different kind")
            expected_spec = PytestSpec if task.kind == "pytest" else IntegrationSpec
            if not isinstance(task.spec, expected_spec):
                _fail(f"{path}.spec", f"does not match {task.kind} kind")


def _validate_output(output: PlannerOutput) -> None:
    if output.schema_version != SCHEMA_VERSION:
        _fail(
            "planner_output.schema_version",
            f"is {output.schema_version}, expected {SCHEMA_VERSION}",
        )
    if output.status not in PLANNER_OUTPUT_STATUSES:
        _fail(
            "planner_output.status",
            f"has unknown status {output.status!r}",
        )
    if output.status == "ready" and (output.plan is None or output.errors):
        _fail("planner_output", "status=ready requires a plan and no errors")
    if output.status == "failed" and not output.errors:
        _fail("planner_output", "status=failed requires at least one error")
    if output.plan is not None:
        _validate_plan(output.plan)


def load_planner_output(path: Path) -> PlannerOutput:
    """Load and verify a planner output, including failed plans."""
    try:
        output = decode(
            PlannerOutput,
            read_json(path),
            "planner output",
        )
    except (OSError, JsonDecodeError) as exc:
        raise ContractError(f"cannot read planner output {path}: {exc}") from exc
    _validate_output(output)
    return output
