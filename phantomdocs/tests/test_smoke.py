import glob
import os
from unittest import mock

import yaml
from click.testing import CliRunner

from phantomdocs.cli import main

# A minimal PhantomOrg org.yaml for ACL enforcement in the CLI tests. Two
# actors: "roberto" (cfo, level-2 -> categories [1,2]) and "elena"
# (cfo + category-3 actor exception -> [1,2,3]). Both hold role "cfo", so a
# node owned by role id "cfo" is writable by either.
ORG_YAML = """\
version: 1
policies:
  access_levels:
    level-3: { label: Executive, categories: [1, 2, 3] }
    level-2: { label: Operative, categories: [1, 2] }
    level-1: { label: Restricted, categories: [1] }
  security_categories:
    category-1: { label: Public }
    category-2: { label: Confidential }
    category-3: { label: "Sensitive financial" }
roles:
  - id: cfo
    access_level: level-2
    security_exceptions: []
actors:
  - id: roberto
    role: cfo
    actor_exceptions: []
  - id: elena
    role: cfo
    actor_exceptions: [category-3]
"""


def _real_user() -> str:
    """The real OS account, matching cli._os_actor() (used by subprocess tests)."""
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def _run(args, env=None, actor="roberto"):
    runner = CliRunner()
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    with mock.patch("phantomdocs.cli._os_actor", return_value=actor):
        return runner.invoke(main, args, env=full_env)


def _org(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text(ORG_YAML, encoding="utf-8")
    return str(p)


def test_init_add_search_verify(tmp_path):
    root = str(tmp_path)
    org = _org(tmp_path)
    r = _run(["init", "--org", "demo", "--root", root])
    assert r.exit_code == 0, r.output

    doc = tmp_path / "hello.md"
    doc.write_text("# hello\n", encoding="utf-8")
    r = _run(
        [
            "add",
            str(doc),
            "--slug",
            "hello.md",
            "--category",
            "1",
            "--owners",
            "cfo",
            "--org-yaml",
            org,
            "--root",
            root,
        ]
    )
    assert r.exit_code == 0, r.output
    assert "urn:demo:doc:hello.md" in r.output

    r = _run(["search", "hello", "--org-yaml", org, "--root", root])
    assert r.exit_code == 0, r.output
    assert "hello.md" in r.output

    r = _run(["verify", "--root", root])
    assert r.exit_code == 0, r.output
    assert "verified 1 node" in r.output


def test_verify_detects_tamper(tmp_path):
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "a.txt"
    doc.write_text("original", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "a.txt",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ]
        ).exit_code
        == 0
    )

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
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0

    r = _run(
        [
            "mkdir",
            "--name",
            "actas",
            "--owners",
            "cfo",
            "--org-yaml",
            org,
            "--root",
            root,
        ]
    )
    assert r.exit_code == 0, r.output
    assert "urn:demo:folder:actas" in r.output

    doc = tmp_path / "minuta.md"
    doc.write_text("# minuta\n", encoding="utf-8")
    r = _run(
        [
            "add",
            str(doc),
            "--slug",
            "minuta.md",
            "--folder",
            "actas",
            "--owners",
            "cfo",
            "--org-yaml",
            org,
            "--root",
            root,
        ]
    )
    assert r.exit_code == 0, r.output
    assert "urn:demo:doc:actas/minuta.md" in r.output

    r = _run(["verify", "--root", root])
    assert r.exit_code == 0, r.output
    assert "verified 2 node" in r.output


def test_tag_refs_and_get_by_ref(tmp_path):
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "a.txt"
    doc.write_text("data", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "a.txt",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ]
        ).exit_code
        == 0
    )

    r = _run(["tag", "latest", "a.txt", "--org-yaml", org, "--root", root])
    assert r.exit_code == 0, r.output
    assert "latest -> " in r.output
    assert "urn:demo:doc:a.txt" in r.output

    r = _run(["refs", "--root", root])
    assert r.exit_code == 0, r.output
    assert "latest" in r.output

    r = _run(["get", "latest", "--org-yaml", org, "--root", root])
    assert r.exit_code == 0, r.output
    assert "urn:demo:doc:a.txt" in r.output


def test_audit_log(tmp_path):
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "a.txt"
    doc.write_text("data", encoding="utf-8")
    r = _run(
        [
            "add",
            str(doc),
            "--slug",
            "a.txt",
            "--owners",
            "cfo",
            "--org-yaml",
            org,
            "--root",
            root,
        ]
    )
    assert r.exit_code == 0, r.output

    r = _run(["audit", "--root", root])
    assert r.exit_code == 0, r.output
    # The audit records the authenticated OS actor (roberto), not a
    # self-asserted --actor label.
    assert "roberto" in r.output
    assert '"action": "add"' in r.output


def test_derive_manifest(tmp_path):
    org = tmp_path / "org.yaml"
    org.write_text("version: 1\norganization:\n  id: demo-org\n", encoding="utf-8")
    out = tmp_path / "manifest.yaml"
    r = _run(["derive-manifest", "--org-yaml", str(org), "--out", str(out)])
    assert r.exit_code == 0, r.output
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["manifest"]["org"] == "demo-org"
    assert data["manifest"]["rootMac"]


