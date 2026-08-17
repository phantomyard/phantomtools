"""Telegram verification module (compiler/telegram.py).

Covers:

- Per-actor statuses: ok / mismatch / no-token / not-declared / error.
- Tokens are resolved from a phantombot config.toml: sub-persona tokens
  (``[channels.telegram.personas.<id>].token``) and the main token for
  the runtime default persona (state.json overrides config.toml,
  mirroring phantombot's runtime behaviour).
- Real username comes from the (injectable) getMe — the Telegram API is
  never called in these tests; a fake stands in.
- Declared telegram_bot (org.yaml) is contrasted with the live username.
- The manifest serializes deterministically (except checked_at) and its
  ok property / summary behave as documented.
- Non-invasive: verify_telegram never writes anything.
"""

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from click.testing import CliRunner

from phantomorg.compiler.telegram import (
    ERROR,
    MISMATCH,
    NO_TOKEN,
    NOT_DECLARED,
    OK,
    TelegramError,
    TelegramManifest,
    _getme,
    _normalize_handle,
    verify_telegram,
)
from phantomorg.spec.loader import load_org_yaml

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"

# Placeholder bot usernames matching the example org.yaml (sanitized).
REAL_BOTS = {
    "paco": "CEO_bot",
    "pepa": "PA_bot",
    "roberto": "CFO_bot",
    "alma": "Alma_bot",
    "elena": "Elena_bot",
}

# Minimal phantombot config.toml shape (mirrors the MacBookPro runtime).
CONFIG_TOML = """\
default_persona = "paco"

[channels.telegram]
token = "111:main_token"
allowed_user_ids = [1000000001]

[channels.telegram.personas.alma]
token = "222:alma_token"

[channels.telegram.personas.elena]
token = "333:elena_token"

[channels.telegram.personas.roberto]
token = "444:roberto_token"

[channels.telegram.personas.pepa]
token = "555:pepa_token"
"""

STATE_JSON = json.dumps({"harness_bins": {"pi": "/x"}, "default_persona": "paco"})


class FakeGetMe:
    """Stands in for the Telegram getMe call.

    ``bots`` maps token -> username (None = token invalid / API error).
    Records tokens so tests can assert non-invasiveness.
    """

    def __init__(self, bots: dict[str, str | None]):
        self.bots = dict(bots)
        self.calls: list[str] = []

    def __call__(self, token: str, timeout: float):
        self.calls.append(token)
        if token not in self.bots:
            return None, "getMe failed: invalid token"
        username = self.bots[token]
        if username is None:
            return None, "getMe failed: unauthorized"
        return username, ""


class TelegramVerifyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.toml"
        self.config.write_text(CONFIG_TOML, encoding="utf-8")
        self.state = self.root / "state.json"
        self.state.write_text(STATE_JSON, encoding="utf-8")
        self.spec = load_org_yaml(AU_ORG)
        self.tokens = {
            "111:main_token": "CEO_bot",
            "222:alma_token": "Alma_bot",
            "333:elena_token": "Elena_bot",
            "444:roberto_token": "CFO_bot",
            "555:pepa_token": "PA_bot",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _verify(self, **kwargs):
        return verify_telegram(
            self.spec,
            self.config,
            state_path=self.state,
            now=lambda: "2026-08-12T00:00:00+00:00",
            **kwargs,
        )

    # ------------------------------------------------------------- statuses

    def test_all_declared_and_matching_ok(self):
        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            manifest = self._verify()
        self.assertTrue(manifest.ok)
        self.assertEqual([c.status for c in manifest.checks], [OK] * 5)
        for c in manifest.checks:
            self.assertEqual(_normalize_handle(c.declared_bot), c.real_bot.lower().lstrip("@"))
        # exactly one getMe call per actor
        self.assertEqual(len(fake.calls), 5)

    def test_mismatch_when_declared_differs(self):
        # Revert pepa to the stale @COS_bot handle -> mismatch on pepa.
        for a in self.spec.actors:
            if a.id == "pepa":
                a.telegram_bot = "@COS_bot"
        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            manifest = self._verify()
        by_id = {c.actor_id: c for c in manifest.checks}
        self.assertEqual(by_id["pepa"].status, MISMATCH)
        self.assertEqual(by_id["pepa"].declared_bot, "@COS_bot")
        self.assertEqual(by_id["pepa"].real_bot, "@PA_bot")
        self.assertEqual(by_id["paco"].status, OK)
        self.assertFalse(manifest.ok)

    def test_case_insensitive_match(self):
        # Declared handle with different case still matches (Telegram
        # usernames are case-insensitive).
        for a in self.spec.actors:
            if a.id == "paco":
                a.telegram_bot = "@ceo_bot"
        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            manifest = self._verify()
        by_id = {c.actor_id: c for c in manifest.checks}
        self.assertEqual(by_id["paco"].status, OK)

    def test_no_token_when_actor_not_in_config(self):
        # An actor declared with telegram_bot but with no token anywhere
        # (no sub-persona token, and not the default persona).
        for a in self.spec.actors:
            if a.id == "elena":
                a.telegram_bot = "@Elena_bot"
        # Remove elena's token from config.
        config = self.config.read_text(encoding="utf-8")
        config = config.replace(
            '\n[channels.telegram.personas.elena]\ntoken = "333:elena_token"\n', "\n"
        )
        self.config.write_text(config, encoding="utf-8")
        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            manifest = self._verify()
        by_id = {c.actor_id: c for c in manifest.checks}
        self.assertEqual(by_id["elena"].status, NO_TOKEN)
        self.assertFalse(manifest.ok)
        # elena's token must NOT have been queried
        self.assertNotIn("333:elena_token", fake.calls)

    def test_getme_failure_is_error(self):
        fake = FakeGetMe(dict(self.tokens, **{"111:main_token": None}))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            manifest = self._verify()
        by_id = {c.actor_id: c for c in manifest.checks}
        self.assertEqual(by_id["paco"].status, ERROR)
        self.assertEqual(by_id["paco"].real_bot, None)
        self.assertIn("getMe failed", by_id["paco"].detail)
        self.assertFalse(manifest.ok)

    def test_not_declared_counts_as_ok(self):
        # Clear all telegram_bot declarations -> every actor not-declared,
        # manifest still ok (nothing to compare).
        for a in self.spec.actors:
            a.telegram_bot = None
        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            manifest = self._verify()
        self.assertEqual([c.status for c in manifest.checks], [NOT_DECLARED] * 5)
        self.assertTrue(manifest.ok)
        # no getMe calls at all
        self.assertEqual(fake.calls, [])

    def test_state_default_persona_overrides_config(self):
        # Runtime default persona comes from state.json. If state says
        # roberto AND roberto has no sub-persona token, the main token
        # (@CEO_bot) is checked against roberto's declared handle
        # (@CFO_bot) -> mismatch; paco (no longer default, no own
        # token) has no token at all.
        config = self.config.read_text(encoding="utf-8")
        config = config.replace(
            '\n[channels.telegram.personas.roberto]\ntoken = "444:roberto_token"\n',
            "\n",
        )
        self.config.write_text(config, encoding="utf-8")
        self.state.write_text(
            json.dumps({"default_persona": "roberto"}), encoding="utf-8"
        )
        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            manifest = self._verify()
        by_id = {c.actor_id: c for c in manifest.checks}
        self.assertEqual(by_id["roberto"].status, MISMATCH)
        self.assertEqual(by_id["roberto"].token_source, "main (default persona)")
        self.assertEqual(by_id["roberto"].real_bot, "@CEO_bot")
        self.assertEqual(by_id["paco"].status, NO_TOKEN)

    def test_manifest_json_shape_and_summary(self):
        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            manifest = self._verify()
        self.assertEqual(manifest.summary(), {"ok": 5, "mismatch": 0, "no-token": 0, "not-declared": 0, "error": 0})
        data = manifest.as_dict()
        self.assertEqual(data["org"], "aquaponics-united")
        self.assertEqual(data["summary"]["ok"], 5)
        self.assertEqual(set(data["checks"]), {"paco", "pepa", "roberto", "alma", "elena"})
        self.assertEqual(data["checks"]["paco"]["status"], OK)
        # deterministic serialization (checked_at fixed via now=)
        self.assertEqual(json.loads(manifest.to_json()), data)

    def test_ok_property_requires_checks(self):
        m = TelegramManifest(
            org_id="x", config_path="/x", checked_at="t", checks=[]
        )
        self.assertFalse(m.ok)

    def test_missing_config_raises(self):
        with self.assertRaises(TelegramError):
            verify_telegram(self.spec, self.root / "nope.toml")

    def test_invalid_toml_raises(self):
        bad = self.root / "bad.toml"
        bad.write_text("this is [not toml", encoding="utf-8")
        with self.assertRaises(TelegramError):
            verify_telegram(self.spec, bad)

    # ------------------------------------------------------------- helpers

    def test_normalize_handle(self):
        self.assertEqual(_normalize_handle("@CEO_bot"), "ceo_bot")
        self.assertEqual(_normalize_handle("CEO_bot"), "ceo_bot")
        self.assertEqual(_normalize_handle("  @PA_bot  "), "pa_bot")
        self.assertIsNone(_normalize_handle(None))

    def test_getme_network_error(self):
        with unittest.mock.patch(
            "phantomorg.compiler.telegram.urllib.request.urlopen",
            side_effect=OSError("no route"),
        ):
            username, detail = _getme("tok", timeout=1)
        self.assertIsNone(username)
        self.assertIn("getMe failed", detail)


# --------------------------------------------------------------- CLI tests


def _write_config(root: Path) -> Path:
    cfg = root / "config.toml"
    cfg.write_text(CONFIG_TOML, encoding="utf-8")
    return cfg


class TelegramCheckCliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = _write_config(self.root)
        self.state = self.root / "state.json"
        self.state.write_text(STATE_JSON, encoding="utf-8")
        self.tokens = {
            "111:main_token": "CEO_bot",
            "222:alma_token": "Alma_bot",
            "333:elena_token": "Elena_bot",
            "444:roberto_token": "CFO_bot",
            "555:pepa_token": "PA_bot",
        }
        from phantomorg.cli import main

        self.runner = CliRunner()
        self.main = main

    def tearDown(self):
        self.tmp.cleanup()

    def _invoke(self, *extra):

        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            return self.runner.invoke(
                self.main,
                [
                    "telegram-check",
                    "--org",
                    str(AU_ORG),
                    "--config",
                    str(self.config),
                    "--state",
                    str(self.state),
                    *extra,
                ],
            )

    def test_ok_exit_zero(self):
        result = self._invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("All declared telegram_bot handles match", result.output)

    def test_failure_exit_one(self):
        # Drift: revert pepa's declared handle to the stale @COS_bot.
        import yaml

        raw = yaml.safe_load(AU_ORG.read_text(encoding="utf-8"))
        for actor in raw["actors"]:
            if actor["id"] == "pepa":
                actor["telegram_bot"] = "@COS_bot"
        tmp_org = self.root / "org-drift.yaml"
        tmp_org.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


        fake = FakeGetMe(dict(self.tokens))
        with unittest.mock.patch(
            "phantomorg.compiler.telegram._getme", side_effect=fake
        ):
            result = self.runner.invoke(
                self.main,
                [
                    "telegram-check",
                    "--org",
                    str(tmp_org),
                    "--config",
                    str(self.config),
                    "--state",
                    str(self.state),
                ],
            )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("mismatch", result.output)
        self.assertIn("@COS_bot", result.output)
        self.assertIn("@PA_bot", result.output)

    def test_json_output_exit_codes(self):
        result = self._invoke("--json")
        self.assertEqual(result.exit_code, 0)
        data = json.loads(result.output)
        self.assertEqual(data["summary"]["ok"], 5)
        self.assertEqual(data["checks"]["pepa"]["status"], OK)

    def test_invalid_org_rejected(self):
        bad = self.root / "bad.yaml"
        bad.write_text("actors: [not-a-mapping]\n", encoding="utf-8")
        result = self.runner.invoke(
            self.main, ["telegram-check", "--org", str(bad)]
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Cannot verify", result.output)

    def test_missing_config_rejected(self):
        result = self.runner.invoke(
            self.main,
            [
                "telegram-check",
                "--org",
                str(AU_ORG),
                "--config",
                str(self.root / "missing.toml"),
            ],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Cannot verify", result.output)


if __name__ == "__main__":
    unittest.main()
