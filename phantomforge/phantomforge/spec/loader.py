"""
Loads an org.yaml, validates its shape (shape_validator, section 5.2 of
the spec) and returns a typed OrgSpec (model.OrgSpec.from_dict).

The *business* validation (cycles in escalation_matrix, cross-references,
size budgets) lives in phantomforge.validator, not here: this module only
guarantees "this is a well-formed org.yaml".
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .model import OrgSpec
from .shape_validator import ShapeError, validate_shape


class OrgSpecError(Exception):
    """Load or shape-validation error of org.yaml."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys.

    PyYAML's default behavior keeps the *last* value of a duplicated key
    silently — for a hand-edited org.yaml that is silent spec data loss
    (e.g. a pasted duplicate ``roles:`` block drops the first one).
    """

    def construct_mapping(self, node, deep=False):
        mapping = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping.add(key)
        return super().construct_mapping(node, deep=deep)


def load_org_yaml(path: str | Path) -> OrgSpec:
    """Loads an org.yaml and returns its typed OrgSpec.

    Raises OrgSpecError for any read/parse/shape/typing problem: malformed
    YAML, invalid UTF-8, duplicate keys, missing files, permission errors,
    directories passed as path, recursion bombs and shape violations all
    surface as OrgSpecError (never raw yaml/OSError exceptions).
    """
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as f:
            # _UniqueKeyLoader extends yaml.SafeLoader, so this is safe_load
            # semantics (no arbitrary object instantiation); bandit cannot
            # see the inheritance, hence the inline nosec below.
            raw = yaml.load(f, Loader=_UniqueKeyLoader)  # nosec B506
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError, OSError) as e:
        raise OrgSpecError(f"org.yaml could not be read/parsed: {e}") from e

    try:
        validate_shape(raw)
    except ShapeError as e:
        raise OrgSpecError(
            f"org.yaml does not conform to the expected shape: {e}"
        ) from e

    try:
        return OrgSpec.from_dict(raw)
    except Exception as exc:
        raise OrgSpecError(f"org.yaml could not be typed: {exc}") from exc