def test_versioning_and_history(tmp_path):
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0

    doc = tmp_path / "a.txt"
    doc.write_text("v1", encoding="utf-8")
    r = _run(
        [
            "add",
            str(doc),
            "--slug",
            "a.txt",
            "--owners",
            "cfo",
            "--org-yaml",
            org,
            "--root",
            root,
        ]
    )
    assert r.exit_code == 0, r.output
    assert "added" in r.output

    data = yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))
    v1_mac = data["nodes"][0]["mac"]

    r = _run(
        [
            "add",
            str(doc),
            "--slug",
            "a.txt",
            "--owners",
            "cfo",
            "--org-yaml",
            org,
            "--root",
            root,
        ]
    )
    assert r.exit_code == 0, r.output
    assert "unchanged" in r.output

    doc.write_text("v2", encoding="utf-8")
    r = _run(
        [
            "add",
            str(doc),
            "--slug",
            "a.txt",
            "--owners",
            "cfo",
            "--org-yaml",
            org,
            "--root",
            root,
        ]
    )
    assert r.exit_code == 0, r.output
    assert "versioned" in r.output

    r = _run(["versions", "a.txt", "--org-yaml", org, "--root", root])
    assert r.exit_code == 0, r.output
    assert "v1" in r.output and "v2" in r.output and "(current)" in r.output

    r = _run(
        ["get", "a.txt", "--mac", v1_mac, "--cat", "--org-yaml", org, "--root", root]
    )
    assert r.exit_code == 0, r.output
    assert "v1" in r.output

    r = _run(["get", "a.txt", "--cat", "--org-yaml", org, "--root", root])
    assert r.exit_code == 0, r.output
    assert "v2" in r.output

    r = _run(["verify", "--root", root])
    assert r.exit_code == 0, r.output
    assert "verified 2 node" in r.output


# ---------------------------------------------------------------------------
# ACL enforcement end-to-end (P1-1)
# ---------------------------------------------------------------------------


def test_get_denies_category_above_clearance(tmp_path):
    """An actor with [1,2] clearance cannot read a category-3 document."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "secret.md"
    doc.write_text("secret", encoding="utf-8")
    # elena can write category-3 (she has the actor exception).
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "secret.md",
                "--category",
                "3",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ],
            actor="elena",
        ).exit_code
        == 0
    )

    # roberto (clearance [1,2]) is denied reading category-3.
    r = _run(
        ["get", "secret.md", "--cat", "--org-yaml", org, "--root", root],
        actor="roberto",
    )
    assert r.exit_code != 0
    assert "denied" in r.output

    # elena can read it.
    r = _run(["get", "secret.md", "--org-yaml", org, "--root", root], actor="elena")
    assert r.exit_code == 0, r.output


def test_search_filters_nodes_by_clearance(tmp_path):
    """search hides nodes the actor cannot read (no existence leak)."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    pub = tmp_path / "pub.md"
    pub.write_text("public", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(pub),
                "--slug",
                "pub.md",
                "--category",
                "1",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ]
        ).exit_code
        == 0
    )
    sec = tmp_path / "secret.md"
    sec.write_text("secret", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(sec),
                "--slug",
                "secret.md",
                "--category",
                "3",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ],
            actor="elena",
        ).exit_code
        == 0
    )

    r = _run(["search", "secret", "--org-yaml", org, "--root", root], actor="roberto")
    assert r.exit_code == 0
    assert "secret.md" not in r.output  # hidden from roberto


def test_add_denied_above_clearance(tmp_path):
    """An actor cannot write a category it cannot read (fail-closed)."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "x.md"
    doc.write_text("x", encoding="utf-8")
    r = _run(
        [
            "add",
            str(doc),
            "--slug",
            "x.md",
            "--category",
            "3",
            "--owners",
            "cfo",
            "--org-yaml",
            org,
            "--root",
            root,
        ],
        actor="roberto",
    )
    assert r.exit_code != 0
    assert "denied" in r.output


def test_write_requires_org_yaml_fail_closed(tmp_path):
    """Without --org-yaml, content mutations are refused (fail-closed)."""
    root = str(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "x.md"
    doc.write_text("x", encoding="utf-8")
    r = _run(["add", str(doc), "--slug", "x.md", "--root", root])
    assert r.exit_code != 0
    assert "org-yaml" in r.output


def test_write_requires_owners_fail_closed(tmp_path):
    """A write with no declared owners is refused (spec §9: no rule -> denied)."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "x.md"
    doc.write_text("x", encoding="utf-8")
    r = _run(["add", str(doc), "--slug", "x.md", "--org-yaml", org, "--root", root])
    assert r.exit_code != 0
    assert "denied" in r.output


