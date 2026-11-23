#!/usr/bin/env python3
"""Load and validate the single CI configuration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import yaml

from ...contracts.base import Diagnostic
from ...contracts.image_contract import ImageBinding
from ...contracts.planner_contract import DEVICES, SUITES, VenvSpec

CONFIG_PATH = Path("scripts/ci/config/ci.yml")
SCHEMA_VERSION = 5
SCOPES = ("pull_request", "nightly")


class CiConfigError(ValueError):
    """Raised when CI configuration or a cross-reference is invalid."""

    def __init__(self, issues: Iterable[Diagnostic]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(issue.message for issue in self.issues))


@dataclass(frozen=True)
class CiTarget:
    name: str
    device: str
    hardware: str


@dataclass(frozen=True)
class CiImage:
    name: str
    target: str
    kind: str
    ref: str

    def as_ref(self) -> ImageBinding:
        return ImageBinding(
            image_id=self.name,
            target=self.target,
            kind=self.kind,
            ref=self.ref,
        )


@dataclass(frozen=True)
class CiRunner:
    target: str
    rank_capacity: int
    runner_variable: str
    runner: str
    container_options: str = "--user root"

    @property
    def name(self) -> str:
        return f"{self.target}/{self.rank_capacity}"


@dataclass(frozen=True)
class PytestAreas:
    job_timeout_minutes: dict[str, int]
    step_timeout_minutes: dict[str, int]
    model_venvs: dict[str, str]
    framework_venvs: dict[str, str]
    combo_venvs: dict[str, str]
    overrides: dict[str, str]


@dataclass(frozen=True)
class IntegrationOverride:
    job_timeout_minutes: int | None = None
    step_timeout_minutes: int | None = None


@dataclass(frozen=True)
class IntegrationSuite:
    name: str
    runner: str
    job_timeout_minutes: dict[str, int]
    step_timeout_minutes: dict[str, int]
    framework_venvs: dict[str, str]
    overrides: dict[str, IntegrationOverride]

    def job_timeout_minutes_for(self, device: str, case_id: str) -> int:
        override = self.overrides.get(case_id)
        if override is not None and override.job_timeout_minutes is not None:
            return override.job_timeout_minutes
        return self.job_timeout_minutes[device]

    def step_timeout_minutes_for(self, device: str, case_id: str) -> int:
        override = self.overrides.get(case_id)
        if override is not None and override.step_timeout_minutes is not None:
            return override.step_timeout_minutes
        return self.step_timeout_minutes[device]


@dataclass(frozen=True)
class IntegrationAreas:
    suites: dict[str, IntegrationSuite]


@dataclass(frozen=True)
class ScopeSelection:
    image: str
    test_areas: tuple[str, ...] | None

    def selects_pytest(self, suite: str) -> bool:
        return self._selects(f"pytest/{suite}")

    def selects_integration(self, suite: str, case_id: str) -> bool:
        return self._selects(f"integration/{suite}/{case_id}")

    def _selects(self, area_id: str) -> bool:
        return self.test_areas is None or area_id in self.test_areas


@dataclass(frozen=True)
class Scope:
    name: str
    selections: tuple[ScopeSelection, ...]


@dataclass(frozen=True)
class CiConfig:
    schema_version: int
    root: Path
    targets: dict[str, CiTarget]
    images: dict[str, CiImage]
    venvs: dict[str, VenvSpec]
    runners: tuple[CiRunner, ...]
    pytest_areas: PytestAreas
    integration_areas: IntegrationAreas
    scopes: dict[str, Scope]

    def target_for(self, name: str) -> CiTarget:
        target = self.targets.get(name)
        if target is None:
            raise CiConfigError([_issue(name, f"unknown target {name!r}")])
        return target

    def image_for(self, name: str) -> CiImage:
        image = self.images.get(name)
        if image is None:
            raise CiConfigError([_issue(name, f"unknown image {name!r}")])
        return image

    def runner_for(self, target: str, ranks: int) -> CiRunner:
        candidates = [
            runner for runner in self.runners
            if runner.target == target and runner.rank_capacity >= ranks
        ]
        if not candidates:
            raise CiConfigError([_issue(
                target,
                f"target {target!r} has no runner with rank_capacity >= {ranks}",
            )])
        return min(candidates, key=lambda runner: runner.rank_capacity)

    def runner_named(self, name: str) -> CiRunner:
        for runner in self.runners:
            if runner.name == name:
                return runner
        raise CiConfigError([_issue(name, f"unknown runner {name!r}")])

    def scope(self, name: str) -> Scope:
        scope = self.scopes.get(name)
        if scope is None:
            raise CiConfigError([_issue(name, f"unknown CI scope {name!r}")])
        return scope

    def device_is_configured(self, device: str) -> bool:
        return any(target.device == device for target in self.targets.values())

    def integration_suite(self, name: str) -> IntegrationSuite:
        suite = self.integration_areas.suites.get(name)
        if suite is None:
            raise CiConfigError([_issue(name, f"unknown integration suite {name!r}")])
        return suite

    def resolve_pytest_venv(
        self, path: str, families: Iterable[str], frameworks: Iterable[str]
    ) -> tuple[str, list[Diagnostic]]:
        override = self.pytest_areas.overrides.get(path)
        if override is not None:
            return override, []
        family_set = set(families)
        framework_set = set(frameworks)
        if len(framework_set) > 1:
            return "", [_issue(
                path,
                "a multi-framework test needs an explicit override",
                "multi-framework-venv",
            )]
        family_venvs = {
            self.pytest_areas.model_venvs.get(family, "default")
            for family in family_set
        }
        if len(family_venvs) > 1:
            return "", [_issue(
                path,
                "a test cannot use multiple model-family venvs",
                "multi-family-venv",
            )]
        family_venv = next(iter(family_venvs), "default")
        framework = next(iter(framework_set), "")
        if family_set and framework and family_venv != "default":
            family = next(iter(family_set))
            combo = self.pytest_areas.combo_venvs.get(f"{family}+{framework}")
            if combo is None:
                return "", [_issue(
                    path,
                    f"no venv for family={family} framework={framework}",
                    "missing-combo-venv",
                )]
            return combo, []
        if framework:
            venv = self.pytest_areas.framework_venvs.get(framework)
            if venv is None:
                return "", [_issue(
                    path,
                    f"no venv for framework={framework}",
                    "missing-framework-venv",
                )]
            return venv, []
        return family_venv, []


def _issue(path: str, message: str, code: str = "invalid-ci-config") -> Diagnostic:
    return Diagnostic(code=code, location=path, message=message)


def _map(value: Any, path: str, issues: list[Diagnostic]) -> dict[str, Any]:
    if not isinstance(value, dict):
        issues.append(_issue(path, "must be a mapping"))
        return {}
    return value


def _row(
    value: Any, path: str, allowed: set[str], issues: list[Diagnostic]
) -> dict[str, Any]:
    row = _map(value, path, issues)
    for key in sorted(set(row) - allowed):
        issues.append(_issue(f"{path}.{key}", "unknown field"))
    return row


def _strings(value: Any, path: str, issues: list[Diagnostic]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    issues.append(_issue(path, "must be a string or list of strings"))
    return ()


def _int(value: Any, path: str, issues: list[Diagnostic], minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        issues.append(_issue(path, f"must be an integer >= {minimum}"))
        return minimum
    return value


def _timeouts(value: Any, path: str, issues: list[Diagnostic]) -> dict[str, int]:
    result: dict[str, int] = {}
    for device, timeout in _map(value, path, issues).items():
        if device not in DEVICES:
            issues.append(_issue(f"{path}.{device}", "unknown device"))
        else:
            result[device] = _int(timeout, f"{path}.{device}", issues, 1)
    return result


def _load_targets(raw: Any, issues: list[Diagnostic]) -> dict[str, CiTarget]:
    targets: dict[str, CiTarget] = {}
    for name, value in _map(raw, "resources.targets", issues).items():
        path = f"resources.targets.{name}"
        row = _row(value, path, {"device", "hardware"}, issues)
        device = str(row.get("device") or "")
        hardware = str(row.get("hardware") or "")
        if device not in DEVICES:
            issues.append(_issue(f"{path}.device", "unknown device"))
        if device != "cpu" and not hardware:
            issues.append(_issue(f"{path}.hardware", "required for accelerator targets"))
        targets[name] = CiTarget(name, device, hardware)
    if not targets:
        issues.append(_issue("resources.targets", "at least one target is required"))
    return targets


def _load_images(
    raw: Any,
    targets: dict[str, CiTarget],
    image_refs: dict[str, str],
    issues: list[Diagnostic],
) -> dict[str, CiImage]:
    images: dict[str, CiImage] = {}
    for name, value in _map(raw, "resources.images", issues).items():
        path = f"resources.images.{name}"
        row = _row(value, path, {"target", "kind", "ref", "build", "publish"}, issues)
        target = str(row.get("target") or "")
        kind = str(row.get("kind") or "")
        ref = str(image_refs.get(name) or row.get("ref") or "")
        if target not in targets:
            issues.append(_issue(f"{path}.target", "unknown target"))
        if kind not in {"host", "container"}:
            issues.append(_issue(f"{path}.kind", "must be host or container"))
        if kind == "container" and row.get("ref"):
            issues.append(_issue(f"{path}.ref", "container ref belongs to the image domain"))
        if kind == "container" and ref:
            _, separator, digest = ref.rpartition("@")
            if not separator or not digest.startswith("sha256:"):
                issues.append(_issue(f"{path}.ref", "must be digest-pinned"))
        if kind == "host" and not ref:
            issues.append(_issue(f"{path}.ref", "image ref is required"))
        images[name] = CiImage(name, target, kind, ref)
    for name in sorted(set(image_refs) - set(images)):
        issues.append(_issue(f"resources.images.{name}", "no image definition"))
    return images


def _load_venvs(
    raw: Any, root: Path, issues: list[Diagnostic]
) -> dict[str, VenvSpec]:
    local: dict[str, VenvSpec] = {}
    for name, value in _map(raw, "resources.venvs", issues).items():
        path = f"resources.venvs.{name}"
        row = _row(
            value,
            path,
            {"python", "requirements", "overrides", "extends", "constraints"},
            issues,
        )
        requirements = _strings(row.get("requirements"), f"{path}.requirements", issues)
        overrides = _strings(row.get("overrides"), f"{path}.overrides", issues)
        for relative in (*requirements, *overrides):
            if not (root / relative).is_file():
                issues.append(_issue(path, f"missing file {relative!r}"))
        local[name] = VenvSpec(
            name=name,
            python=str(row.get("python") or ""),
            requirements=requirements,
            overrides=overrides,
            extends=str(row.get("extends") or "") or None,
            constraints=str(row.get("constraints") or "auto"),
        )
        if not local[name].python:
            issues.append(_issue(f"{path}.python", "python version is required"))
    if "default" not in local:
        issues.append(_issue("resources.venvs", "default venv is required"))
    for venv in local.values():
        if venv.extends and venv.extends not in local:
            issues.append(_issue(f"resources.venvs.{venv.name}.extends", "unknown venv"))

    resolved: dict[str, VenvSpec] = {}
    resolving: set[str] = set()

    def resolve(name: str) -> VenvSpec:
        if name in resolved:
            return resolved[name]
        if name in resolving:
            issues.append(_issue(f"resources.venvs.{name}.extends", "environment cycle"))
            return local[name]
        resolving.add(name)
        spec = local[name]
        base = resolve(spec.extends) if spec.extends in local else None
        result = spec if base is None else replace(
            spec,
            requirements=tuple(dict.fromkeys((*base.requirements, *spec.requirements))),
            overrides=tuple(dict.fromkeys((*base.overrides, *spec.overrides))),
        )
        resolving.remove(name)
        resolved[name] = result
        return result

    for name in local:
        resolve(name)
    return resolved


def _load_runners(
    raw: Any, targets: dict[str, CiTarget], issues: list[Diagnostic]
) -> tuple[CiRunner, ...]:
    if not isinstance(raw, list):
        issues.append(_issue("resources.runners", "must be a list"))
        return ()
    runners: list[CiRunner] = []
    seen: set[tuple[str, int]] = set()
    for index, value in enumerate(raw):
        path = f"resources.runners[{index}]"
        row = _row(
            value, path, {"target", "rank_capacity", "runs_on", "container_options"}, issues
        )
        target = str(row.get("target") or "")
        capacity = _int(row.get("rank_capacity"), f"{path}.rank_capacity", issues, 1)
        runs_on = _row(
            row.get("runs_on"), f"{path}.runs_on", {"variable", "fallback"}, issues
        )
        variable = str(runs_on.get("variable") or "")
        fallback = str(runs_on.get("fallback") or "")
        if target not in targets:
            issues.append(_issue(f"{path}.target", "unknown target"))
        if not variable or not fallback:
            issues.append(_issue(f"{path}.runs_on", "variable and fallback are required"))
        if (target, capacity) in seen:
            issues.append(_issue(path, "duplicate target and rank_capacity"))
        seen.add((target, capacity))
        runners.append(CiRunner(
            target,
            capacity,
            variable,
            fallback,
            str(row.get("container_options") or "--user root"),
        ))
    if not runners:
        issues.append(_issue("resources.runners", "at least one runner is required"))
    for target in targets:
        if not any(runner.target == target for runner in runners):
            issues.append(_issue("resources.runners", f"target {target} has no runner"))
    return tuple(sorted(runners, key=lambda item: (item.target, item.rank_capacity)))


def _load_pytest(
    raw: Any, venvs: dict[str, VenvSpec], root: Path, issues: list[Diagnostic]
) -> PytestAreas:
    row = _row(
        raw,
        "test_areas.pytest",
        {"defaults", "model_venvs", "framework_venvs", "combo_venvs", "overrides"},
        issues,
    )
    defaults = _row(
        row.get("defaults"),
        "test_areas.pytest.defaults",
        {"job_timeout_minutes", "step_timeout_minutes"},
        issues,
    )
    model = _map(row.get("model_venvs"), "test_areas.pytest.model_venvs", issues)
    framework = _map(
        row.get("framework_venvs"), "test_areas.pytest.framework_venvs", issues
    )
    combo = _map(row.get("combo_venvs"), "test_areas.pytest.combo_venvs", issues)
    for key, value in (*model.items(), *framework.items(), *combo.items()):
        if str(value) not in venvs:
            issues.append(_issue(f"test_areas.pytest.{key}", f"unknown venv {value!r}"))
    overrides: dict[str, str] = {}
    for path, value in _map(
        row.get("overrides"), "test_areas.pytest.overrides", issues
    ).items():
        where = f"test_areas.pytest.overrides.{path}"
        override = _row(value, where, {"venv"}, issues)
        venv = str(override.get("venv") or "")
        if venv not in venvs:
            issues.append(_issue(f"{where}.venv", "unknown venv"))
        if not (root / path).is_file():
            issues.append(_issue(where, "test file is missing"))
        overrides[path] = venv
    return PytestAreas(
        job_timeout_minutes=_timeouts(
            defaults.get("job_timeout_minutes"),
            "test_areas.pytest.defaults.job_timeout_minutes",
            issues,
        ),
        step_timeout_minutes=_timeouts(
            defaults.get("step_timeout_minutes"),
            "test_areas.pytest.defaults.step_timeout_minutes",
            issues,
        ),
        model_venvs={key: str(value) for key, value in model.items()},
        framework_venvs={key: str(value) for key, value in framework.items()},
        combo_venvs={key: str(value) for key, value in combo.items()},
        overrides=overrides,
    )


def _load_integration(
    raw: Any, venvs: dict[str, VenvSpec], root: Path, issues: list[Diagnostic]
) -> IntegrationAreas:
    row = _row(raw, "test_areas.integration", {"suites"}, issues)
    suites: dict[str, IntegrationSuite] = {}
    for name, value in _map(
        row.get("suites"), "test_areas.integration.suites", issues
    ).items():
        path = f"test_areas.integration.suites.{name}"
        suite = _row(
            value, path, {"runner", "defaults", "framework_venvs", "overrides"}, issues
        )
        runner = str(suite.get("runner") or "")
        if not runner or not (root / runner).is_file():
            issues.append(_issue(f"{path}.runner", "runner script is missing"))
        defaults = _row(
            suite.get("defaults"),
            f"{path}.defaults",
            {"job_timeout_minutes", "step_timeout_minutes"},
            issues,
        )
        frameworks = _map(
            suite.get("framework_venvs"), f"{path}.framework_venvs", issues
        )
        for framework, venv in frameworks.items():
            if str(venv) not in venvs:
                issues.append(_issue(
                    f"{path}.framework_venvs.{framework}", f"unknown venv {venv!r}"
                ))
        overrides: dict[str, IntegrationOverride] = {}
        for case_id, item in _map(
            suite.get("overrides"), f"{path}.overrides", issues
        ).items():
            where = f"{path}.overrides.{case_id}"
            override = _row(
                item, where, {"job_timeout_minutes", "step_timeout_minutes"}, issues
            )
            job = override.get("job_timeout_minutes")
            step = override.get("step_timeout_minutes")
            overrides[case_id] = IntegrationOverride(
                job_timeout_minutes=(
                    _int(job, f"{where}.job_timeout_minutes", issues, 1)
                    if job is not None else None
                ),
                step_timeout_minutes=(
                    _int(step, f"{where}.step_timeout_minutes", issues, 1)
                    if step is not None else None
                ),
            )
        suites[name] = IntegrationSuite(
            name=name,
            runner=runner,
            job_timeout_minutes=_timeouts(
                defaults.get("job_timeout_minutes"), f"{path}.defaults.job_timeout_minutes", issues
            ),
            step_timeout_minutes=_timeouts(
                defaults.get("step_timeout_minutes"), f"{path}.defaults.step_timeout_minutes", issues
            ),
            framework_venvs={key: str(venv) for key, venv in frameworks.items()},
            overrides=overrides,
        )
    return IntegrationAreas(suites=suites)


def _test_areas(
    raw: Any,
    path: str,
    suites: set[str],
    issues: list[Diagnostic],
) -> tuple[str, ...] | None:
    if raw is None:
        return None
    values = _strings(raw, path, issues)
    if not values:
        issues.append(_issue(path, "must not be empty"))
        return ()
    if len(set(values)) != len(values):
        issues.append(_issue(path, "contains duplicate TestAreas"))
    for value in values:
        if value.startswith("pytest/"):
            if value.removeprefix("pytest/") not in SUITES:
                issues.append(_issue(path, f"unknown TestArea {value!r}"))
        elif value.startswith("integration/"):
            parts = value.split("/", 2)
            if len(parts) != 3 or not parts[2] or parts[1] not in suites:
                issues.append(_issue(path, f"unknown TestArea {value!r}"))
        else:
            issues.append(_issue(path, f"unknown TestArea {value!r}"))
    return values


def _load_scopes(
    raw: Any,
    images: dict[str, CiImage],
    suites: set[str],
    issues: list[Diagnostic],
) -> dict[str, Scope]:
    scopes: dict[str, Scope] = {}
    for name, value in _map(raw, "scopes", issues).items():
        path = f"scopes.{name}"
        if not isinstance(value, list):
            issues.append(_issue(path, "must be a list of selections"))
            value = []
        if not value:
            issues.append(_issue(path, "at least one selection is required"))
        selections: list[ScopeSelection] = []
        seen: set[str] = set()
        for index, item in enumerate(value):
            where = f"{path}[{index}]"
            selection = _row(item, where, {"environment", "test_areas"}, issues)
            environment = _row(
                selection.get("environment"), f"{where}.environment", {"image"}, issues
            )
            image = str(environment.get("image") or "")
            if image not in images:
                issues.append(_issue(f"{where}.environment.image", "unknown image"))
            if image in seen:
                issues.append(_issue(where, "duplicate image selection"))
            seen.add(image)
            selections.append(ScopeSelection(
                image,
                _test_areas(selection.get("test_areas"), f"{where}.test_areas", suites, issues),
            ))
        scopes[name] = Scope(name, tuple(selections))
    for name in SCOPES:
        if name not in scopes:
            issues.append(_issue("scopes", f"required scope {name!r} is missing"))
    return scopes


def load(root: Path, *, image_refs: dict[str, str] | None = None) -> CiConfig:
    """Load ci.yml and validate all configuration-local cross references."""
    issues: list[Diagnostic] = []
    path = root / CONFIG_PATH
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CiConfigError([_issue(CONFIG_PATH.as_posix(), str(exc))]) from exc
    document = _row(
        document, "ci", {"schema_version", "resources", "test_areas", "scopes"}, issues
    )
    if document.get("schema_version") != SCHEMA_VERSION:
        issues.append(_issue(
            "schema_version", f"expected {SCHEMA_VERSION}", "unsupported-schema"
        ))
    resources = _row(
        document.get("resources"),
        "resources",
        {"targets", "images", "runners", "venvs"},
        issues,
    )
    targets = _load_targets(resources.get("targets"), issues)
    images = _load_images(resources.get("images"), targets, image_refs or {}, issues)
    venvs = _load_venvs(resources.get("venvs"), root, issues)
    runners = _load_runners(resources.get("runners"), targets, issues)
    test_areas = _row(
        document.get("test_areas"), "test_areas", {"pytest", "integration"}, issues
    )
    pytest_areas = _load_pytest(test_areas.get("pytest"), venvs, root, issues)
    integration_areas = _load_integration(
        test_areas.get("integration"), venvs, root, issues
    )
    scopes = _load_scopes(
        document.get("scopes"), images, set(integration_areas.suites), issues
    )
    if issues:
        raise CiConfigError(issues)
    return CiConfig(
        schema_version=SCHEMA_VERSION,
        root=root,
        targets=targets,
        images=images,
        venvs=venvs,
        runners=runners,
        pytest_areas=pytest_areas,
        integration_areas=integration_areas,
        scopes=scopes,
    )
