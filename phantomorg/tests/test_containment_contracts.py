import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).parents[1]
CONTRACTS = ROOT / "docs" / "containment"
EXAMPLES = CONTRACTS / "examples"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_inventory_example_conforms_to_contract():
    schema = _load(CONTRACTS / "inventory-v1.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(
        _load(EXAMPLES / "inventory.synthetic.json"),
        schema,
        format_checker=jsonschema.FormatChecker(),
    )


def test_boundary_declaration_example_conforms_to_contract():
    schema = _load(CONTRACTS / "boundary-declaration-v1.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(
        _load(EXAMPLES / "boundary-declaration.synthetic.json"),
        schema,
        format_checker=jsonschema.FormatChecker(),
    )


def test_boundary_declaration_rejects_empty_boundaries():
    schema = _load(CONTRACTS / "boundary-declaration-v1.schema.json")
    declaration = _load(EXAMPLES / "boundary-declaration.synthetic.json")
    declaration["boundaries"] = []

    validator = jsonschema.Draft202012Validator(schema)
    assert list(validator.iter_errors(declaration))


def test_inventory_rejects_unlabelled_evidence():
    schema = _load(CONTRACTS / "inventory-v1.schema.json")
    inventory = _load(EXAMPLES / "inventory.synthetic.json")
    del inventory["hosts"][0]["evidence"]["status"]

    validator = jsonschema.Draft202012Validator(schema)
    assert list(validator.iter_errors(inventory))


def test_boundary_declaration_requires_shared_record_isolation():
    schema = _load(CONTRACTS / "boundary-declaration-v1.schema.json")
    declaration = _load(EXAMPLES / "boundary-declaration.synthetic.json")
    del declaration["boundaries"][0]["shared_record_isolation"]

    validator = jsonschema.Draft202012Validator(schema)
    assert list(validator.iter_errors(declaration))


def test_inventory_rejects_malformed_timestamp():
    schema = _load(CONTRACTS / "inventory-v1.schema.json")
    inventory = _load(EXAMPLES / "inventory.synthetic.json")
    inventory["collected_at"] = "not-a-timestamp"

    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    assert list(validator.iter_errors(inventory))
