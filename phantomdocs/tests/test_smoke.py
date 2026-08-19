import glob
import os

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
