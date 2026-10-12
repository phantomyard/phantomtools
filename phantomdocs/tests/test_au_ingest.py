"""AU glue regression tests; fictional org data, no deployment credentials."""

import importlib.util
import io
import multiprocessing
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner
from phantomdocs.cli import main as pd
from phantomdocs.content import FileContent
from phantomdocs.manifest import load as load_manifest

spec = importlib.util.spec_from_file_location(
    "au_ingest",
    Path(__file__).parents[1] / "contrib/au/ingest_bandejas.py",
)
au = importlib.util.module_from_spec(spec)
spec.loader.exec_module(au)


@pytest.fixture
def org():
    roles = {
        "chief_of_staff",
        "project_lead",
        "ceo",
        "cfo",
        "volunteer_lead",
        "marketing",
        "training_lead",
    }
    domain_owners = {
        "direction": ["ceo"],
        "secretariat": ["chief_of_staff"],
        "finance": ["cfo"],
        "projects": ["project_lead", "chief_of_staff", "cfo"],
        "volunteering": ["volunteer_lead"],
        "marketing": ["marketing"],
        "training": ["training_lead"],
    }
    return {
        "version": 1,
        "organization": {"id": "aquaponics-united"},
        "policies": {
            "access_levels": {"level-2": {"categories": [1, 2]}},
            "security_categories": {
                "category-1": {},
                "category-2": {},
                "category-3": {},
                "category-4-almaponia": {},
            },
        },
        "roles": [
            {
                "id": role,
                "access_level": "level-2",
                "reports_to": None,
                "security_exceptions": ["category-3"]
                if role == "chief_of_staff"
                else [],
            }
            for role in sorted(roles)
        ],
        "actors": [
            {"id": "pepa", "role": "chief_of_staff", "actor_exceptions": []},
            {
                "id": "alma",
                "role": "project_lead",
                "actor_exceptions": ["category-4-almaponia"],
            },
            {"id": "outsider", "role": "unknown", "actor_exceptions": []},
        ],
        "documents": {
            "namespace": "au",
            "inboxes": {
                actor: {"id": f"inbox-{actor}", "name": actor}
                for actor in [
                    "paco",
                    "pepa",
                    "roberto",
                    "voluntariado",
                    "carla",
                    "elena",
                ]
            },
            "naming": {
                "domains": [
                    {
                        "id": domain,
                        "category": 2,
                        "owners": owners,
                        "types": [{"id": "minutes"}]
                        if domain == "secretariat"
                        else [{"id": "almaponia"}]
                        if domain == "projects"
                        else [],
                    }
                    for domain, owners in domain_owners.items()
                ]
            },
        },
    }


@pytest.mark.parametrize(
    "name,kind",
    [
        ("X.txt", "transcription"),
        ("X.resumen.md", "summary"),
        ("X.acta.md", "minutes"),
        ("X.transcripcion.pdf", "transcription"),
        ("X_2026-10-09-10-30-00.resumen.md", "summary"),
        ("X.resumen_2026-10-09-10-30-00.md", "summary"),
        ("X_2026-10-09-10-30-00.txt", "transcription"),
    ],
)
def test_one_meeting_key(name, kind):
    assert au.artifact(name) == ("x", kind)


def test_no_lossy_grouping_or_substring_classification():
    assert au.artifact("2026-10-09-10-30_avila.v2.resumen.md") == (
        "2026-10-09-10-30_avila.v2",
        "summary",
    )
    assert au.artifact("resumen-of-budget.pdf") is None
    assert au.artifact("AU-2026-10-09-001_GO_title_EVID.txt") is None
    assert au.artifact("unknown.xlsx") is None
    assert au.artifact("résumé.txt")[0] != au.artifact("resume.txt")[0]
    assert au.slug_for("X.txt", "Aa") != au.slug_for("X.txt", "aa")


def files(source="almaponia", key="X"):
    return [
        {
            "id": f"id-{i}",
            "name": f"{key}{suffix}",
            "source": source,
            "mimeType": "application/octet-stream",
        }
        for i, suffix in enumerate([".txt", ".resumen.md", ".acta.md"])
    ]


