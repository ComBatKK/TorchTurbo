#!/usr/bin/env python3
"""Derive test facts from pytest markers and integration suite manifests."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import CiConfig
from ...contracts.base import Diagnostic, DiagnosticError
from ...contracts.planner_contract import DEVICES
from ...tools.hashing import file_sha256

PYTEST_ROOT = Path("tests/unit_tests")
MANIFEST_SCHEMA_VERSION = 1
DEVICE_MARKERS = frozenset({"cpu", "npu", "gpu"})
MULTI_MARKERS = {"multi_npu": "npu", "multi_gpu": "gpu"}
REQUIRED_MARKERS = ("cpu", "npu", "multi_npu", "gpu", "multi_gpu", "integration")
FRAMEWORK_PACKAGES = {"llamafactory": "lf", "swift": "swift"}
MODEL_PACKAGES = frozenset({"base", "modules", "registry", "weight_converter"})


class FactsError(DiagnosticError):
    """Raised when repository test facts are missing or inconsistent."""


@dataclass(frozen=True)
class PytestFact:
    path: str
    suite: str
    frameworks: tuple[str, ...]
    model_families: tuple[str, ...]
    devices: tuple[str, ...]
    ranks_by_device: dict[str, int]
    process_group: str


@dataclass(frozen=True)
class IntegrationFact:
    suite: str
    case_id: str
    framework: str
    platform: str
    targets: tuple[str, ...]
    ranks: int
    config_sha256: str
    golden_sha256: str


@dataclass(frozen=True)
class TestFacts:
    __test__ = False

    pytest: tuple[PytestFact, ...]
    integration: tuple[IntegrationFact, ...]


def _issue(path: str, message: str, code: str = "invalid-test-facts") -> Diagnostic:
    return Diagnostic(code=code, location=path, message=message)


def test_parts(path: str) -> tuple[str, ...]:
    parts = Path(path).parts
    if parts[:2] == ("tests", "unit_tests"):
        return parts[2:]
    return parts[1:] if parts[:1] == ("tests",) else parts


def classify_suite(
    path: str, frameworks: Iterable[str], families: Iterable[str]
) -> str:
    framework_set = set(frameworks)
    family_set = set(families)
    parts = test_parts(path)
    if parts[:1] == ("adapters",) or framework_set:
        return "adapter"
    if parts[:1] == ("models",) and len(parts) >= 2:
        if len(parts) == 2 or parts[1] == "modules":
            return "core"
        if parts[1] not in MODEL_PACKAGES:
            return "model"
    return "model" if family_set else "core"


def _marker_names(node: ast.AST) -> set[str]:
    values = node.elts if isinstance(node, (ast.List, ast.Tuple, ast.Set)) else [node]
    names: set[str] = set()
    for value in values:
        if isinstance(value, ast.Call):
            value = value.func
        if isinstance(value, ast.Attribute):
            names.add(value.attr)
    return names


def _module_markers(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is not None and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in targets
        ):
            names |= _marker_names(value)
    return names


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _literal_int(call: ast.Call, name: str) -> int | None:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, int) else None
    return None


def _parametrized_ranks(tree: ast.AST) -> int:
    required = 1
    names = {"world_size", "nprocs", "num_processes", "rank_size"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _dotted_name(
            node.func
        ).rsplit(".", 1)[-1] != "parametrize" or len(node.args) < 2:
            continue
        try:
            parameters = ast.literal_eval(node.args[0])
            values = ast.literal_eval(node.args[1])
        except (ValueError, SyntaxError):
            continue
        if isinstance(parameters, str):
            parameters = [part.strip() for part in parameters.split(",")]
        if not isinstance(parameters, (list, tuple)):
            continue
        indexes = [
            index for index, name in enumerate(parameters)
            if isinstance(name, str) and name in names
        ]
        if not indexes or not isinstance(values, (list, tuple)):
            continue
        for row in values:
            row = row if isinstance(row, (list, tuple)) else (row,)
            for index in indexes:
                if index < len(row) and isinstance(row[index], int):
                    required = max(required, row[index])
    return required


def _ranks(tree: ast.AST) -> int:
    required = 1
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else ""
        )
        if name not in {"spawn", "init_process_group", "require_npus"}:
            continue
        if name == "require_npus":
            value = node.args[0] if node.args else None
            size = value.value if isinstance(value, ast.Constant) else None
        else:
            size = _literal_int(
                node, "world_size" if name == "init_process_group" else "nprocs"
            )
        required = max(required, size if isinstance(size, int) else 2)
    return max(required, _parametrized_ranks(tree))


def _imports(tree: ast.Module) -> tuple[set[str], set[str]]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    frameworks = {
        FRAMEWORK_PACKAGES[name.split(".", 1)[0]]
        for name in modules
        if name.split(".", 1)[0] in FRAMEWORK_PACKAGES
    }
    return modules, frameworks


def _process_group(path: str, families: set[str]) -> str:
    parts = test_parts(path)
    if parts[:1] == ("models",):
        if len(parts) == 2:
            return "layer:tests/models"
        if len(parts) >= 3 and parts[1] not in MODEL_PACKAGES:
            return f"family:{parts[1]}"
    if families:
        return "import:" + ",".join(sorted(families))
    return f"layer:{'/'.join(Path(path).parts[:-1])}"


def _pytest_facts(root: Path) -> tuple[list[PytestFact], list[Diagnostic]]:
    facts: list[PytestFact] = []
    issues: list[Diagnostic] = []
    for path in sorted((root / PYTEST_ROOT).rglob("test_*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError) as exc:
            issues.append(_issue(rel, f"cannot parse test file: {exc}"))
            continue
        module_markers = _module_markers(tree)
        devices: set[str] = set()
        ranks: dict[str, int] = {}
        tests = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        ]
        for test in tests:
            markers = module_markers.copy()
            for decorator in test.decorator_list:
                markers |= _marker_names(decorator)
            test_devices = markers & DEVICE_MARKERS
            multi = {name for name in markers if name in MULTI_MARKERS}
            test_devices |= {MULTI_MARKERS[name] for name in multi}
            test_devices = test_devices or {"cpu"}
            rank = max(_ranks(test), 2 if multi else 1)
            devices |= test_devices
            for device in test_devices:
                ranks[device] = max(ranks.get(device, 1), rank)
        if not tests:
            continue
        if len(devices) == 1:
            device = next(iter(devices))
            ranks[device] = max(ranks.get(device, 1), _ranks(tree))
        modules, frameworks = _imports(tree)
        families = {
            name.split(".")[2] for name in modules
            if name.startswith("torchturbo.models.")
            and len(name.split(".")) > 2
            and name.split(".")[2] not in MODEL_PACKAGES
        }
        parts = test_parts(rel)
        if parts[:2] == ("adapters", "llamafactory_v0"):
            frameworks.add("lf")
        if parts[:2] == ("adapters", "msswift"):
            frameworks.add("swift")
        facts.append(PytestFact(
            path=rel,
            suite=classify_suite(rel, frameworks, families),
            frameworks=tuple(sorted(frameworks)),
            model_families=tuple(sorted(families)),
            devices=tuple(sorted(devices)),
            ranks_by_device={device: ranks[device] for device in sorted(devices)},
            process_group=_process_group(rel, families),
        ))
    return facts, issues


def _manifest(
    root: Path, suite_name: str, runner: str
) -> tuple[dict[str, Any] | None, Diagnostic | None]:
    result = subprocess.run(
        [
            sys.executable,
            str(root / runner),
            "--suite",
            suite_name,
            "--list-cases",
            "--json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return None, _issue(
            runner,
            f"integration suite {suite_name!r} manifest failed with rc="
            f"{result.returncode}: {detail}",
            "integration-manifest",
        )
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        return None, _issue(
            runner,
            f"integration suite {suite_name!r} manifest is not JSON: {exc}",
            "integration-manifest",
        )
    if not isinstance(payload, dict):
        return None, _issue(
            runner,
            f"integration suite {suite_name!r} manifest must be a mapping",
            "integration-manifest",
        )
    return payload, None


def _repo_file(
    root: Path, value: str, where: str, issues: list[Diagnostic]
) -> Path | None:
    if not value:
        issues.append(_issue(where, "path is required"))
        return None
    path = Path(value)
    if path.is_absolute():
        issues.append(_issue(where, "path must be repository-relative"))
        return None
    absolute = (root / path).resolve()
    try:
        absolute.relative_to(root.resolve())
    except ValueError:
        issues.append(_issue(where, "path escapes the repository"))
        return None
    if not absolute.is_file():
        issues.append(_issue(where, f"file is missing: {value}"))
        return None
    return absolute


def _targets(
    item: dict[str, Any],
    platform: str,
    config: CiConfig,
    where: str,
    issues: list[Diagnostic],
) -> tuple[str, ...]:
    single = item.get("target")
    multiple = item.get("targets")
    if single is not None and multiple is not None:
        issues.append(_issue(where, "declare target or targets, not both"))
    if isinstance(single, str) and single:
        values = (single,)
    elif isinstance(multiple, list) and all(
        isinstance(value, str) and value for value in multiple
    ):
        values = tuple(multiple)
    else:
        if multiple is not None:
            issues.append(_issue(where, "targets must be non-empty strings"))
        values = ()
    for name in values:
        target = config.targets.get(name)
        if target is None:
            issues.append(_issue(where, f"unknown target {name!r}"))
        elif target.device != platform:
            issues.append(
                _issue(where, f"target {name!r} is for {target.device}, not {platform}")
            )
    return values


def _integration_facts(
    root: Path, config: CiConfig
) -> tuple[list[IntegrationFact], list[Diagnostic]]:
    issues: list[Diagnostic] = []
    facts: list[IntegrationFact] = []
    seen: set[tuple[str, str]] = set()
    for suite_name, suite in sorted(config.integration_suites.items()):
        payload, error = _manifest(root, suite_name, suite.runner)
        if error is not None:
            issues.append(error)
            continue
        assert payload is not None
        if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            issues.append(_issue(
                suite.runner,
                f"suite {suite_name!r} manifest schema must be "
                f"{MANIFEST_SCHEMA_VERSION}",
                "integration-manifest",
            ))
        if payload.get("suite") != suite_name:
            issues.append(_issue(
                suite.runner,
                f"manifest suite is {payload.get('suite')!r}, expected {suite_name!r}",
                "integration-manifest",
            ))
        cases = payload.get("cases")
        if not isinstance(cases, list):
            issues.append(_issue(
                suite.runner,
                f"suite {suite_name!r} manifest cases must be a list",
                "integration-manifest",
            ))
            continue
        for index, item in enumerate(cases):
            where = f"{suite.runner}.cases[{index}]"
            if not isinstance(item, dict):
                issues.append(_issue(where, "case must be a mapping"))
                continue
            case_id = item.get("case_id")
            if not isinstance(case_id, str) or not case_id:
                issues.append(_issue(f"{where}.case_id", "case_id is required"))
                continue
            key = (suite_name, case_id)
            if key in seen:
                issues.append(_issue(f"{where}.case_id", f"duplicate case {case_id!r}"))
            seen.add(key)
            framework = item.get("framework")
            if framework not in suite.framework_venvs:
                issues.append(_issue(
                    f"{where}.framework",
                    f"framework {framework!r} has no venv mapping",
                ))
                framework = ""
            platform = item.get("platform")
            if platform not in DEVICES:
                issues.append(_issue(f"{where}.platform", "unknown platform"))
                platform = ""
            elif not config.device_is_configured(platform):
                issues.append(
                    _issue(f"{where}.platform", f"no CI target for {platform!r}")
                )
            targets = _targets(
                item, platform, config, f"{where}.targets", issues
            ) if platform else ()
            ranks = item.get("ranks")
            if not isinstance(ranks, int) or isinstance(ranks, bool) or ranks < 1:
                issues.append(_issue(f"{where}.ranks", "ranks must be an integer >= 1"))
                ranks = 1
            if item.get("compare") is not True:
                issues.append(
                    _issue(f"{where}.compare", "CI integration cases must compare")
                )
            if item.get("update_golden") is not False:
                issues.append(
                    _issue(f"{where}.update_golden", "CI must not update golden values")
                )
            config_file = _repo_file(
                root, str(item.get("config") or ""), f"{where}.config", issues
            )
            golden_file = _repo_file(
                root, str(item.get("golden") or ""), f"{where}.golden", issues
            )
            facts.append(IntegrationFact(
                suite=suite_name,
                case_id=case_id,
                framework=framework,
                platform=platform,
                targets=targets,
                ranks=ranks,
                config_sha256=(
                    file_sha256(config_file) if config_file is not None else ""
                ),
                golden_sha256=(
                    file_sha256(golden_file) if golden_file is not None else ""
                ),
            ))
    return facts, issues


def load_facts(root: Path, config: CiConfig) -> TestFacts:
    """Load all configured pytest and integration facts."""
    pytest, pytest_issues = _pytest_facts(root)
    integration, integration_issues = _integration_facts(root, config)
    issues = [*pytest_issues, *integration_issues]
    if not pytest:
        issues.append(_issue(PYTEST_ROOT.as_posix(), "no pytest facts found"))
    if issues:
        raise FactsError(issues)
    return TestFacts(
        pytest=tuple(pytest),
        integration=tuple(
            sorted(integration, key=lambda item: (item.suite, item.case_id))
        ),
    )
