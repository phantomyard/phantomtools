import tempfile
import unittest
from pathlib import Path

from phantomorg.compiler import build
from phantomorg.spec.loader import OrgSpecError, load_org_yaml
from phantomorg.validator import validate_org

AU_ORG = Path(__file__).parent.parent / "organizations/verdant-aquaponics/org.yaml"

NORMA_REL = "kb/procedures/comunicacion-agentes.md"


def _minimal_org_with_channels(tmp: Path) -> Path:
    """Minimal org declaring communication channels (opt-in for the norm)."""
    org = tmp / "org.yaml"
    org.write_text(
        "version: 1\n"
        "organization: {id: acme, name: ACME, sector: pyme, languages: [es]}\n"
        "departments: [{id: d, name: D, parent: null, access_policy: level-3}]\n"
        "roles: [{id: r, name: R, department: d, reports_to: null, "
        "access_level: level-3, description: 'Gestiona X. Contactar ante Y.'}]\n"
        "actors: [{id: a, role: r, tools: [], telegram_bot: '@R_bot'}]\n"
        "policies: {access_levels: {level-3: {label: L, categories: []}}, "
        "security_categories: {cat-1: {label: C}}}\n"
        "escalation_matrix: [{from: r, to: r, condition: duda}]\n"
        "communication: {request_id_format: '{org_id}-{yyyymmdd}-{seq4}', "
        "message_types: [REQUEST], max_hops: 3, norm_version: '1.2', "
        "channels: {human: {platform: telegram, group: 'G', "
        "chat_id: '-1001'}, agent: {platform: phantomchat, "
        "relay: 'ws://relay.example:7777'}}}\n",
        encoding="utf-8",
    )
    return org