def test_complete_and_incomplete_groups(org):
    assert len(au.plan(org, files(), {}, "pepa")) == 3
    assert au.plan(org, files()[:1], {}, "pepa") == []
    assert len(au.plan(org, files() + [dict(files()[0])], {}, "pepa")) == 3
    pdf = files()
    pdf[0]["name"] = "X.transcripcion.pdf"
    assert len(au.plan(org, pdf, {}, "pepa")) == 3


def test_classification_and_department_custody(org):
    assert au.policy(org, "almaponia", True, "pepa") == (
        "category-2",
        ["chief_of_staff", "project_lead"],
    )
    # A finance meeting is category-2 minutes, not finance's category-3 content.
    org["documents"]["naming"]["domains"][2]["category"] = 3
    assert au.policy(org, "finance", False, "pepa") == (
        "category-2",
        ["cfo", "chief_of_staff"],
    )
    assert au.source_folder(org, "almaponia") == au.GO_ARCHIVE
    assert au.source_folder(org, "secretariat") == "inbox-pepa"


@pytest.mark.parametrize("change", ["category", "owner", "alma", "custodian"])
def test_policy_changes_fail_closed(org, change):
    if change == "category":
        org["documents"]["naming"]["domains"][3]["category"] = 1
    elif change == "owner":
        org["documents"]["naming"]["domains"][3]["owners"] = ["cfo"]
    elif change == "alma":
        org["actors"][1]["role"] = "unknown"
        org["actors"][1]["actor_exceptions"] = []
    else:
        org["documents"]["naming"]["domains"][1]["owners"] = ["ceo"]
    with pytest.raises(ValueError):
        au.policy(org, "almaponia", True, "pepa")


def test_branded_catalog_pins_names_and_requires_completeness(org):
    items = files()
    for i, f in enumerate(items):
        f["name"] = f"AU-2026-10-09-00{i}_GO_title_EVID.pdf"
    catalog = {
        f["id"]: {
            "name": f["name"],
            "source": "almaponia",
            "meeting": "meeting-1",
            "class": kind,
            "project": True,
        }
        for f, kind in zip(items, ["transcription", "summary", "minutes"])
    }
    assert len(au.plan(org, items, catalog, "pepa")) == 3
    items[0]["name"] = "changed.pdf"
    with pytest.raises(ValueError, match="no longer matches"):
        au.plan(org, items, catalog, "pepa")


def test_drive_pagination_and_incomplete_search():
    drive = au.Drive.__new__(au.Drive)
    calls = []

    def get(url, params, timeout):
        calls.append(dict(params))
        data = {"files": [{"id": str(len(calls))}]}
        if len(calls) == 1:
            data["nextPageToken"] = "next"
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    drive.session = SimpleNamespace(get=get)
    assert drive.list_files("folder") == [{"id": "1"}, {"id": "2"}]
    assert calls[1]["pageToken"] == "next"
    drive.session.get = lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"files": [], "incompleteSearch": True},
    )
    with pytest.raises(ValueError, match="incomplete search"):
        drive.list_files("folder")


@pytest.fixture
def sandbox(tmp_path, org, monkeypatch):
    if not hasattr(os, "fchmod"):
        # Existing product POSIX mode preservation is outside this glue's
        # scope. Windows exercises content/ACL/ref logic, not Unix modes.
        monkeypatch.setattr(os, "fchmod", lambda *args: None, raising=False)
    root = tmp_path / "store"
    model = tmp_path / "org.yaml"
    model.write_text(yaml.safe_dump(org), encoding="utf-8")
    runner = CliRunner()
    monkeypatch.setattr("phantomdocs.cli._os_actor", lambda: "pepa")

    def invoke(command):
        result = runner.invoke(pd, command)
        assert result.exit_code == 0, result.output
        return result

    invoke(
        ["init", "--org", "aquaponics-united", "--namespace", "au", "--root", str(root)]
    )
    invoke(
        [
            "mkdir",
            "--name",
            "reuniones",
            "--category",
            "2",
            "--owners",
            "chief_of_staff",
            "--org-yaml",
            str(model),
            "--root",
            str(root),
        ]
    )
    state = tmp_path / "seen"
    state.mkdir()
    reads, adds = [], []

    def read(uri, **kwargs):
        reads.append(uri)
        return FileContent.from_stream(io.BytesIO(uri.encode())), {
            "backend": "gdrive",
            "ref": uri[9:],
        }

    monkeypatch.setattr("phantomdocs.cli.read_reference", read)

    def run(command, **kwargs):
        adds.append(command)
        result = runner.invoke(pd, command[1:])
        return SimpleNamespace(
            returncode=result.exit_code, stdout=result.output, stderr=""
        )

    options = {
        "org": org,
        "root": root,
        "state": state,
        "docs": "documents.sh",
        "org_yaml": model,
        "actor": "pepa",
        "folder": "reuniones",
        "dry_run": False,
        "run": run,
    }
    return SimpleNamespace(
        root=root,
        model=model,
        state=state,
        reads=reads,
        adds=adds,
        options=options,
        runner=runner,
        invoke=invoke,
    )


