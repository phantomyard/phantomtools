"""
Sector templates for `pf new-org --template <sector>`.

Each template is only a list of default departments — no roles or actors,
because those are always specific to each real organization and forcing a
template there would be exactly the mistake that Epic 3 wants to avoid
(over-specifying before knowing the real case). Departments, on the other
hand, do repeat fairly regularly within the same sector and save the
first hand-typed `add-department` calls.

`ngo` is modeled directly on the real structure of Aquaponics United
(Management/Operations/Training/Finance), the first validated use case
of PhantomForge.
"""

from __future__ import annotations

TEMPLATES: dict[str, list[dict]] = {
    "ngo": [
        {
            "id": "direccion",
            "name": "Management",
            "parent": None,
            "access_policy": "level-3",
        },
        {
            "id": "operaciones",
            "name": "Operations",
            "parent": "direccion",
            "access_policy": "level-2",
        },
        {
            "id": "formacion",
            "name": "Training",
            "parent": "direccion",
            "access_policy": "level-2",
        },
        {
            "id": "finanzas",
            "name": "Finance",
            "parent": "direccion",
            "access_policy": "level-2",
        },
    ],
    "pyme": [
        {
            "id": "direccion",
            "name": "Management",
            "parent": None,
            "access_policy": "level-3",
        },
        {
            "id": "ventas",
            "name": "Sales",
            "parent": "direccion",
            "access_policy": "level-2",
        },
        {
            "id": "operaciones",
            "name": "Operations",
            "parent": "direccion",
            "access_policy": "level-2",
        },
        {
            "id": "finanzas",
            "name": "Finance",
            "parent": "direccion",
            "access_policy": "level-2",
        },
    ],
    "consultora": [
        {
            "id": "direccion",
            "name": "Management",
            "parent": None,
            "access_policy": "level-3",
        },
        {
            "id": "proyectos",
            "name": "Projects",
            "parent": "direccion",
            "access_policy": "level-2",
        },
        {
            "id": "desarrollo_negocio",
            "name": "Business Development",
            "parent": "direccion",
            "access_policy": "level-2",
        },
        {
            "id": "finanzas",
            "name": "Finance",
            "parent": "direccion",
            "access_policy": "level-2",
        },
    ],
    "finance": [
        {
            "id": "direccion",
            "name": "Management",
            "parent": None,
            "access_policy": "level-3",
        },
        {
            "id": "inversiones",
            "name": "Investments",
            "parent": "direccion",
            "access_policy": "level-2",
        },
        {
            "id": "cumplimiento",
            "name": "Compliance",
            "parent": "direccion",
            "access_policy": "level-2",
        },
        {
            "id": "relacion_clientes",
            "name": "Client Relations",
            "parent": "direccion",
            "access_policy": "level-2",
        },
    ],
}


def available_templates() -> list[str]:
    return sorted(TEMPLATES.keys())


def departments_for(template: str) -> list[dict]:
    if template not in TEMPLATES:
        raise ValueError(
            f"Template '{template}' does not exist. Available: {available_templates()}"
        )
    # shallow copy: they are flat dicts, list(dict(...)) is enough
    return [dict(d) for d in TEMPLATES[template]]
