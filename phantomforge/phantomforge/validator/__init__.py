"""
Validation orchestrator. `pf validate` calls validate_org(); the compiler
calls validate_org() before generating anything (section 8 of the spec:
"validate(org_spec) # abort if it fails").
"""

from __future__ import annotations

from pathlib import Path

from ..spec.loader import OrgSpecError, load_org_yaml
from ..spec.model import OrgSpec
from .budgets import check_memory_budget, check_soul_budget
from .graph import EscalationCycleError, check_no_cycles
from .refs import check_references, suggest_role_level_exceptions


class ValidationResult:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_org(org_yaml_path: str | Path) -> tuple[OrgSpec, ValidationResult]:
    """
    Loads and validates an organization end to end.
    Returns (spec, result). If result.ok is False, the spec may still be
    usable for debug but must NOT be used for compile().
    """
    result = ValidationResult()

    try:
        spec = load_org_yaml(org_yaml_path)
    except OrgSpecError as e:
        result.errors.append(str(e))
        raise  # without a valid spec there is nothing else to check

    try:
        check_no_cycles(spec)
    except EscalationCycleError as e:
        result.errors.append(str(e))

    result.errors.extend(check_references(spec))
    result.warnings.extend(suggest_role_level_exceptions(spec))

    return spec, result


def validate_compiled_output(spec: OrgSpec, out_dir: Path) -> ValidationResult:
    """Runs the budget checks on the already compiled files."""
    result = ValidationResult()
    for actor in spec.actors:
        actor_dir = out_dir / actor.id
        role = spec.role_by_id(actor.role)

        soul_path = actor_dir / "SOUL.md"
        if soul_path.exists():
            msg = check_soul_budget(soul_path, role.soul_line_budget)
            if msg:
                result.warnings.append(msg)

        memory_path = actor_dir / "MEMORY.md"
        if memory_path.exists():
            msg = check_memory_budget(memory_path)
            if msg:
                result.warnings.append(msg)
    return result