def test_functional_fake_drive_recovery_and_rerun(org, sandbox, monkeypatch, tmp_path):
    items = files()
    monkeypatch.setattr(
        au,
        "Drive",
        lambda *a: SimpleNamespace(
            list_files=lambda folder: [
                {k: v for k, v in f.items() if k != "source"} for f in items
            ]
        ),
    )
    monkeypatch.setattr(au, "ingest_lock", lambda state: nullcontext())
    monkeypatch.setattr(au.subprocess, "run", sandbox.options["run"])
    # main's default run is bound at definition time, so wrap execute to inject
    # the fake wrapper while keeping the real pd CLI, ACL, manifest and hashing.
    original_execute = au.execute
    monkeypatch.setattr(
        au, "execute", lambda **kw: original_execute(**kw, run=sandbox.options["run"])
    )
    args = [
        "--org-yaml",
        str(sandbox.model),
        "--root",
        str(sandbox.root),
        "--docs",
        "documents.sh",
        "--service-account",
        "unused",
        "--subject",
        "unused",
        "--source",
        "almaponia",
        "--state",
        str(sandbox.state),
    ]
    assert au.main(args) == 0
    assert len(sandbox.reads) == 3
    assert not list((sandbox.root / "blobs").iterdir())
    manifest = load_manifest(str(sandbox.root / "manifest.yaml"))
    docs = [n for n in manifest["nodes"] if n["kind"] == "doc"]
    assert len(docs) == 3
    assert all(
        n["category"] == "category-2"
        and n["owners"] == ["chief_of_staff", "project_lead"]
        for n in docs
    )
    # Simulate commit followed by lost marker; refs must still prevent all reads.
    for marker in sandbox.state.iterdir():
        marker.unlink()
    before = (sandbox.root / "manifest.yaml").read_bytes()
    assert au.main(args) == 0
    assert len(sandbox.reads) == len(sandbox.adds) == 3
    assert (sandbox.root / "manifest.yaml").read_bytes() == before
    monkeypatch.setattr("phantomdocs.cli._os_actor", lambda: "alma")
    result = sandbox.runner.invoke(
        pd,
        ["search", "x", "--org-yaml", str(sandbox.model), "--root", str(sandbox.root)],
    )
    assert result.exit_code == 0 and result.output.count("doc ") == 3
    monkeypatch.setattr("phantomdocs.cli._os_actor", lambda: "outsider")
    denied = sandbox.runner.invoke(
        pd,
        ["search", "x", "--org-yaml", str(sandbox.model), "--root", str(sandbox.root)],
    )
    assert "no matches" in denied.output


def test_dry_run_and_failed_add_leave_no_markers(org, sandbox):
    actions = au.plan(org, files(), {}, "pepa")
    before = (sandbox.root / "manifest.yaml").read_bytes()
    assert au.execute(actions=actions, **{**sandbox.options, "dry_run": True}) == 0
    assert not sandbox.reads and not list(sandbox.state.iterdir())
    assert (sandbox.root / "manifest.yaml").read_bytes() == before
    fail = lambda *a, **k: SimpleNamespace(returncode=1)
    assert au.execute(actions=actions, **{**sandbox.options, "run": fail}) == 1
    assert not list(sandbox.state.iterdir())


