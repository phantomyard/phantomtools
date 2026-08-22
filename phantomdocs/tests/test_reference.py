import os
from unittest import mock

import yaml
from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.storage import read_reference

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
            "add", "--ref", f"file://{ext}",
            "--slug", "external.pdf",
            "--category", "1",
            "--owners", "ceo",
            "--org-yaml", str(org),
            "--root", root,
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
