from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.update import is_newer


def test_is_newer():
    assert is_newer("0.2.0", "0.1.0") is True
    assert is_newer("0.1.0", "0.1.0") is False
    assert is_newer("0.1.0", "0.2.0") is False
    assert is_newer("1.0.0", "0.9.9") is True
    assert is_newer("0.2.10", "0.2.9") is True


def test_update_no_repo():
    r = CliRunner().invoke(main, ["update", "--repo", ""])
    assert r.exit_code == 2
    assert "no update repository" in r.output