def test_orphan_marker_and_existing_wrong_category_fail_closed(org, sandbox):
    actions = au.plan(org, files(), {}, "pepa")
    (sandbox.state / actions[-1]["id"]).touch()
    with pytest.raises(ValueError, match="orphan"):
        au.execute(actions=actions, **sandbox.options)
    assert not sandbox.reads  # batch preflight, no partial writes
    (sandbox.state / actions[-1]["id"]).unlink()
    sandbox.invoke(
        [
            "add",
            "--ref",
            "gdrive://id-0",
            "--slug",
            "old",
            "--category",
            "1",
            "--owners",
            "chief_of_staff",
            "--org-yaml",
            str(sandbox.model),
            "--root",
            str(sandbox.root),
        ]
    )
    with pytest.raises(ValueError, match="different classification"):
        au.execute(actions=actions, **sandbox.options)
    assert len(sandbox.reads) == 1


def test_unknown_catalog_ids_and_multiple_sources_fail_closed(org):
    with pytest.raises(ValueError, match="not found"):
        au.plan(org, files(), {"missing": {}}, "pepa")
    extra = dict(files()[0], source="secretariat")
    with pytest.raises(ValueError, match="multiple sources"):
        au.plan(org, files() + [extra], {}, "pepa")


def test_catalog_selection_preserves_branded_names(tmp_path, monkeypatch):
    items = files()
    for item in items:
        item["name"] = item["id"] + "_EVID.pdf"
    answers = iter(
        [
            "1,2,3",
            "last-go",
            "transcription",
            "last-go",
            "summary",
            "last-go",
            "minutes",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))
    target = tmp_path / "recovery.json"
    au.prepare_catalog(items, target)
    import json

    catalog = json.loads(target.read_text())
    assert set(catalog) == {f["id"] for f in items}
    assert all(catalog[f["id"]]["name"] == f["name"] for f in items)


def test_branded_and_avila_recovery(org, sandbox):
    project_items = files()
    for i, f in enumerate(project_items):
        f["name"] = f"AU-2026-10-09-00{i}_GO_last_EVID.pdf"
    avila = files(source="secretariat", key="2026-10-09-10-30_coordinacion_nodo_avila")
    for f in avila:
        f["id"] = "avila-" + f["id"]
    catalog = {
        f["id"]: {
            "source": f["source"],
            "name": f["name"],
            "meeting": "last-go",
            "class": kind,
            "project": True,
        }
        for f, kind in zip(project_items, ["transcription", "summary", "minutes"])
    }
    actions = au.plan(org, project_items + avila, catalog, "pepa")
    assert len(actions) == 6
    assert au.execute(actions=actions, **sandbox.options) == 0
    assert len(sandbox.reads) == 6
    assert au.execute(actions=actions, **sandbox.options) == 0
    assert len(sandbox.reads) == 6


@pytest.mark.skipif(
    os.name != "posix", reason="AU flock serialization is verified on Linux"
)
def test_timer_and_recovery_lock_serialize(tmp_path):
    ctx = multiprocessing.get_context("fork")
    ready, acquired = ctx.Event(), ctx.Event()

    def worker():
        ready.set()
        with au.ingest_lock(tmp_path):
            acquired.set()

    with au.ingest_lock(tmp_path):
        process = ctx.Process(target=worker)
        process.start()
        assert ready.wait(5)
        assert not acquired.wait(0.1)
    try:
        assert acquired.wait(5)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()


