import json
import tempfile
import unittest
from pathlib import Path

from phantomorg.compiler.build import build
from phantomorg.compiler.phantomchat_gen import (
    PHANTOMCHAT_FILENAME,
    PhantomchatConfig,
    phantomchat_config,
)
from phantomorg.validator import validate_org

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"

BRIDGE_NPUB = "npub1w6huqqg6v56jpzu757j8d6gywxndmfl2fa28neqqzwnjzxete7psswsyx9"
HUMAN_NPUB = "npub1nn8csdm4tpjvveutqjavgxqdsndqw6w0ua2u0rd4050lyzzstq7s5usgk5"
PRINCIPAL_NPUB = "npub1dz4cat4eg4n9u6hhhr44xtff97az2cp80n0x66kp245j6u03rfjqzanzpp"


def _minimal_org_with_agent_channel(
    tmp: Path, with_npub: bool = True, principal: str | None = None
) -> Path:
    org = tmp / "org.yaml"
    actor = (
        "{id: a, role: r, tools: [], npub: 'npub1ax0ysc0rz74p3j3mreylczfc658setut8g4thqv80qk0y6td3ursy8jhvm'}"
        if with_npub
        else "{id: a, role: r, tools: []}"
    )
    principal_line = (
        f"principal_npubs: ['{principal}'], " if principal is not None else ""
    )
    org.write_text(
        "version: 1\n"
        "organization: {id: acme, name: ACME, sector: pyme, languages: [es]}\n"
        "departments: [{id: d, name: D, parent: null, access_policy: level-3}]\n"
        "roles: [{id: r, name: R, department: d, reports_to: null, "
        "access_level: level-3}]\n"
        f"actors: [{actor}]\n"
        "policies: {access_levels: {level-3: {label: L, categories: []}}, "
        "security_categories: {cat-1: {label: C}}}\n"
        "escalation_matrix: []\n"
        "communication: {request_id_format: x, message_types: [REQUEST], "
        "max_hops: 3, channels: {agent: {platform: phantomchat, "
        "relay: 'ws://relay.example:7777', "
        f"bridge_npub: '{BRIDGE_NPUB}', "
        f"human_npubs: ['{HUMAN_NPUB}'], "
        f"{principal_line}"
        "public_relays: ['wss://relay.damus.io', 'wss://nos.lol']}}}\n",
        encoding="utf-8",
    )
    return org


class TestPhantomchatGeneration(unittest.TestCase):
    def test_au_generates_phantomchat_for_all_actors(self):
        spec, result = validate_org(AU_ORG)
        self.assertTrue(result.ok)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            written = build(spec, out_dir)

            for actor_id in ("marco", "lucia", "diego", "dana", "elias"):
                p = out_dir / actor_id / PHANTOMCHAT_FILENAME
                self.assertTrue(p.exists(), f"{actor_id} must get phantomchat.json")
                self.assertIn(p, written[actor_id])

    def test_au_allowlist_matches_deployed_shape(self):
        """Transport admission is not principal authorization: the shared
        human group identity, the bridge and relays are delivery endpoints,
        never trusted. With no explicit principal_npubs, the allowlist is
        empty (fail-closed)."""
        spec, _ = validate_org(AU_ORG)
        actors = {a.id: a for a in spec.actors}

        for actor in actors.values():
            pc = phantomchat_config(spec, actor)
            self.assertIsNotNone(pc)
            # Relays: private first, then the 5 public ones.
            self.assertEqual(pc.relays[0], "ws://relay.example.invalid:7777")
            self.assertEqual(len(pc.relays), 6)
            # No principal designated -> empty allowlist (fail-closed).
            self.assertEqual(pc.allowed_npubs, [])
            self.assertNotIn(BRIDGE_NPUB, pc.allowed_npubs)
            self.assertNotIn(HUMAN_NPUB, pc.allowed_npubs)
            self.assertEqual(pc.greeted, [])

    def test_au_phantomchat_config_shape(self):
        spec, _ = validate_org(AU_ORG)
        pc = phantomchat_config(spec, spec.actor_by_id("dana"))
        self.assertIsNotNone(pc)
        parsed = json.loads(pc.to_json())
        self.assertEqual(len(parsed["relays"]), 6)
        self.assertEqual(parsed["allowed_npubs"], [])
        self.assertEqual(parsed["greeted"], [])
        self.assertNotIn(BRIDGE_NPUB, parsed["allowed_npubs"])
        self.assertNotIn(HUMAN_NPUB, parsed["allowed_npubs"])

    def test_generated_roundtrip_parses(self):
        spec, _ = validate_org(AU_ORG)
        pc = phantomchat_config(spec, spec.actor_by_id("dana"))
        parsed = json.loads(pc.to_json())
        self.assertEqual(list(parsed.keys()), ["relays", "allowed_npubs", "greeted"])
        self.assertEqual(len(parsed["relays"]), 6)
        self.assertEqual(parsed["allowed_npubs"], [])
        self.assertEqual(parsed["greeted"], [])

    def test_human_npubs_are_delivery_not_principal(self):
        """The shared human group identity (human_npubs) must NEVER become a
        trusted principal: with no principal_npubs, the allowlist stays empty
        even when a human npub is present in the channel config."""
        with tempfile.TemporaryDirectory() as tmp:
            org = _minimal_org_with_agent_channel(Path(tmp), principal=None)
            spec, _ = validate_org(org)
            pc = phantomchat_config(spec, spec.actor_by_id("a"))
            self.assertIsNotNone(pc)
            self.assertEqual(pc.allowed_npubs, [])
            self.assertNotIn(HUMAN_NPUB, pc.allowed_npubs)

    def test_principal_npubs_are_trusted_but_humans_are_not(self):
        """Only explicit principal_npubs enter allowed_npubs; the human group
        identity and the bridge stay screened by the threat judge."""
        with tempfile.TemporaryDirectory() as tmp:
            org = _minimal_org_with_agent_channel(Path(tmp), principal=PRINCIPAL_NPUB)
            spec, _ = validate_org(org)
            pc = phantomchat_config(spec, spec.actor_by_id("a"))
            self.assertIsNotNone(pc)
            self.assertEqual(pc.allowed_npubs, [PRINCIPAL_NPUB])
            self.assertNotIn(HUMAN_NPUB, pc.allowed_npubs)
            self.assertNotIn(BRIDGE_NPUB, pc.allowed_npubs)

    def test_serializer_format(self):
        pc = PhantomchatConfig(
            relays=["ws://x:7777"],
            allowed_npubs=[
                "npub1ax0ysc0rz74p3j3mreylczfc658setut8g4thqv80qk0y6td3ursy8jhvm"
            ],
            greeted=["npub1ax0ysc0rz74p3j3mreylczfc658setut8g4thqv80qk0y6td3ursy8jhvm"],
        )
        text = pc.to_json()
        self.assertTrue(text.endswith("\n"))
        self.assertIn('  "relays": [', text)  # 2-space indent


if __name__ == "__main__":
    unittest.main()
