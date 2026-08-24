"""Shape validation: ids and key reference fields must be strings."""

import unittest
from pathlib import Path
from typing import ClassVar

import yaml

from phantomorg.spec.shape_validator import ShapeError, validate_shape

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"


class TestShapeValidatorIdTypes(unittest.TestCase):
    def setUp(self):
        with open(AU_ORG, encoding="utf-8") as f:
            self.doc = yaml.safe_load(f)

    def test_numeric_department_id_rejected(self):
        self.doc["departments"][0]["id"] = 123
        with self.assertRaises(ShapeError):
            validate_shape(self.doc)

    def test_list_role_id_rejected(self):
        # A list is hashable-unsafe for the duplicate check; must be rejected.
        self.doc["roles"][0]["id"] = ["ceo"]
        with self.assertRaises(ShapeError):
            validate_shape(self.doc)

    def test_numeric_actor_role_rejected(self):
        self.doc["actors"][0]["role"] = 42
        with self.assertRaises(ShapeError):
            validate_shape(self.doc)

    def test_valid_doc_passes(self):
        validate_shape(self.doc)  # must not raise


class TestIdentifierGrammar(unittest.TestCase):
    """High finding: ids must be safe path components / reference keys.
    A YAML id like ``../outside`` must be rejected at validation time so
    the compiler can never write outside the requested output dir."""

    def setUp(self):
        with open(AU_ORG, encoding="utf-8") as f:
            self.doc = yaml.safe_load(f)

    def _check_id(self, value, path_parts):
        """Set the id at path_parts (list of keys) and expect ShapeError."""
        import copy

        doc = copy.deepcopy(self.doc)
        node = doc
        for part in path_parts[:-1]:
            node = node[part]
        node[path_parts[-1]] = value
        with self.assertRaises(ShapeError):
            validate_shape(doc)

    def test_actor_id_path_traversal_rejected(self):
        self._check_id("../outside", ["actors", 0, "id"])

    def test_actor_id_nested_path_rejected(self):
        self._check_id("a/b", ["actors", 0, "id"])

    def test_actor_id_dotdot_rejected(self):
        self._check_id("..", ["actors", 0, "id"])

    def test_actor_id_hidden_dotfile_rejected(self):
        self._check_id(".hidden", ["actors", 0, "id"])

    def test_actor_id_whitespace_rejected(self):
        self._check_id("a b", ["actors", 0, "id"])

    def test_actor_id_leading_dash_rejected(self):
        self._check_id("-x", ["actors", 0, "id"])

    def test_actor_id_absolute_path_rejected(self):
        self._check_id("/etc/passwd", ["actors", 0, "id"])

    def test_actor_id_uppercase_rejected(self):
        self._check_id("Marco", ["actors", 0, "id"])

    def test_org_id_path_traversal_rejected(self):
        self._check_id("../org", ["organization", "id"])

    def test_department_id_path_traversal_rejected(self):
        self._check_id("../dept", ["departments", 0, "id"])

    def test_role_id_path_traversal_rejected(self):
        self._check_id("../role", ["roles", 0, "id"])

    def test_access_level_key_path_traversal_rejected(self):
        full = dict(self.doc)
        full["policies"] = {
            "access_levels": {"../x": {"label": "L", "categories": []}},
            "security_categories": self.doc["policies"]["security_categories"],
        }
        with self.assertRaises(ShapeError):
            validate_shape(full)

    def test_identifier_with_underscore_is_allowed(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["roles"][0]["id"] = "chief_of_staff"
        validate_shape(doc)  # must not raise

    def test_identifier_with_digits_is_allowed(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["actors"][0]["id"] = "actor2"
        validate_shape(doc)  # must not raise


class TestShapeTypeChecks(unittest.TestCase):
    """Medium finding: malformed YAML must raise ShapeError, never a raw
    Python exception (AttributeError/TypeError from unvalidated nested
    types escaping the validator boundary)."""

    def setUp(self):
        with open(AU_ORG, encoding="utf-8") as f:
            self.doc = yaml.safe_load(f)

    def _mutate(self, mutator):
        import copy

        doc = copy.deepcopy(self.doc)
        mutator(doc)
        with self.assertRaises(ShapeError):
            validate_shape(doc)

    def test_policies_not_a_mapping(self):
        self._mutate(lambda d: d.__setitem__("policies", ["access_levels"]))

    def test_access_levels_not_a_mapping(self):
        self._mutate(lambda d: d["policies"].__setitem__("access_levels", "level-3"))

    def test_access_level_value_not_a_mapping(self):
        self._mutate(
            lambda d: d["policies"]["access_levels"].__setitem__("level-3", "oops")
        )

    def test_access_level_label_not_string(self):
        self._mutate(
            lambda d: d["policies"]["access_levels"]["level-3"].__setitem__("label", 42)
        )

    def test_access_level_categories_not_a_list(self):
        self._mutate(
            lambda d: d["policies"]["access_levels"]["level-3"].__setitem__(
                "categories", "1,2,3"
            )
        )

    def test_access_level_category_not_int(self):
        self._mutate(
            lambda d: d["policies"]["access_levels"]["level-3"][
                "categories"
            ].__setitem__(0, "one")
        )

    def test_security_categories_not_a_mapping(self):
        self._mutate(
            lambda d: d["policies"].__setitem__("security_categories", ["cat-0"])
        )

    def test_security_category_scope_invalid(self):
        self._mutate(
            lambda d: d["policies"]["security_categories"]["category-0"].__setitem__(
                "scope", "everyone"
            )
        )

    def test_message_types_not_a_list(self):
        self._mutate(
            lambda d: d["communication"].__setitem__("message_types", "REQUEST")
        )

    def test_message_type_not_a_string(self):
        self._mutate(lambda d: d["communication"]["message_types"].__setitem__(0, 42))

    def test_max_hops_not_int(self):
        self._mutate(lambda d: d["communication"].__setitem__("max_hops", "3"))

    def test_max_hops_below_minimum(self):
        self._mutate(lambda d: d["communication"].__setitem__("max_hops", 0))

    def test_request_id_format_not_string(self):
        self._mutate(lambda d: d["communication"].__setitem__("request_id_format", 123))

    def test_version_not_int(self):
        self._mutate(lambda d: d.__setitem__("version", "1"))

    def test_roles_functions_not_a_list(self):
        self._mutate(lambda d: d["roles"][0].__setitem__("functions", "vision, tools"))

    def test_roles_functions_item_not_string(self):
        self._mutate(lambda d: d["roles"][0]["functions"].__setitem__(0, 7))

    def test_roles_soul_line_budget_not_int(self):
        self._mutate(lambda d: d["roles"][0].__setitem__("soul_line_budget", "300"))

    def test_roles_soul_line_budget_too_small(self):
        self._mutate(lambda d: d["roles"][0].__setitem__("soul_line_budget", 10))

    def test_actors_tools_item_not_string(self):
        self._mutate(lambda d: d["actors"][0]["tools"].__setitem__(0, ["email"]))

    def test_actors_tools_excluded_not_a_list(self):
        self._mutate(lambda d: d["actors"][0].__setitem__("tools_excluded", "all"))

    def test_actors_tone_not_string(self):
        self._mutate(lambda d: d["actors"][0].__setitem__("tone", 3))

    def test_departments_parent_wrong_type(self):
        self._mutate(lambda d: d["departments"][0].__setitem__("parent", 7))

    def test_roles_reports_to_wrong_type(self):
        self._mutate(lambda d: d["roles"][0].__setitem__("reports_to", 7))

    def test_escalation_from_not_string(self):
        self._mutate(lambda d: d["escalation_matrix"][0].__setitem__("from", 1))

    def test_escalation_cross_department_wrong_type(self):
        self._mutate(
            lambda d: d["escalation_matrix"][0].__setitem__("cross_department", "yes")
        )

    def test_escalation_entry_not_a_mapping(self):
        self._mutate(lambda d: d["escalation_matrix"].__setitem__(0, "from: ceo"))

    def test_organization_languages_item_not_string(self):
        self._mutate(lambda d: d["organization"]["languages"].__setitem__(0, 1))

    def test_department_not_a_mapping(self):
        self._mutate(lambda d: d["departments"].__setitem__(0, ["id"]))


class TestCompilerPathContainment(unittest.TestCase):
    """Defense-in-depth: even a spec built outside load_org_yaml (which
    skips shape validation) cannot make the compiler write outside the
    output directory."""

    def test_build_rejects_escaping_actor_id(self):
        import tempfile

        from phantomorg.compiler.build import build
        from phantomorg.spec.model import (
            AccessLevel,
            Actor,
            Communication,
            Department,
            Organization,
            OrgSpec,
            Policies,
            Role,
            SecurityCategory,
        )

        org = Organization(id="test-org", name="T", sector="s", languages=["en"])
        dept = Department(id="d", name="D", parent=None, access_policy="level-3")
        role = Role(id="ceo", name="CEO", department="d", access_level="level-3")
        actor = Actor(id="../outside", role="ceo")
        policies = Policies(
            access_levels={"level-3": AccessLevel(label="L", categories=[])},
            security_categories={"cat-1": SecurityCategory(label="C")},
        )
        spec = OrgSpec(
            version=1,
            organization=org,
            departments=[dept],
            roles=[role],
            actors=[actor],
            humans=[],
            policies=policies,
            escalation_matrix=[],
            communication=Communication(
                request_id_format="x", message_types=["REQUEST"]
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            out = Path(tmp)
            with self.assertRaises(ValueError) as ctx:
                build(spec, out)
            self.assertIn("escapes", str(ctx.exception))
            # nothing was written outside
            self.assertEqual([], list(out.rglob("outside")))


class TestNpubValidation(unittest.TestCase):
    """actors[].npub is optional, but when present must be a valid NIP-19
    bech32 npub (charset + length + BIP-173 checksum)."""

    # Real npubs extracted from the AU bots' phantomchat identities.
    VALID: ClassVar[list[str]] = [
        "npub16fg8f93njtj7nervk94w6kgtdp4vtze8dzfer2qjc394mx6luzgqavqwgg",
        "npub1lq22ue4wzezjy4v06xa925r0ed73h35chm38qme82jerzshtd2wsaujvls",
        "npub1fcmtmz4ftp6tmdnhxeu0gt5nqqr7lxf9vxlm8qu6s2vmuah5presk0agh9",
        "npub1ax0ysc0rz74p3j3mreylczfc658setut8g4thqv80qk0y6td3ursy8jhvm",
        "npub195framkkdk6fx0qqeyqlpmpwynl02kvrwa4u6qulkt9hyz3q6s8qq6flel",
    ]

    def setUp(self):
        with open(AU_ORG, encoding="utf-8") as f:
            self.doc = yaml.safe_load(f)

    def test_valid_npub_passes(self):
        for npub in self.VALID:
            self.doc["actors"][0]["npub"] = npub
            validate_shape(self.doc)  # must not raise

    def test_npub_optional(self):
        # npub is optional per-actor: removing it from a copy of the real
        # AU org (which declares them all) must still validate.
        del self.doc["actors"][0]["npub"]
        validate_shape(self.doc)

    def test_invalid_checksum_rejected(self):
        # Same key as VALID[0] with the final char flipped -> checksum fails.
        bad = self.VALID[0][:-1] + ("h" if self.VALID[0][-1] != "h" else "g")
        self.doc["actors"][0]["npub"] = bad
        with self.assertRaises(ShapeError) as ctx:
            validate_shape(self.doc)
        self.assertIn("invalid NIP-19 npub", str(ctx.exception))

    def test_bad_charset_rejected(self):
        # 'O' is not in the bech32 charset (bech32 is case-insensitive but
        # only uses 'qpzry9x8gf2tvdw0s3jn54khce6mua7l').
        bad = self.VALID[0][:10] + "O" + self.VALID[0][11:]
        self.doc["actors"][0]["npub"] = bad
        with self.assertRaises(ShapeError):
            validate_shape(self.doc)

    def test_wrong_length_rejected(self):
        for bad in (self.VALID[0][:-1], self.VALID[0] + "q"):
            self.doc["actors"][0]["npub"] = bad
            with self.assertRaises(ShapeError):
                validate_shape(self.doc)

    def test_wrong_hrp_rejected(self):
        # "nub1" / "npub" missing the '1' separator / uppercase HRP.
        for bad in (
            "nub1" + self.VALID[0][5:],
            "NPUB1" + self.VALID[0][5:],
            self.VALID[0].replace("npub1", "npubx", 1),
        ):
            self.doc["actors"][0]["npub"] = bad
            with self.assertRaises(ShapeError):
                validate_shape(self.doc)

    def test_npub_not_a_string_rejected(self):
        for bad in (42, ["npub"], {"npub": 1}, True):
            self.doc["actors"][0]["npub"] = bad
            with self.assertRaises(ShapeError):
                validate_shape(self.doc)

    def test_npub_typo_key_rejected(self):
        # A typo'd field name must not silently pass (mirrors the
        # telegram_bott regression in test_spec_media).
        self.doc["actors"][0]["npubb"] = self.VALID[0]
        with self.assertRaises(ShapeError) as ctx:
            validate_shape(self.doc)
        self.assertIn("unknown field", str(ctx.exception))

    def test_valid_telegram_handle_passes(self):
        # Real AU bot handles (live getMe) — all valid shapes.
        for handle in (
            "@marco_bot",
            "@lucia_bot",
            "@dana_bot",
            "@elias_bot",
            "@diego_bot",
            "@a_123",
        ):
            self.doc["actors"][0]["telegram_bot"] = handle
            validate_shape(self.doc)  # must not raise

    def test_telegram_handle_optional(self):
        del self.doc["actors"][0]["telegram_bot"]
        validate_shape(self.doc)

    def test_invalid_telegram_handle_rejected(self):
        # Missing '@', too short (<5 chars after @), spaces, accents,
        # too long — all must fail.
        for bad in (
            "marco_bot",  # no @
            "@ab",  # too short
            "@a b",  # space
            "@Áé_bot",  # non-ASCII
            "@" + "x" * 33,  # too long
            "",
        ):
            self.doc["actors"][0]["telegram_bot"] = bad
            with self.assertRaises(ShapeError) as ctx:
                validate_shape(self.doc)
            self.assertIn("invalid handle", str(ctx.exception))

    def test_telegram_handle_not_a_string_rejected(self):
        for bad in (42, ["@bot"], {"@bot": 1}, True):
            self.doc["actors"][0]["telegram_bot"] = bad
            with self.assertRaises(ShapeError):
                validate_shape(self.doc)


class TestHumansRegistry(unittest.TestCase):
    """org.yaml ``humans:`` block: optional, validatable registry of
    human counterparts (Board president, treasurer...)."""

    VALID_NPUB = "npub16fg8f93njtj7nervk94w6kgtdp4vtze8dzfer2qjc394mx6luzgqavqwgg"

    def setUp(self):
        with open(AU_ORG, encoding="utf-8") as f:
            self.doc = yaml.safe_load(f)

    def test_org_without_humans_is_valid(self):
        # The AU org now declares humans, but an org without the block
        # (or with an empty list) must stay valid — unlike roles/actors,
        # humans is optional.
        import copy

        doc = copy.deepcopy(self.doc)
        doc.pop("humans", None)
        validate_shape(doc)  # must not raise
        doc2 = copy.deepcopy(self.doc)
        doc2["humans"] = []
        validate_shape(doc2)  # must not raise

    def test_valid_humans_pass(self):
        validate_shape(self.doc)  # must not raise

    def test_human_invalid_id_rejected(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["humans"][0]["id"] = "Board President Alba"
        with self.assertRaises(ShapeError):
            validate_shape(doc)

    def test_human_typo_key_rejected(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["humans"][0]["telegram_user_id_typo"] = 1
        with self.assertRaises(ShapeError) as ctx:
            validate_shape(doc)
        self.assertIn("unknown field", str(ctx.exception))

    def test_human_nullable_telegram_and_npub(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["humans"][1]["telegram_user_id"] = None
        doc["humans"][1]["npub"] = None
        validate_shape(doc)  # must not raise

    def test_human_invalid_npub_rejected(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["humans"][0]["npub"] = "npub1notarealnpub"
        with self.assertRaises(ShapeError):
            validate_shape(doc)

    def test_human_npub_accepts_valid(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["humans"][0]["npub"] = self.VALID_NPUB
        validate_shape(doc)  # must not raise


class TestDocumentsBlockAndProjectScope(unittest.TestCase):
    """PhantomDocs merge adds an optional root ``documents:`` block and
    ``scope: project`` security categories. ``po validate`` must accept
    both (the runtime already does) instead of rejecting them as unknown
    root keys / invalid scopes."""

    def setUp(self):
        with open(AU_ORG, encoding="utf-8") as f:
            self.doc = yaml.safe_load(f)

    def test_documents_block_is_valid(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["documents"] = {
            "namespace": "au",
            "org_pubkey": (
                "npub1ttkrrfadcrqps2vs6lmrq8rlr3dp6km378z4xklmz3jt82s2jytqzfw5mu"
            ),
            "inboxes": {"ceo": {"name": "AU Inbox/Direccion", "id": "1ZJ"}},
            "naming": {
                "domains": [
                    {
                        "id": "direction",
                        "category": 2,
                        "owners": ["ceo"],
                        "types": [{"id": "action-plans"}],
                    }
                ]
            },
        }
        validate_shape(doc)  # must not raise

    def test_documents_block_is_optional(self):
        # Absent documents block (as in the pre-PhantomDocs orgs) stays valid.
        validate_shape(self.doc)  # must not raise

    def test_scope_project_is_valid(self):
        import copy

        doc = copy.deepcopy(self.doc)
        doc["policies"]["security_categories"]["category-0"]["scope"] = "project"
        validate_shape(doc)  # must not raise

    def test_scope_project_still_rejects_garbage(self):
        # Adding `project` to VALID_SCOPES must not widen the enum to
        # arbitrary strings.
        import copy

        doc = copy.deepcopy(self.doc)
        doc["policies"]["security_categories"]["category-0"]["scope"] = "projects"
        with self.assertRaises(ShapeError):
            validate_shape(doc)

    def test_documents_must_be_mapping(self):
        # schema.json requires documents to be an object. A list, scalar
        # or null must be rejected (PhantomDocs consumers call .get() on
        # it and would crash on a non-mapping).
        import copy

        for bad in ([], ["namespace"], "au", 42, True, None):
            doc = copy.deepcopy(self.doc)
            doc["documents"] = bad
            with self.assertRaises(ShapeError) as ctx:
                validate_shape(doc)
            self.assertIn("documents", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