def test_exact_nine_file_catalog_dry_run_recovery_and_retry(
    org, sandbox, monkeypatch, tmp_path, capsys
):
    """Three branded GO artifacts plus six Avila formats, with no live Drive."""
    import json

    project = files()
    for index, item in enumerate(project):
        item.update(
            id=f"rehearsal-go-{index}",
            name=f"AU-SYNTHETIC-00{index}_GO_rehearsal_EVID.pdf",
        )
    key = "2026-10-09-10-30_coordinacion_nodo_avila"
    avila = files(source="secretariat", key=key)
    avila.extend(
        {
            "id": f"pdf-{index}",
            "name": key + suffix,
            "source": "secretariat",
            "mimeType": "application/pdf",
        }
        for index, suffix in enumerate(
            [".transcripcion.pdf", ".resumen.pdf", ".acta.pdf"]
        )
    )
    for index, item in enumerate(avila):
        item["id"] = f"rehearsal-avila-{index}"
    selected = project + avila
    catalog = {
        item["id"]: {
            "name": item["name"],
            "source": item["source"],
            "meeting": "synthetic-go" if item in project else key,
            "class": ["transcription", "summary", "minutes"][index % 3],
            "project": item in project,
        }
        for index, item in enumerate(selected)
    }
    catalog_file = tmp_path / "recovery.json"
    catalog_file.write_text(json.dumps(catalog), encoding="utf-8")
    unselected = {
        "id": "unselected-file",
        "name": "unselected-sensitive.xlsx",
        "source": "almaponia",
        "mimeType": "application/octet-stream",
    }
    listed, locks = [], []

    def list_files(folder):
        listed.append(folder)
        source = "almaponia" if folder == au.GO_ARCHIVE else "secretariat"
        assert folder in {au.GO_ARCHIVE, "inbox-pepa"}
        return [
            {key: value for key, value in item.items() if key != "source"}
            for item in selected + [unselected]
            if item["source"] == source
        ]

    monkeypatch.setattr(
        au, "Drive", lambda *args: SimpleNamespace(list_files=list_files)
    )
    monkeypatch.setattr(
        au, "ingest_lock", lambda state: locks.append(state) or nullcontext()
    )
    execute = au.execute
    monkeypatch.setattr(
        au, "execute", lambda **kw: execute(**kw, run=sandbox.options["run"])
    )
    args = [
        "--org-yaml",
        str(sandbox.model),
        "--root",
        str(sandbox.root),
        "--docs",
        "documents.sh",
        "--service-account",
        "unused",
        "--subject",
        "unused",
        "--source",
        "almaponia",
        "--source",
        "secretariat",
        "--state",
        str(sandbox.state),
        "--catalog",
        str(catalog_file),
    ]
    before = (sandbox.root / "manifest.yaml").read_bytes()
    capsys.readouterr()
    assert au.main(args + ["--dry-run"]) == 0
    planned = [
        json.loads(line)["command"] for line in capsys.readouterr().out.splitlines()
    ]
    assert len(planned) == 9
    assert {command[command.index("--ref") + 1] for command in planned} == {
        "gdrive://" + file_id for file_id in catalog
    }
    assert not sandbox.reads and not sandbox.adds and not locks
    assert not list(sandbox.state.iterdir())
    assert (sandbox.root / "manifest.yaml").read_bytes() == before

    assert au.main(args) == 0
    assert capsys.readouterr().out.count("OK indexed ref:") == 9
    assert len(sandbox.reads) == len(sandbox.adds) == 9
    manifest = load_manifest(str(sandbox.root / "manifest.yaml"))
    refs = au.references(manifest)
    assert set(refs) == set(catalog)
    assert "unselected-file" not in refs
    for file_id, nodes in refs.items():
        assert len(nodes) == 1 and nodes[0]["category"] == "category-2"
        assert nodes[0]["owners"] == (
            ["chief_of_staff", "project_lead"]
            if catalog[file_id]["project"]
            else ["chief_of_staff"]
        )
    assert not list((sandbox.root / "blobs").iterdir())
    assert len(list(sandbox.state.iterdir())) == 9
    after = (sandbox.root / "manifest.yaml").read_bytes()
    monkeypatch.setattr(
        "phantomdocs.documents.content_hash",
        lambda *args: pytest.fail("retry must not hash document bytes"),
    )
    assert au.main(args) == 0
    repeated = capsys.readouterr().out
    assert (
        repeated.count("SKIP indexed ref:") == 9 and "OK indexed ref:" not in repeated
    )
    assert len(sandbox.reads) == len(sandbox.adds) == 9
    assert (sandbox.root / "manifest.yaml").read_bytes() == after
    assert listed == [au.GO_ARCHIVE, "inbox-pepa"] * 3
    assert len(locks) == 2
    monkeypatch.setattr("phantomdocs.cli._os_actor", lambda: "alma")
    found = sandbox.runner.invoke(
        pd,
        [
            "search",
            "rehearsal",
            "--org-yaml",
            str(sandbox.model),
            "--root",
            str(sandbox.root),
        ],
    )
    assert found.exit_code == 0 and found.output.count("doc ") == 3
