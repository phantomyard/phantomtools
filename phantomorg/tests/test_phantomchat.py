"""Phantomchat verification module (compiler/phantomchat.py).

Covers:

- Per-actor statuses: ok / mismatch / missing-identity /
  missing-phantomchat / not-declared / error.
- Real npub is read from the (injectable) runner output — the binary is
  never invoked in these tests; a fake runner stands in for
  `phantombot phantomchat --persona X`.
- identity.json must actually contain an nsec key (a malformed identity
  file is treated as missing).
- Declared npub (org.yaml) is contrasted with the runtime identity npub.
- The manifest serializes deterministically (except checked_at) and its
  ok property / summary behave as documented.
- Non-invasive: verify_phantomchat never writes to the personas dir.
"""

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from click.testing import CliRunner

from phantomorg.compiler.phantomchat import (
    ERROR,
    IDENTITY_FILENAME,
    MISMATCH,
    MISSING_IDENTITY,
    MISSING_PHANTOMCHAT,
    NOT_DECLARED,
    OK,
    PHANTOMCHAT_FILENAME,
    PhantomchatManifest,
    _extract_npub,
    verify_phantomchat,
)
from phantomorg.spec.loader import load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"

# Real AU runtime npubs (extracted non-invasively from the MacBookPro
# identities) — used to build realistic fixtures.
PACO_NPUB = "npub163h60w38hxsva60hjap53n8eh264g923da9qg58q7dqv68hz0evqygqkhf"
PEPA_NPUB = "npub1gecyq3xylqzzvehz08mnsd4rxpjphhm03saq4rx3hx5xee4cg7tqts74vr"
ROBERTO_NPUB = "npub14t8u4p6n5lfahlwtfr2zq4zqrggktjmnv99qx0yx0j3wpfuvl2tssf775x"
ALMA_NPUB = "npub1ggyxfrue07z39dl0ag3lge3z8l7vtunlyrg9quwcdh4r84rnwq4s25aqa9"
ELENA_NPUB = "npub13wcvzezm4wxxyv3c6nda700zrjt63w06tpevkf52mkuggy3aqf7qlm0mp4"

ALL_NPUBS = [PACO_NPUB, PEPA_NPUB, ROBERTO_NPUB, ALMA_NPUB, ELENA_NPUB]

# TUI output shape of `phantombot phantomchat --persona X` (identity
# exists): box-drawing + the npub line. The extractor only needs the
# npub token; the fake runner reproduces the essential shape.
TUI_OUTPUT = (
    "┌  Configure phantomchat (Nostr NIP-17 DMs) for persona '{id}'\n"
    "│\n"
    "◇  Existing identity ─────────────────────────────────╮\n"
    "│  Persona '{id}' already has a phantomchat identity. │\n"
    "│  Its npub (paste this into the PhantomChat app):    │\n"
    "│    {npub}  │\n"
    "└─────────────────────────────────────────────────────╯\n"
)


def _identity_json() -> str:
    # Real shape: {"nsec": "<63 chars>"} — the value is NOT the npub and
    # the module never needs it (the binary derives the npub).
    return json.dumps({"nsec": "nsec1" + "a" * 59}) + "\n"


def _phantomchat_json() -> str:
    return (
        json.dumps(
            {
                "relays": ["ws://relay.example.invalid:7777"],
                "allowed_npubs": ["npub1" + "b" * 58],
                "greeted": [],
            }
        )
        + "\n"
    )


class FakeRunner:
    """Stands in for the phantombot binary.

    ``npubs`` maps persona id -> real npub (None = no identity / command
    fails). Records invocations so tests can assert non-invasiveness
    (exactly the expected commands, nothing else).
    """

    def __init__(self, npubs: dict[str, str | None], rc: int = 0):
        self.npubs = dict(npubs)
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        # args shape: ["phantomchat", "--persona", <id>]
        persona = args[2] if len(args) > 2 else ""
        real = self.npubs.get(persona)
        if real is None or self.rc != 0:
            return type("P", (), {"returncode": self.rc, "stdout": "", "stderr": ""})()
        out = TUI_OUTPUT.format(id=persona, npub=real)
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()


class PhantomchatVerifyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.personas = Path(self.tmp.name)
        self.spec = load_org_yaml(AU_ORG)
        self._real = {
            "marco": PACO_NPUB,
            "lucia": PEPA_NPUB,
            "diego": ROBERTO_NPUB,
            "dana": ALMA_NPUB,
            "elias": ELENA_NPUB,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _make_actor_dir(self, actor_id, *, identity=True, phantomchat=True):
        d = self.personas / actor_id
        d.mkdir(parents=True, exist_ok=True)
        if identity:
            (d / IDENTITY_FILENAME).write_text(_identity_json(), encoding="utf-8")
        if phantomchat:
            (d / PHANTOMCHAT_FILENAME).write_text(_phantomchat_json(), encoding="utf-8")
        return d

    def _with_declared_npubs(self, npubs: dict[str, str | None]):
        """Rewrite AU org.yaml with declared npubs (in a temp copy)."""
        import yaml

        org_path = Path(AU_ORG)
        raw = yaml.safe_load(org_path.read_text(encoding="utf-8"))
        for actor in raw["actors"]:
            if actor["id"] in npubs:
                actor["npub"] = npubs[actor["id"]]
        tmp_org = Path(self.tmp.name) / "org.yaml"
        tmp_org.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        return load_org_yaml(tmp_org)

    # ------------------------------------------------------------- statuses

    def test_all_declared_and_matching_ok(self):
        spec = self._with_declared_npubs(self._real)
        for a in self._real:
            self._make_actor_dir(a)
        runner = FakeRunner(self._real)
        manifest = verify_phantomchat(
            spec, self.personas, runner=runner, now=lambda: "2026-08-11T00:00:00+00:00"
        )
        self.assertTrue(manifest.ok)
        self.assertEqual([c.status for c in manifest.checks], [OK] * 5)
        for c in manifest.checks:
            self.assertEqual(c.declared_npub, c.real_npub)
        # exactly one non-invasive invocation per actor
        self.assertEqual([a[0] for a in runner.calls], ["phantomchat"] * 5)

    def test_mismatch_when_declared_differs(self):
        # Declare lucia's npub for marco -> mismatch on marco, ok elsewhere.
        wrong = dict(self._real)
        wrong["marco"] = PEPA_NPUB
        spec = self._with_declared_npubs(wrong)
        for a in self._real:
            self._make_actor_dir(a)
        manifest = verify_phantomchat(
            spec, self.personas, runner=FakeRunner(self._real)
        )
        by_id = {c.actor_id: c for c in manifest.checks}
        self.assertEqual(by_id["marco"].status, MISMATCH)
        self.assertEqual(by_id["marco"].declared_npub, PEPA_NPUB)
        self.assertEqual(by_id["marco"].real_npub, PACO_NPUB)
        self.assertEqual(by_id["lucia"].status, OK)
        self.assertFalse(manifest.ok)

    def test_no_declared_npub_is_not_declared(self):
        # Org declares no npubs (strip them from the real AU org, which
        # now declares all) -> every actor is NOT_DECLARED (but runtime
        # identities exist).
        spec = self._with_declared_npubs({a: None for a in self._real})
        for a in self._real:
            self._make_actor_dir(a)
        manifest = verify_phantomchat(
            spec, self.personas, runner=FakeRunner(self._real)
        )
        self.assertTrue(manifest.ok)  # nothing declared -> nothing to fail
        self.assertEqual({c.status for c in manifest.checks}, {NOT_DECLARED})
        for c in manifest.checks:
            self.assertIsNone(c.declared_npub)
            self.assertEqual(c.real_npub, self._real[c.actor_id])

    def test_missing_identity_with_declared_npub(self):
        spec = self._with_declared_npubs(self._real)
        self._make_actor_dir("marco", identity=False)  # marco has no keypair
        for a in ("lucia", "diego", "dana", "elias"):
            self._make_actor_dir(a)
        runner = FakeRunner(self._real)
        manifest = verify_phantomchat(spec, self.personas, runner=runner)
        by_id = {c.actor_id: c for c in manifest.checks}
        self.assertEqual(by_id["marco"].status, MISSING_IDENTITY)
        self.assertFalse(by_id["marco"].identity_exists)
        self.assertFalse(manifest.ok)
        # the runner was never asked about marco (no identity to check)
        asked = [c[2] for c in runner.calls if len(c) > 2]
        self.assertNotIn("marco", asked)
        self.assertEqual(sorted(asked), ["dana", "diego", "elias", "lucia"])

    def test_identity_without_nsec_treated_as_missing(self):
        spec = self._with_declared_npubs(self._real)
        d = self._make_actor_dir("marco")
        (d / IDENTITY_FILENAME).write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        for a in ("lucia", "diego", "dana", "elias"):
            self._make_actor_dir(a)
        manifest = verify_phantomchat(
            spec, self.personas, runner=FakeRunner(self._real)
        )
        by_id = {c.actor_id: c for c in manifest.checks}
        self.assertEqual(by_id["marco"].status, MISSING_IDENTITY)

    def test_missing_phantomchat_json_reported(self):
        spec = self._with_declared_npubs(self._real)
        self._make_actor_dir("marco", phantomchat=False)
        for a in ("lucia", "diego", "dana", "elias"):
            self._make_actor_dir(a)
        manifest = verify_phantomchat(
            spec, self.personas, runner=FakeRunner(self._real)
        )
        by_id = {c.actor_id: c for c in manifest.checks}
        # identity exists and npub matches -> ok, but the missing
        # phantomchat.json is visible in the check (not a failure).
        self.assertEqual(by_id["marco"].status, MISSING_PHANTOMCHAT)
        self.assertFalse(by_id["marco"].phantomchat_exists)
        self.assertFalse(manifest.ok)

    def test_runner_failure_is_error(self):
        spec = self._with_declared_npubs(self._real)
        for a in self._real:
            self._make_actor_dir(a)
        runner = FakeRunner(self._real, rc=1)
        manifest = verify_phantomchat(spec, self.personas, runner=runner)
        self.assertTrue(all(c.status == ERROR for c in manifest.checks))
        self.assertFalse(manifest.ok)

    def test_runner_raises_oserror_is_error(self):
        spec = self._with_declared_npubs(self._real)
        for a in self._real:
            self._make_actor_dir(a)

        def boom(args):
            raise OSError("no such binary")

        manifest = verify_phantomchat(spec, self.personas, runner=boom)
        self.assertTrue(all(c.status == ERROR for c in manifest.checks))
        for c in manifest.checks:
            self.assertIn("could not run", c.detail)

    # ------------------------------------------------------------ manifest

    def test_manifest_json_shape_and_summary(self):
        spec = self._with_declared_npubs(self._real)
        for a in self._real:
            self._make_actor_dir(a)
        manifest = verify_phantomchat(
            spec,
            self.personas,
            runner=FakeRunner(self._real),
            now=lambda: "2026-08-11T00:00:00+00:00",
        )
        data = json.loads(manifest.to_json())
        self.assertEqual(data["format_version"], 1)
        self.assertEqual(data["org"], "verdant-aquaponics")
        self.assertEqual(data["checked_at"], "2026-08-11T00:00:00+00:00")
        self.assertEqual(
            data["summary"],
            {
                "ok": 5,
                **{
                    s: 0
                    for s in (
                        "mismatch",
                        "missing-identity",
                        "missing-phantomchat",
                        "not-declared",
                        "error",
                    )
                },
            },
        )
        self.assertEqual(set(data["checks"]), set(self._real))
        # deterministic: same run serializes identically
        again = json.loads(manifest.to_json())
        self.assertEqual(data, again)

    def test_ok_property_requires_checks(self):
        empty = PhantomchatManifest(
            org_id="x",
            personas_dir=".",
            phantomchat_bin="p",
            checked_at="t",
            checks=[],
        )
        self.assertFalse(empty.ok)

    def test_not_declared_counts_as_ok(self):
        m = PhantomchatManifest(
            org_id="x",
            personas_dir=".",
            phantomchat_bin="p",
            checked_at="t",
            checks=[type("C", (), {"status": NOT_DECLARED})()],
        )
        self.assertTrue(m.ok)

    # ------------------------------------------------------------- extract

    def test_extract_npub_from_tui_output(self):
        out = TUI_OUTPUT.format(id="marco", npub=PACO_NPUB)
        self.assertEqual(_extract_npub(out), PACO_NPUB)

    def test_extract_npub_none_when_absent(self):
        self.assertIsNone(_extract_npub("no npub here"))
        self.assertIsNone(_extract_npub(""))
        self.assertIsNone(
            _extract_npub("npub1" + "b" * 58)
        )  # 'b' not in bech32 charset


class PhantomchatCheckCLITest(unittest.TestCase):
    """CLI surface of `po phantomchat-check` (exit codes + JSON output)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.personas = Path(self.tmp.name)
        self.runner = CliRunner()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _manifest(ok: bool):
        from phantomorg.compiler.phantomchat import (
            MISMATCH as MISMATCH_STATUS,
        )
        from phantomorg.compiler.phantomchat import (
            OK as OK_STATUS,
        )
        from phantomorg.compiler.phantomchat import (
            ActorCheck,
            PhantomchatManifest,
        )

        checks = [
            ActorCheck(actor_id="marco", status=OK_STATUS),
        ]
        if not ok:
            checks.append(
                ActorCheck(
                    actor_id="diego",
                    status=MISMATCH_STATUS,
                    declared_npub="npub1" + "c" * 58,
                    real_npub="npub1" + "d" * 58,
                    identity_exists=True,
                    phantomchat_exists=True,
                    detail="declared npub differs from runtime identity",
                )
            )
        return PhantomchatManifest(
            org_id="verdant-aquaponics",
            personas_dir=".",
            phantomchat_bin="phantombot",
            checked_at="2026-08-11T00:00:00+00:00",
            checks=checks,
        )

    def test_ok_exit_zero(self):
        import phantomorg.cli as cli_mod

        with unittest.mock.patch.object(
            cli_mod, "verify_phantomchat", return_value=self._manifest(ok=True)
        ):
            result = self.runner.invoke(
                cli_mod.main,
                [
                    "phantomchat-check",
                    "--org",
                    str(AU_ORG),
                    "--personas-dir",
                    str(self.personas),
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("✓ All declared npubs match the runtime.", result.output)

    def test_failure_exit_one(self):
        import phantomorg.cli as cli_mod

        with unittest.mock.patch.object(
            cli_mod, "verify_phantomchat", return_value=self._manifest(ok=False)
        ):
            result = self.runner.invoke(
                cli_mod.main,
                [
                    "phantomchat-check",
                    "--org",
                    str(AU_ORG),
                    "--personas-dir",
                    str(self.personas),
                ],
            )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("diego: mismatch", result.output)

    def test_json_output_exit_codes(self):
        import phantomorg.cli as cli_mod

        with unittest.mock.patch.object(
            cli_mod, "verify_phantomchat", return_value=self._manifest(ok=False)
        ):
            result = self.runner.invoke(
                cli_mod.main,
                [
                    "phantomchat-check",
                    "--org",
                    str(AU_ORG),
                    "--personas-dir",
                    str(self.personas),
                    "--json",
                ],
            )
        self.assertEqual(result.exit_code, 1, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["org"], "verdant-aquaponics")
        self.assertEqual(data["checks"]["diego"]["status"], "mismatch")

    def test_invalid_org_rejected(self):
        import phantomorg.cli as cli_mod

        bad = Path(self.tmp.name) / "bad.yaml"
        bad.write_text("version: 1\norganization: {\n", encoding="utf-8")
        result = self.runner.invoke(
            cli_mod.main,
            ["phantomchat-check", "--org", str(bad)],
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Cannot verify", result.output)


if __name__ == "__main__":
    unittest.main()