class TestNormaCompiled(unittest.TestCase):
    def test_au_declares_channels_and_norm_version(self):
        spec = load_org_yaml(AU_ORG)
        self.assertEqual(spec.communication.norm_version, "1.5")
        self.assertIsNotNone(spec.communication.human_channel)
        self.assertIsNotNone(spec.communication.agent_channel)
        self.assertEqual(spec.communication.human_channel.group, "Coordinación")
        self.assertEqual(spec.communication.human_channel.chat_id, "-1000000000001")
        self.assertEqual(
            spec.communication.agent_channel.relay, "ws://relay.example.invalid:7777"
        )
        # Envelope (norma v1.5)
        self.assertIsNotNone(spec.communication.envelope)
        self.assertEqual(spec.communication.envelope.marker, "[env]")
        self.assertEqual(spec.communication.envelope.ttl_hours, 6)
        # F2-12: trace_agents eliminado (config muerta sin efecto runtime)
        self.assertFalse(hasattr(spec.communication.envelope, "trace_agents"))
        self.assertEqual(spec.communication.max_hops, 3)

    def test_au_role_descriptions_parsed(self):
        spec = load_org_yaml(AU_ORG)
        self.assertIn("Presupuestos", spec.role_by_id("cfo").description)
        self.assertIn("Greenroot", spec.role_by_id("project_lead").description)

    def test_build_writes_norma_when_channels_present(self):
        spec, result = validate_org(AU_ORG)
        self.assertTrue(result.ok)

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            written = build(spec, out_dir, only="dana")

            norma_path = out_dir / "dana" / NORMA_REL
            self.assertTrue(norma_path.exists(), "norma must be compiled")
            self.assertIn(norma_path, written["dana"])

            text = norma_path.read_text(encoding="utf-8")
            # Channels, request format, relay, hierarchy, roles, escalation.
            self.assertIn("Coordinación", text)
            self.assertIn("-1000000000001", text)
            self.assertIn("ws://relay.example.invalid:7777", text)
            self.assertIn("verdant-aquaponics-{yyyymmdd}-{seq4}", text)
            self.assertIn("Versión", text)
            self.assertIn("1.5", text)
            self.assertIn("phantomchat", text)
            self.assertIn("@marco_bot", text)
            self.assertIn("Greenroot", text)
            self.assertIn("Board President", text)
            self.assertIn("REQUEST", text)
            self.assertIn("Matriz de escalado", text)
            # Envelope section (norma v1.5)
            self.assertIn("[env]", text)
            self.assertIn("Envelope de protocolo", text)
            self.assertIn("Regla del rid", text)
            self.assertIn("Ciclo de vida del bucle (bot-loop)", text)
            self.assertIn("R1 — Timeout de petición", text)
            self.assertIn("R2 — Límite de reintentos", text)
            self.assertIn("R3 — El rid es único por petición", text)
            self.assertIn("hops", text)
            self.assertIn("trace", text)
            self.assertIn("expires", text)
            self.assertIn("max_hops", text)

    def test_norma_is_fully_regenerated_with_org_changes(self):
        """The norm is compiled state (write_if_changed), NOT a seed: a
        change in org.yaml (e.g. norm_version bump) must propagate to the
        compiled file on the next build."""
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="dana")
            norma_path = out_dir / "dana" / NORMA_REL
            self.assertIn("1.5", norma_path.read_text(encoding="utf-8"))

            # Bump the norm version in the spec -> next build rewrites.
            spec.communication.norm_version = "1.6"
            written = build(spec, out_dir, only="dana")
            self.assertIn(norma_path, written["dana"])
            self.assertIn("1.6", norma_path.read_text(encoding="utf-8"))

    def test_build_writes_concise_norm_into_judge_drawer(self):
        """memory/norms.md is one of the drawers the threat judge reads in
        full. The compiler must block-merge a CONCISE operational norm into
        it (an ORG:BEGIN/END block), not just a pointer to the KB — so the
        judge is briefed on routine traffic. The full protocol page stays in
        the KB; the drawer block is the short summary."""
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="dana")
            drawer = (out_dir / "dana" / "memory" / "norms.md").read_text(
                encoding="utf-8"
            )
            # The drawer carries an owned ORG block with the concise rules.
            self.assertIn("ORG:BEGIN norms", drawer)
            self.assertIn("ORG:END norms", drawer)
            # Interim hardening: the block opens with a deploy-date
            # `## YYYY-MM-DD` header so phantombot's drawer-ingest
            # parser files entries with real dates, not file mtime.
            self.assertRegex(
                drawer,
                r"<!-- ORG:BEGIN norms -->\s*\n## \d{4}-\d{2}-\d{2}\n",
            )
            # The concise block names the channels / request-id format the
            # judge needs to recognize routine traffic.
            self.assertIn("telegram", drawer.lower())
            self.assertIn("phantomchat", drawer.lower())
            # The full protocol page is referenced for the human-readable copy.
            self.assertIn("[[procedures/comunicacion-agentes]]", drawer)

    def test_norm_drawer_one_bullet_per_line(self):
        """The concise drawer must file one bullet per line: phantombot's
        drawer-ingest parser splits on newlines, and a single line carrying
        two bullets gets filed as one mangled entry (the regression this
        template change exists to prevent)."""
        spec, _ = validate_org(AU_ORG)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            build(spec, out_dir, only="dana")
            drawer = (out_dir / "dana" / "memory" / "norms.md").read_text(
                encoding="utf-8"
            )
            block = drawer.split("<!-- ORG:BEGIN norms -->")[1].split(
                "<!-- ORG:END norms -->"
            )[0]
            bullets = [ln for ln in block.splitlines() if ln.lstrip().startswith("- ")]
            self.assertGreaterEqual(len(bullets), 5)
            # No line may carry more than one bullet (trim_blocks regression).
            for ln in bullets:
                self.assertNotIn("- ", ln[2:], f"two bullets on one line: {ln!r}")

    def test_build_skips_norma_without_channels(self):
        """Backward compatible: orgs without channels get no norm file."""
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
                "actors: [{id: a, role: r, tools: []}]\n"
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
                written = build(spec, out_dir, only="a")
                self.assertNotIn(out_dir / "a" / NORMA_REL, written["a"])
                self.assertFalse((out_dir / "a" / NORMA_REL).exists())

    def test_channels_invalid_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            org = Path(tmp) / "org.yaml"
            org.write_text(
                "version: 1\n"
                "organization: {id: acme, name: ACME, sector: pyme, "
                "languages: [es]}\n"
                "departments: [{id: d, name: D, parent: null, "
                "access_level: level-3}]\n"
                "roles: [{id: r, name: R, department: d, reports_to: null, "
                "access_level: level-3}]\n"
                "actors: [{id: a, role: r, tools: []}]\n"
                "policies: {access_levels: {level-3: {label: L, "
                "categories: []}}, security_categories: {cat-1: {label: C}}}\n"
                "escalation_matrix: []\n"
                "communication: {request_id_format: x, "
                "message_types: [REQUEST], max_hops: 3, "
                "channels: {human: {platform: telegram, bogus: 1}}}\n",
                encoding="utf-8",
            )
            with self.assertRaises(OrgSpecError):
                load_org_yaml(org)

    def test_channels_missing_platform_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            org = Path(tmp) / "org.yaml"
            org.write_text(
                "version: 1\n"
                "organization: {id: acme, name: ACME, sector: pyme, "
                "languages: [es]}\n"
                "departments: [{id: d, name: D, parent: null, "
                "access_level: level-3}]\n"
                "roles: [{id: r, name: R, department: d, reports_to: null, "
                "access_level: level-3}]\n"
                "actors: [{id: a, role: r, tools: []}]\n"
                "policies: {access_levels: {level-3: {label: L, "
                "categories: []}}, security_categories: {cat-1: {label: C}}}\n"
                "escalation_matrix: []\n"
                "communication: {request_id_format: x, "
                "message_types: [REQUEST], max_hops: 3, "
                "channels: {agent: {relay: 'ws://x:7777'}}}\n",
                encoding="utf-8",
            )
            with self.assertRaises(OrgSpecError):
                load_org_yaml(org)

    def test_envelope_invalid_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            org = Path(tmp) / "org.yaml"
            org.write_text(
                "version: 1\n"
                "organization: {id: acme, name: ACME, sector: pyme, "
                "languages: [es]}\n"
                "departments: [{id: d, name: D, parent: null, "
                "access_level: level-3}]\n"
                "roles: [{id: r, name: R, department: d, reports_to: null, "
                "access_level: level-3}]\n"
                "actors: [{id: a, role: r, tools: []}]\n"
                "policies: {access_levels: {level-3: {label: L, "
                "categories: []}}, security_categories: {cat-1: {label: C}}}\n"
                "escalation_matrix: []\n"
                "communication: {request_id_format: x, "
                "message_types: [REQUEST], max_hops: 3, "
                "envelope: {marker: '[env]', bogus: 1}}\n",
                encoding="utf-8",
            )
            with self.assertRaises(OrgSpecError):
                load_org_yaml(org)

    def test_envelope_invalid_ttl_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            org = Path(tmp) / "org.yaml"
            org.write_text(
                "version: 1\n"
                "organization: {id: acme, name: ACME, sector: pyme, "
                "languages: [es]}\n"
                "departments: [{id: d, name: D, parent: null, "
                "access_level: level-3}]\n"
                "roles: [{id: r, name: R, department: d, reports_to: null, "
                "access_level: level-3}]\n"
                "actors: [{id: a, role: r, tools: []}]\n"
                "policies: {access_levels: {level-3: {label: L, "
                "categories: []}}, security_categories: {cat-1: {label: C}}}\n"
                "escalation_matrix: []\n"
                "communication: {request_id_format: x, "
                "message_types: [REQUEST], max_hops: 3, "
                "envelope: {marker: '[env]', ttl_hours: 0}}\n",
                encoding="utf-8",
            )
            with self.assertRaises(OrgSpecError):
                load_org_yaml(org)

    def test_envelope_marker_must_be_fixed_const(self):
        # F2-03: the marker is a protocol constant (hardcoded in the bridge);
        # any other value must be rejected at build time.
        with tempfile.TemporaryDirectory() as tmp:
            org = Path(tmp) / "org.yaml"
            org.write_text(
                "version: 1\n"
                "organization: {id: acme, name: ACME, sector: pyme, "
                "languages: [es]}\n"
                "departments: [{id: d, name: D, parent: null, "
                "access_level: level-3}]\n"
                "roles: [{id: r, name: R, department: d, reports_to: null, "
                "access_level: level-3}]\n"
                "actors: [{id: a, role: r, tools: []}]\n"
                "policies: {access_levels: {level-3: {label: L, "
                "categories: []}}, security_categories: {cat-1: {label: C}}}\n"
                "escalation_matrix: []\n"
                "communication: {request_id_format: x, "
                "message_types: [REQUEST], max_hops: 3, "
                "envelope: {marker: '[protocol]'}}\n",
                encoding="utf-8",
            )
            with self.assertRaises(OrgSpecError):
                load_org_yaml(org)

    def test_envelope_trace_agents_rejected(self):
        # F2-12: trace_agents removed from the config (no runtime
        # effect); declaring it must be a shape error.
        with tempfile.TemporaryDirectory() as tmp:
            org = Path(tmp) / "org.yaml"
            org.write_text(
                "version: 1\n"
                "organization: {id: acme, name: ACME, sector: pyme, "
                "languages: [es]}\n"
                "departments: [{id: d, name: D, parent: null, "
                "access_level: level-3}]\n"
                "roles: [{id: r, name: R, department: d, reports_to: null, "
                "access_level: level-3}]\n"
                "actors: [{id: a, role: r, tools: []}]\n"
                "policies: {access_levels: {level-3: {label: L, "
                "categories: []}}, security_categories: {cat-1: {label: C}}}\n"
                "escalation_matrix: []\n"
                "communication: {request_id_format: x, "
                "message_types: [REQUEST], max_hops: 3, "
                "envelope: {marker: '[env]', trace_agents: true}}\n",
                encoding="utf-8",
            )
            with self.assertRaises(OrgSpecError):
                load_org_yaml(org)


if __name__ == "__main__":
    unittest.main()
