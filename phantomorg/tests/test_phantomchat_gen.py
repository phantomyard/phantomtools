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

BRIDGE_NPUB = "npub1k5sucm83q6tg4a9qhz8vx6gu8m3x03ecnnr0klv6skzhv8elkfkstydrel"
HUMAN_NPUB = "npub1cml4wlfllw6mmw8esxgtslnka3scdxek7ecvh6vej7rtdjvzzd0s0cum9v"


def _minimal_org_with_agent_channel(tmp: Path, with_npub: bool = True) -> Path:
    org = tmp / "org.yaml"
    actor = (
        "{id: a, role: r, tools: [], npub: 'npub1ggyxfrue07z39dl0ag3lge3z8l7vtunlyrg9quwcdh4r84rnwq4s25aqa9'}"
        if with_npub
        else "{id: a, role: r, tools: []}"
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
        """Transport admission is not principal authorization: only explicitly
        configured human principal keys are trusted and onboarding is visible."""
        spec, _ = validate_org(AU_ORG)
        actors = {a.id: a for a in spec.actors}

        for actor in actors.values():
            pc = phantomchat_config(spec, actor)
            self.assertIsNotNone(pc)
            # Relays: private first, then the 5 public ones.
            self.assertEqual(pc.relays[0], "ws://relay.example.invalid:7777")
            self.assertEqual(len(pc.relays), 6)
            self.assertEqual(pc.allowed_npubs, [HUMAN_NPUB])
            self.assertNotIn(BRIDGE_NPUB, pc.allowed_npubs)
            self.assertEqual(pc.greeted, [])

    def test_au_phantomchat_config_shape(self):
        spec, _ = validate_org(AU_ORG)
        pc = phantomchat_config(spec, spec.actor_by_id("dana"))
        self.assertIsNotNone(pc)
        parsed = json.loads(pc.to_json())
        self.assertEqual(len(parsed["relays"]), 6)
        self.assertEqual(parsed["allowed_npubs"], [HUMAN_NPUB])
        self.assertEqual(parsed["greeted"], [])
        self.assertNotIn(BRIDGE_NPUB, parsed["allowed_npubs"])

    def test_generated_roundtrip_parses(self):
        spec, _ = validate_org(AU_ORG)
        pc = phantomchat_config(spec, spec.actor_by_id("dana"))
        parsed = json.loads(pc.to_json())
        self.assertEqual(list(parsed.keys()), ["relays", "allowed_npubs", "greeted"])
        self.assertEqual(len(parsed["relays"]), 6)
        self.assertEqual(parsed["allowed_npubs"], [HUMAN_NPUB])
        self.assertEqual(parsed["greeted"], [])

    def test_serializer_format(self):
        pc = PhantomchatConfig(
            relays=["ws://x:7777"],
            allowed_npubs=[
                "npub1ggyxfrue07z39dl0ag3lge3z8l7vtunlyrg9quwcdh4r84rnwq4s25aqa9"
            ],
            greeted=["npub1ggyxfrue07z39dl0ag3lge3z8l7vtunlyrg9quwcdh4r84rnwq4s25aqa9"],
        )
        text = pc.to_json()
        self.assertTrue(text.endswith("\n"))
        self.assertIn('  "relays": [', text)  # 2-space indent


if __name__ == "__main__":
    unittest.main()