def test_write_denies_unrelated_same_category(tmp_path):
    """An actor who can READ the category but is not an owner is denied write."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "x.md"
    doc.write_text("x", encoding="utf-8")
    # roberto creates a node owned by the *actor* id "roberto".
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "x.md",
                "--owners",
                "roberto",
                "--org-yaml",
                org,
                "--root",
                root,
            ],
            actor="roberto",
        ).exit_code
        == 0
    )
    # elena can read category 1, but is not an owner -> versioning denied.
    doc.write_text("x2", encoding="utf-8")
    r = _run(
        ["add", str(doc), "--slug", "x.md", "--org-yaml", org, "--root", root],
        actor="elena",
    )
    assert r.exit_code != 0
    assert "denied" in r.output


def test_write_allows_role_owner(tmp_path):
    """A node owned by a ROLE id is writable by any actor holding that role."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "x.md"
    doc.write_text("x", encoding="utf-8")
    # elena writes, declaring role owner "cfo"; roberto (same role) re-writes.
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "x.md",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ],
            actor="elena",
        ).exit_code
        == 0
    )
    doc.write_text("x2", encoding="utf-8")
    r = _run(
        ["add", str(doc), "--slug", "x.md", "--org-yaml", org, "--root", root],
        actor="roberto",
    )
    assert r.exit_code == 0, r.output
    assert "versioned" in r.output


def test_read_denies_unknown_os_account(tmp_path):
    """An OS account that is not an actor in the org model is refused."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "x.md"
    doc.write_text("x", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "x.md",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ]
        ).exit_code
        == 0
    )

    r = _run(["get", "x.md", "--org-yaml", org, "--root", root], actor="intruder")
    assert r.exit_code != 0
    assert "not an actor" in r.output


def test_read_denied_without_os_identity(tmp_path):
    """When the OS identity cannot be resolved, reads are refused (fail-closed)."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "x.md"
    doc.write_text("x", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "x.md",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ]
        ).exit_code
        == 0
    )

    runner = CliRunner()
    with mock.patch("phantomdocs.cli._os_actor", return_value=None):
        r = runner.invoke(main, ["get", "x.md", "--org-yaml", org, "--root", root])
    assert r.exit_code != 0
    assert "OS identity" in r.output


def test_audit_chain_verifies(tmp_path):
    """The audit log is hash-chained; verify reports a broken chain."""
    root = str(tmp_path)
    org = _org(tmp_path)
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0
    doc = tmp_path / "a.txt"
    doc.write_text("data", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "a.txt",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ]
        ).exit_code
        == 0
    )
    doc.write_text("data2", encoding="utf-8")
    assert (
        _run(
            [
                "add",
                str(doc),
                "--slug",
                "a.txt",
                "--owners",
                "cfo",
                "--org-yaml",
                org,
                "--root",
                root,
            ]
        ).exit_code
        == 0
    )

    # Intact chain verifies.
    r = _run(["verify", "--root", root])
    assert r.exit_code == 0, r.output

    # Delete the middle audit line -> chain broken.
    audit = tmp_path / "audit.log"
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3
    audit.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
    r = _run(["verify", "--root", root])
    assert r.exit_code != 0
    assert "chain broken" in r.output


def test_local_two_slash_uri(tmp_path):
    """local://<root> (two slashes) resolves to <root>, not the cwd."""
    from phantomdocs.storage import LocalBackend, resolve_backend

    b = resolve_backend("local://" + str(tmp_path))
    assert isinstance(b, LocalBackend)
    assert os.path.abspath(b.root) == os.path.abspath(str(tmp_path))


def test_concurrent_adds_all_survive(tmp_path):
    """Concurrent `pd add` processes each append their own node; none is lost
    (inter-process manifest lock + unique temp files).

    Subprocesses resolve the actor via the real OS account, so the org model
    must declare that account as an actor with write access.
    """
    import subprocess
    import sys

    root = str(tmp_path)
    user = _real_user()
    org_p = tmp_path / "org.yaml"
    org_p.write_text(
        "version: 1\n"
        "policies:\n"
        "  access_levels:\n"
        "    level-2: { label: Operative, categories: [1, 2] }\n"
        "  security_categories:\n"
        "    category-1: { label: Public }\n"
        "roles:\n"
        "  - id: cfo\n"
        "    access_level: level-2\n"
        "    security_exceptions: []\n"
        "actors:\n"
        f"  - id: {user}\n"
        "    role: cfo\n"
        "    actor_exceptions: []\n",
        encoding="utf-8",
    )
    assert _run(["init", "--org", "demo", "--root", root]).exit_code == 0

    env = {**os.environ}
    procs = []
    for i in range(6):
        doc = tmp_path / f"doc{i}.md"
        doc.write_text(f"content {i}", encoding="utf-8")
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "phantomdocs.cli",
                    "add",
                    str(doc),
                    "--slug",
                    f"doc{i}.md",
                    "--owners",
                    "cfo",
                    "--org-yaml",
                    str(org_p),
                    "--root",
                    root,
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        )
    for p in procs:
        _, err = p.communicate()
        assert p.returncode == 0, err

    data = yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))
    nodes = [n for n in data["nodes"] if n.get("kind") == "doc"]
    assert len(nodes) == 6, f"expected 6 docs, got {len(nodes)}"
