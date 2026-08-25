import os
from unittest import mock

import yaml
from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.storage import _shell_quote, location_uri, read_reference

ORG_YAML = """\
version: 1
policies:
  access_levels:
    level-3: { label: Executive, categories: [1, 2, 3] }
  security_categories:
    category-1: { label: Public }
roles:
  - id: ceo
    access_level: level-3
    security_exceptions: []
actors:
  - id: marco
    role: ceo
    actor_exceptions: []
"""


def _run(args, actor="marco", env=None):
    runner = CliRunner()
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    with mock.patch("phantomdocs.cli._os_actor", return_value=actor):
        return runner.invoke(main, args, env=full_env)


def test_read_reference_local_file(tmp_path):
    p = tmp_path / "doc.txt"
    p.write_bytes(b"hello reference")
    data, loc = read_reference(f"file://{p}")
    assert data == b"hello reference"
    assert loc == {"backend": "file", "ref": str(p)}


def test_read_reference_bare_path(tmp_path):
    p = tmp_path / "doc.txt"
    p.write_bytes(b"bare path")
    data, loc = read_reference(str(p))
    assert data == b"bare path"
    assert loc == {"backend": "file", "ref": str(p)}


def test_read_reference_gdrive(tmp_path):
    src = tmp_path / "in-drive.txt"
    src.write_bytes(b"gdrive bytes")
    ws = tmp_path / "fake-workspace.py"
    ws.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, shutil\n"
        "if sys.argv[1:3] == ['drive', 'download']:\n"
        f"    shutil.copyfile({str(src)!r}, sys.argv[4])\n",
        encoding="utf-8",
    )
    os.chmod(ws, 0o755)
    data, loc = read_reference("gdrive://abc123", workspace_py=str(ws))
    assert data == b"gdrive bytes"
    assert loc == {"backend": "gdrive", "ref": "abc123"}


def test_add_by_reference_no_local_copy(tmp_path):
    """`add --ref` records a reference location and does NOT store a blob."""
    root = str(tmp_path)
    org = tmp_path / "org.yaml"
    org.write_text(ORG_YAML, encoding="utf-8")
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0

    ext = tmp_path / "external.pdf"
    ext.write_bytes(b"%PDF-1.4 reference bytes")

    r = _run(
        [
            "add",
            "--ref",
            f"file://{ext}",
            "--slug",
            "external.pdf",
            "--category",
            "1",
            "--owners",
            "ceo",
            "--org-yaml",
            str(org),
            "--root",
            root,
        ]
    )
    assert r.exit_code == 0, r.output
    assert "added external.pdf" in r.output

    with open(f"{root}/manifest.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    docs = [n for n in data["nodes"] if n.get("kind") == "doc"]
    assert len(docs) == 1
    assert docs[0]["locations"][0] == {"backend": "file", "ref": str(ext)}
    # no blob copied into the content-addressed store
    assert os.listdir(f"{root}/blobs") == []


def test_add_requires_exactly_one_of_path_or_ref(tmp_path):
    root = str(tmp_path)
    org = tmp_path / "org.yaml"
    org.write_text(ORG_YAML, encoding="utf-8")
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    r = _run(["add", "--slug", "x.pdf", "--org-yaml", str(org), "--root", root])
    assert r.exit_code != 0
    assert "exactly one" in r.output


def test_shell_quote(tmp_path):
    assert _shell_quote("plain") == "'plain'"
    assert _shell_quote("a b") == "'a b'"
    # Embedded single quotes are escaped so the value is one literal argument.
    assert _shell_quote("it's.txt") == "'it'\\''s.txt'"


def test_read_reference_ssh_quotes_remote_path(monkeypatch):
    """A reference path with shell metacharacters must be read as a literal
    filename, not executed on the remote host."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = b"remote bytes"
        stderr = b""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr("phantomdocs.storage._run_checked", fake_run)

    data, _loc = read_reference(
        "ssh://user@host:2222/data/$(touch /tmp/pwned); rm -rf /"
    )
    remote_cmd = captured["args"][-1]
    assert remote_cmd == "cat '/data/$(touch /tmp/pwned); rm -rf /'"
    assert data == b"remote bytes"


def test_read_reference_ssh_preserves_connection(monkeypatch):
    """The stored reference must preserve host/user/port so get/verify can
    re-read it (a bare path reconstructs an empty target)."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = b"remote bytes"
        stderr = b""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setattr("phantomdocs.storage._run_checked", fake_run)

    uri = "ssh://bob@example.com:2222/data/report.pdf"
    _data, loc = read_reference(uri)
    assert captured["args"][0] == "ssh"
    assert captured["args"][captured["args"].index("-p") + 1] == "2222"
    assert "bob@example.com" in captured["args"]
    assert loc == {"backend": "ssh", "ref": uri}
    # Round-trip: reading the stored reference resolves the same target.
    data2, loc2 = read_reference(loc["ref"])
    assert data2 == b"remote bytes"
    assert loc2 == loc


def test_location_uri_reconstructs(tmp_path):
    assert (
        location_uri({"backend": "ssh", "ref": "ssh://u@h:2222/p"})
        == "ssh://u@h:2222/p"
    )
    assert location_uri({"backend": "file", "ref": "/abs/p"}) == "file:///abs/p"
    assert location_uri({"backend": "gdrive", "ref": "id123"}) == "gdrive://id123"
