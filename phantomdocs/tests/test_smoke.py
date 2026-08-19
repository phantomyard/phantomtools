import glob
import os

import yaml
from click.testing import CliRunner

from phantomdocs.cli import main


def _run(args):
    return CliRunner().invoke(main, args)


def test_init_add_search_verify(tmp_path):
    root = str(tmp_path)
    r = _run(["init", "--org", "demo", "--root", root])
    assert r.exit_code == 0, r.output

    doc = tmp_path / "hello.md"
    doc.write_text("# hello\n", encoding="utf-8")
    r = _run(["add", str(doc), "--slug", "hello.md", "--category", "1", "--root", root])
    assert r.exit_code == 0, r.output
    assert "urn:demo:doc:hello.md" in r.output

    r = _run(["search", "hello", "--root", root])
    assert r.exit_code == 0, r.output
    assert "hello.md" in r.output

    r = _run(["verify", "--root", root])
    assert r.exit_code == 0, r.output
    assert "verified 1 node" in r.output


def test_verify_detects_tamper(tmp_path):
    root = str(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "a.txt"
    doc.write_text("original", encoding="utf-8")
    assert _run(["add", str(doc), "--slug", "a.txt", "--root", root]).exit_code == 0

    blobs = glob.glob(os.path.join(root, "blobs", "*", "*"))
    assert blobs
    with open(blobs[0], "wb") as f:
        f.write(b"tampered")

    r = _run(["verify", "--root", root])
    assert r.exit_code != 0
    assert "content hash mismatch" in r.output


def test_init_refuses_existing(tmp_path):
    root = str(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    r = _run(["init", "--org", "demo", "--root", root])
    assert r.exit_code != 0


def test_mkdir_and_nested_add(tmp_path):
    root = str(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0

    r = _run(["mkdir", "--name", "actas", "--root", root])
    assert r.exit_code == 0, r.output
    assert "urn:demo:folder:actas" in r.output

    doc = tmp_path / "minuta.md"
    doc.write_text("# minuta\n", encoding="utf-8")
    r = _run(
        ["add", str(doc), "--slug", "minuta.md", "--folder", "actas", "--root", root]
    )
    assert r.exit_code == 0, r.output
    assert "urn:demo:doc:actas/minuta.md" in r.output

    r = _run(["verify", "--root", root])
    assert r.exit_code == 0, r.output
    assert "verified 2 node" in r.output


def test_tag_refs_and_get_by_ref(tmp_path):
    root = str(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "a.txt"
    doc.write_text("data", encoding="utf-8")
    assert _run(["add", str(doc), "--slug", "a.txt", "--root", root]).exit_code == 0

    r = _run(["tag", "latest", "a.txt", "--root", root])
    assert r.exit_code == 0, r.output
    assert "latest -> urn:demo:doc:a.txt" in r.output

    r = _run(["refs", "--root", root])
    assert r.exit_code == 0, r.output
    assert "latest" in r.output

    r = _run(["get", "latest", "--root", root])
    assert r.exit_code == 0, r.output
    assert "urn:demo:doc:a.txt" in r.output


def test_audit_log(tmp_path):
    root = str(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "a.txt"
    doc.write_text("data", encoding="utf-8")
    r = _run(["add", str(doc), "--slug", "a.txt", "--actor", "pepa", "--root", root])
    assert r.exit_code == 0, r.output

    r = _run(["audit", "--root", root])
    assert r.exit_code == 0, r.output
    assert "pepa" in r.output
    assert '"action": "add"' in r.output


def test_derive_manifest(tmp_path):
    org = tmp_path / "org.yaml"
    org.write_text("organization:\n  id: demo-org\n", encoding="utf-8")
    out = tmp_path / "manifest.yaml"
    r = _run(["derive-manifest", "--org-yaml", str(org), "--out", str(out)])
    assert r.exit_code == 0, r.output
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["manifest"]["org"] == "demo-org"
    assert data["manifest"]["rootMac"]
