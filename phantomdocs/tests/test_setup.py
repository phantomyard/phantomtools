"""Update-package tests (SPEC §13, issue #33): `pd setup` rendering."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from click.testing import CliRunner

from phantomdocs.cli import main

ORG = """\
version: 1
organization:
  id: example-org
policies:
  access_levels:
    level-1:
      categories: [1]
    level-2:
      categories: [1, 2]
    level-3:
      categories: [1, 2, 3]
  security_categories:
    category-1: { label: public }
    category-2: { label: confidential }
    category-3: { label: credentials }
    category-4: { label: "Sensitive project (umbrella)" }
    category-4-almaponia: { label: "Sensitive - ALMAPONIA" }
roles:
  - id: ceo
    access_level: level-3
    security_exceptions: [category-4]
  - id: chief_of_staff
    access_level: level-2
    security_exceptions: [category-3, category-4]
  - id: project_lead
    access_level: level-2
    security_exceptions: []
actors:
  - id: paco
    role: ceo
    actor_exceptions: []
  - id: alma
    role: project_lead
    actor_exceptions: [category-4-almaponia]
documents:
  namespace: au
  org_pubkey: npub1example
  inboxes:
    alma: { name: "AU Inbox/ALMAPONIA", id: "drive-id-123" }
"""


@pytest.fixture
def org_file(tmp_path):
    p = tmp_path / "org.yaml"
    p.write_text(ORG)
    return str(p)


def test_setup_renders_all_three_files(tmp_path, org_file):
    persona = tmp_path / "persona"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--org-yaml",
            org_file,
            "--actor",
            "alma",
            "--persona-dir",
            str(persona),
        ],
    )
    assert result.exit_code == 0, result.output

    docs = (persona / "kb" / "procedures" / "Documents.md").read_text()
    memory = (persona / "MEMORY.md").read_text()
    wrapper = (persona / "tools" / "documents.sh").read_text()

    assert "<!-- phantomdocs:start -->" in docs
    assert "<!-- phantomdocs:end -->" in docs
    assert "alma" in docs
    assert "category-4-almaponia" in docs
    assert "AU Inbox/ALMAPONIA" in docs
    assert "gdrive://drive-id-123" in docs

    assert "## Document management" in memory
    assert "alma" in memory

    assert wrapper.startswith("#!/usr/bin/env bash")
    assert "ACTOR='alma'" in wrapper
    assert "--actor" in wrapper
    assert os.access(persona / "tools" / "documents.sh", os.X_OK)


def test_setup_is_idempotent(tmp_path, org_file):
    persona = tmp_path / "persona"
    runner = CliRunner()
    args = [
        "setup",
        "--org-yaml",
        org_file,
        "--actor",
        "alma",
        "--persona-dir",
        str(persona),
    ]
    assert runner.invoke(main, args).exit_code == 0
    first = (persona / "kb" / "procedures" / "Documents.md").read_text()
    assert runner.invoke(main, args).exit_code == 0
    second = (persona / "kb" / "procedures" / "Documents.md").read_text()
    assert first == second


def test_setup_preserves_persona_content_around_markers(tmp_path, org_file):
    persona = tmp_path / "persona"
    (persona / "kb" / "procedures").mkdir(parents=True)
    (persona / "MEMORY.md").write_text(
        "# My persona memory\n\npersona-authored intro line\n"
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--org-yaml",
            org_file,
            "--actor",
            "alma",
            "--persona-dir",
            str(persona),
        ],
    )
    assert result.exit_code == 0, result.output
    memory = (persona / "MEMORY.md").read_text()
    assert "persona-authored intro line" in memory
    assert "## Document management" in memory
    # markers bound the generated block
    assert memory.index("<!-- phantomdocs:start -->") < memory.index(
        "## Document management"
    )


def test_setup_unknown_actor_fails_closed(tmp_path, org_file):
    persona = tmp_path / "persona"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--org-yaml",
            org_file,
            "--actor",
            "ghost",
            "--persona-dir",
            str(persona),
        ],
    )
    assert result.exit_code != 0
    assert "not an actor" in result.output


def test_setup_actor_without_inbox_renders_local_note(tmp_path):
    org_file = tmp_path / "org.yaml"
    no_inbox = ORG.replace(
        '    alma: { name: "AU Inbox/ALMAPONIA", id: "drive-id-123" }\n',
        "",
    )
    org_file.write_text(no_inbox)
    persona = tmp_path / "persona"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "setup",
            "--org-yaml",
            str(org_file),
            "--actor",
            "alma",
            "--persona-dir",
            str(persona),
        ],
    )
    assert result.exit_code == 0, result.output
    docs = (persona / "kb" / "procedures" / "Documents.md").read_text()
    assert "none declared" in docs


def _make_wrapper(tmp_path, org_file):
    """Render + write the generated wrapper, returning its path."""
    persona = tmp_path / "persona"
    runner = CliRunner()
    r = runner.invoke(
        main,
        [
            "setup",
            "--org-yaml",
            org_file,
            "--actor",
            "alma",
            "--persona-dir",
            str(persona),
        ],
    )
    assert r.exit_code == 0, r.output
    return persona / "tools" / "documents.sh"


def test_wrapper_injects_options_after_subcommand(tmp_path, org_file):
    """The generated wrapper must run (not fail on group-level options) and
    inject --actor/--org-yaml *after* the subcommand, only for commands that
    accept them, honouring an explicit operator override."""
    wrapper = _make_wrapper(tmp_path, org_file)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "argv.log"
    shim = bindir / "pd"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"open({str(log)!r}, 'a').write(repr(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}

    def run(*argv):
        return subprocess.run(
            [str(wrapper), *argv], env=env, capture_output=True, text=True, check=False
        )

    assert run("status").returncode == 0
    assert run("add", "foo.pdf", "--slug", "x").returncode == 0
    assert run("add", "--actor", "pepa", "bar.pdf").returncode == 0
    assert run("verify").returncode == 0

    import ast

    calls = [ast.literal_eval(line) for line in log.read_text().strip().splitlines()]
    # status: not in the actor/org-yaml command set -> no injection at all.
    assert calls[0] == ["status"]
    # add: both options injected after the subcommand, before the arguments.
    assert calls[1] == [
        "add",
        "--actor",
        "alma",
        "--org-yaml",
        str(org_file),
        "foo.pdf",
        "--slug",
        "x",
    ]
    # operator override: their --actor wins; only one --actor present.
    assert calls[2] == [
        "add",
        "--org-yaml",
        str(org_file),
        "--actor",
        "pepa",
        "bar.pdf",
    ]
    assert calls[2].count("--actor") == 1
    # verify: accepts --org-yaml but not --actor.
    assert calls[3] == ["verify", "--org-yaml", str(org_file)]


def test_wrapper_executes_against_real_pd(tmp_path, org_file):
    """End-to-end: the generated wrapper actually runs `pd` without the
    pre-fix `No such option: --actor` failure."""
    wrapper = _make_wrapper(tmp_path, org_file)

    root = tmp_path / "root"
    runner = CliRunner()
    assert (
        runner.invoke(
            main,
            ["init", "--org", "example-org", "--namespace", "au", "--root", str(root)],
        ).exit_code
        == 0
    )

    # The real `pd` entry point lives next to the interpreter running the
    # tests (the venv bin dir).
    bindir = os.path.dirname(sys.executable)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}

    p = subprocess.run(
        [str(wrapper), "status", "--root", str(root)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    assert "No such option" not in p.stderr

    p = subprocess.run(
        [str(wrapper), "search", "foo", "--root", str(root)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    assert "No such option" not in p.stderr
