import json
import tempfile
import unittest
from pathlib import Path

from phantomforge.compiler.build import build
from phantomforge.compiler.phantomchat_gen import (
    PHANTOMCHAT_FILENAME,
    PhantomchatConfig,
    phantomchat_config,
)
from phantomforge.spec.loader import load_org_yaml
from phantomforge.validator import validate_org

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"

BRIDGE_NPUB = "npub1klkkqdft4xmr2rxplzhyys6z7sypygm6k9k396lkua087d85ez2qs2kfmk"
HUMAN_NPUB = "npub1pvrd6n2kn3j6t8fl7d8nwqvjzvj8f45gkcfltwdc3pvnr8h0rkkqh7jlfh"


def _minimal_org_with_agent_channel(tmp: Path, with_npub: bool = True) -> Path:
    org = tmp / "org.yaml"
    actor = (
        "{id: a, role: r, tools: [], npub: 'npub10dkp4yu0tfmra36qx35xa4apj28np3acfsrqtf07xqtqs3sxlvlsg3zkmq'}"
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

            for actor_id in ("paco", "pepa", "roberto", "alma", "elena"):
                p = out_dir / actor_id / PHANTOMCHAT_FILENAME
                self.assertTrue(p.exists(), f"{actor_id} must get phantomchat.json")
                self.assertIn(p, written[actor_id])

    def test_au_allowlist_matches_deployed_shape(self):
        """The generated allowlist must match what is actually deployed:
        relays = private first + public; allowed = other actors + humans +
        bridge (no self); greeted = humans + bridge + other actors."""
        spec, _ = validate_org(AU_ORG)
        actors = {a.id: a for a in spec.actors}

        for actor_id, actor in actors.items():
            pc = phantomchat_config(spec, actor)
            self.assertIsNotNone(pc)
            # Relays: private first, then the 5 public ones.
            self.assertEqual(pc.relays[0], "ws://relay.example.invalid:7777")
            self.assertEqual(len(pc.relays), 6)
            # Allowed: 4 other actors + human + bridge = 6, no self.
            self.assertEqual(len(pc.allowed_npubs), 6)
            self.assertNotIn(actor.npub, pc.allowed_npubs)
            self.assertIn(BRIDGE_NPUB, pc.allowed_npubs)
            self.assertIn(HUMAN_NPUB, pc.allowed_npubs)
            # Greeted: same set, human + bridge first.
            self.assertEqual(len(pc.greeted), 6)
            self.assertEqual(pc.greeted[0], HUMAN_NPUB)
            self.assertEqual(pc.greeted[1], BRIDGE_NPUB)
            self.assertEqual(set(pc.greeted), set(pc.allowed_npubs))

    def test_au_matches_deployed_files_byte_for_byte(self):
        """Regression: the generated content for alma/roberto must equal the
        real files currently deployed on the MacBookPro (verified 2026-08-11
        18:xx during the phantomchat integration)."""
        spec, _ = validate_org(AU_ORG)

        deployed = {
            "alma": """{
  "relays": [
    "ws://relay.example.invalid:7777",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://nostr.mom",
    "wss://nostr.data.haus"
  ],
  "allowed_npubs": [
    "npub1p585a0cqnf949lc6jsfff49tqenqmxche2khtvsqfhn6p2a83qtspju756",
    "npub15g9murn5rz3yh20c6wwkczv8ztn5trz8gfmulaxf8zt77fxatx5skz0vwt",
    "npub1gthx35eejxlrkxc0faj7gpw5jy62hcw260g7ws95cq0x7hav58asyeuest",
    "npub1994zf2vg2pdsyg3gehhgahqu9mflz83azd7w5l3ukzdv6yk8fqgqd69axh",
    "npub1pvrd6n2kn3j6t8fl7d8nwqvjzvj8f45gkcfltwdc3pvnr8h0rkkqh7jlfh",
    "npub1klkkqdft4xmr2rxplzhyys6z7sypygm6k9k396lkua087d85ez2qs2kfmk"
  ],
  "greeted": [
    "npub1pvrd6n2kn3j6t8fl7d8nwqvjzvj8f45gkcfltwdc3pvnr8h0rkkqh7jlfh",
    "npub1klkkqdft4xmr2rxplzhyys6z7sypygm6k9k396lkua087d85ez2qs2kfmk",
    "npub1p585a0cqnf949lc6jsfff49tqenqmxche2khtvsqfhn6p2a83qtspju756",
    "npub15g9murn5rz3yh20c6wwkczv8ztn5trz8gfmulaxf8zt77fxatx5skz0vwt",
    "npub1gthx35eejxlrkxc0faj7gpw5jy62hcw260g7ws95cq0x7hav58asyeuest",
    "npub1994zf2vg2pdsyg3gehhgahqu9mflz83azd7w5l3ukzdv6yk8fqgqd69axh"
  ]
}
""",
        }

        for actor_id, expected in deployed.items():
            pc = phantomchat_config(spec, spec.actor_by_id(actor_id))
            self.assertEqual(pc.to_json(), expected, f"{actor_id} must match deploy")

    def test_actor_without_npub_gets_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            org = _minimal_org_with_agent_channel(Path(tmp), with_npub=False)
            spec = load_org_yaml(org)
            with tempfile.TemporaryDirectory() as tmp2:
                out_dir = Path(tmp2)
                written = build(spec, out_dir)
                self.assertNotIn(
                    out_dir / "a" / PHANTOMCHAT_FILENAME, written["a"]
                )
                self.assertFalse(
                    (out_dir / "a" / PHANTOMCHAT_FILENAME).exists()
                )

    def test_no_agent_channel_gets_no_file(self):
        """Backward compatible: orgs without an agent channel (no relay)
        produce no phantomchat.json even for npub-declaring actors."""
        with tempfile.TemporaryDirectory() as tmp:
            org = Path(tmp) / "org.yaml"
            org.write_text(
                "version: 1\n"
                "organization: {id: acme, name: ACME, sector: pyme, "
                "languages: [es]}\n"
                "departments: [{id: d, name: D, parent: null, "
                "access_policy: level-3}]\n"
                "roles: [{id: r, name: R, department: d, reports_to: null, "
                "access_level: level-3}]\n"
                "actors: [{id: a, role: r, tools: [], npub: 'npub10dkp4yu0tfmra36qx35xa4apj28np3acfsrqtf07xqtqs3sxlvlsg3zkmq'}]\n"
                "policies: {access_levels: {level-3: {label: L, "
                "categories: []}}, security_categories: {cat-1: {label: C}}}\n"
                "escalation_matrix: []\n"
                "communication: {request_id_format: x, "
                "message_types: [REQUEST], max_hops: 3}\n",
                encoding="utf-8",
            )
            spec = load_org_yaml(org)
            with tempfile.TemporaryDirectory() as tmp2:
                out_dir = Path(tmp2)
                written = build(spec, out_dir)
                self.assertFalse(
                    (out_dir / "a" / PHANTOMCHAT_FILENAME).exists()
                )

    def test_generated_roundtrip_parses(self):
        spec, _ = validate_org(AU_ORG)
        pc = phantomchat_config(spec, spec.actor_by_id("alma"))
        parsed = json.loads(pc.to_json())
        self.assertEqual(
            list(parsed.keys()), ["relays", "allowed_npubs", "greeted"]
        )
        self.assertEqual(len(parsed["relays"]), 6)
        self.assertEqual(len(parsed["allowed_npubs"]), 6)
        self.assertEqual(len(parsed["greeted"]), 6)

    def test_serializer_format(self):
        pc = PhantomchatConfig(
            relays=["ws://x:7777"],
            allowed_npubs=["npub10dkp4yu0tfmra36qx35xa4apj28np3acfsrqtf07xqtqs3sxlvlsg3zkmq"],
            greeted=["npub10dkp4yu0tfmra36qx35xa4apj28np3acfsrqtf07xqtqs3sxlvlsg3zkmq"],
        )
        text = pc.to_json()
        self.assertTrue(text.endswith("\n"))
        self.assertIn('  "relays": [', text)  # 2-space indent


if __name__ == "__main__":
    unittest.main()
