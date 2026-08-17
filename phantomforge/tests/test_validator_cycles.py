import unittest
from pathlib import Path

from phantomforge.spec.loader import load_org_yaml
from phantomforge.spec.model import EscalationEntry
from phantomforge.validator.graph import EscalationCycleError, check_no_cycles

AU_ORG = Path(__file__).parent.parent / "organizations/aquaponics-united/org.yaml"


class TestValidatorCycles(unittest.TestCase):
    def test_au_org_has_no_cycles(self):
        spec = load_org_yaml(AU_ORG)
        check_no_cycles(spec)  # no debe lanzar

    def test_detects_direct_cycle(self):
        spec = load_org_yaml(AU_ORG)
        # We force a cycle: ceo escalates back to chief_of_staff
        # (chief_of_staff -> ceo already exists in the real spec)
        spec.escalation_matrix.append(
            EscalationEntry(
                from_="ceo", to="chief_of_staff", condition="artificial test cycle"
            )
        )
        with self.assertRaises(EscalationCycleError):
            check_no_cycles(spec)


if __name__ == "__main__":
    unittest.main()
