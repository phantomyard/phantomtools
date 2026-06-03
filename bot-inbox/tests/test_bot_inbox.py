"""Tests for bot-inbox. Pure stdlib + pytest, no network, no shared FS."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin" / "bot-inbox"


def load_module():
    # The script has no .py extension, so give importlib an explicit loader.
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("bot_inbox", str(BIN))
    spec = importlib.util.spec_from_loader("bot_inbox", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


bi = load_module()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_INBOX_ROOT", str(tmp_path))
    monkeypatch.delenv("PHANTOMBOT_PERSONA", raising=False)
    return tmp_path


def run(argv):
    bi.main(argv)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def test_validate_ok():
    msg = {"from": "beart", "to": "domhnall", "type": "notice",
           "subject": "hi", "ts": "2026-06-03T10:00:00Z"}
    assert bi.validate_payload(msg) == []


def test_validate_missing_fields():
    problems = bi.validate_payload({"from": "beart"})
    assert any("subject" in p for p in problems)
    assert any("type" in p for p in problems)


def test_validate_bad_type():
    msg = {"from": "a", "to": "b", "type": "shout", "subject": "x",
           "ts": "t"}
    assert any("invalid type" in p for p in bi.validate_payload(msg))


def test_validate_response_needs_ref():
    msg = {"from": "a", "to": "b", "type": "response", "subject": "x",
           "ts": "t"}
    assert any("ref" in p for p in bi.validate_payload(msg))


def test_validate_bad_name():
    msg = {"from": "Bad Name!", "to": "b", "type": "notice", "subject": "x",
           "ts": "t"}
    assert any("invalid from" in p for p in bi.validate_payload(msg))


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #

def test_send_writes_atomic_no_tmp_left(root):
    run(["--from", "beart", "send", "--to", "domhnall",
         "--subject", "review PR #42", "--body", "please look"])
    box = root / "domhnall"
    files = list(box.iterdir())
    msgs = [f for f in files if f.suffix == ".json" and not f.name.startswith(".")]
    assert len(msgs) == 1
    # No leftover tmp/dotfiles.
    assert not [f for f in files if f.name.startswith(".")]
    payload = json.loads(msgs[0].read_text())
    assert payload["from"] == "beart"
    assert payload["to"] == "domhnall"
    assert payload["type"] == "request"
    assert payload["body"] == "please look"
    # Requests get an auto ref.
    assert payload["ref"]


def test_send_filename_is_fs_safe(root):
    run(["--from", "beart", "send", "--to", "x", "--subject", "s"])
    name = next((root / "x").glob("*.json")).name
    assert ":" not in name
    assert name.startswith("2") and name.endswith(".json")


# --------------------------------------------------------------------------- #
# identity (--from default via $PHANTOMBOT_PERSONA)
# --------------------------------------------------------------------------- #

def test_from_defaults_to_phantombot_persona(root, monkeypatch):
    # phantombot exposes the active persona key per-turn; --from should
    # pick it up so a bot never needs to hardcode its own name.
    monkeypatch.setenv("PHANTOMBOT_PERSONA", "beart")
    run(["send", "--to", "x", "--subject", "s"])
    payload = json.loads(next((root / "x").glob("*.json")).read_text())
    assert payload["from"] == "beart"


def test_explicit_from_overrides_persona_env(root, monkeypatch):
    monkeypatch.setenv("PHANTOMBOT_PERSONA", "beart")
    run(["--from", "domhnall", "send", "--to", "x", "--subject", "s"])
    payload = json.loads(next((root / "x").glob("*.json")).read_text())
    assert payload["from"] == "domhnall"


def test_missing_name_errors(root):
    with pytest.raises(SystemExit):
        run(["send", "--to", "x", "--subject", "s"])


def test_send_notice_has_no_auto_ref(root):
    run(["--from", "beart", "send", "--to", "x", "--type", "notice",
         "--subject", "fyi"])
    payload = json.loads(next((root / "x").glob("*.json")).read_text())
    assert "ref" not in payload


def test_send_response_keeps_given_ref(root):
    run(["--from", "beart", "send", "--to", "x", "--type", "response",
         "--subject", "re", "--ref", "abc123"])
    payload = json.loads(next((root / "x").glob("*.json")).read_text())
    assert payload["ref"] == "abc123"


def test_send_rejects_bad_recipient(root):
    with pytest.raises(SystemExit):
        run(["--from", "beart", "send", "--to", "Bad Name", "--subject", "s"])


def test_send_requires_identity(root):
    with pytest.raises(SystemExit):
        run(["send", "--to", "x", "--subject", "s"])


# --------------------------------------------------------------------------- #
# list / read / ack round-trip
# --------------------------------------------------------------------------- #

def test_round_trip(root, capsys):
    run(["--from", "beart", "send", "--to", "domhnall",
         "--subject", "ping", "--body", "hello", "--ref", "r1"])
    capsys.readouterr()

    # domhnall lists -> sees one
    run(["--from", "domhnall", "list", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    msg_id = out[0]["id"]
    assert out[0]["from"] == "beart"

    # read + ack
    run(["--from", "domhnall", "read", "--id", msg_id, "--ack"])
    capsys.readouterr()

    # now empty
    run(["--from", "domhnall", "list", "--json"])
    assert json.loads(capsys.readouterr().out) == []

    # and present in processed/
    processed = root / "domhnall" / "processed"
    assert len(list(processed.glob("*.json"))) == 1


def test_read_oldest_by_default(root, capsys):
    run(["--from", "b", "send", "--to", "x", "--subject", "first"])
    run(["--from", "b", "send", "--to", "x", "--subject", "second"])
    capsys.readouterr()
    run(["--from", "x", "read", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["message"]["subject"] == "first"


def test_read_prefix_match(root, capsys):
    run(["--from", "b", "send", "--to", "x", "--subject", "s"])
    capsys.readouterr()
    run(["--from", "x", "list", "--json"])
    full_id = json.loads(capsys.readouterr().out)[0]["id"]
    run(["--from", "x", "read", "--id", full_id[:10], "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == full_id


def test_ack_subcommand(root, capsys):
    run(["--from", "b", "send", "--to", "x", "--subject", "s"])
    capsys.readouterr()
    run(["--from", "x", "list", "--json"])
    full_id = json.loads(capsys.readouterr().out)[0]["id"]
    run(["--from", "x", "ack", full_id])
    capsys.readouterr()
    run(["--from", "x", "list", "--json"])
    assert json.loads(capsys.readouterr().out) == []


def test_dotfiles_ignored(root, capsys):
    box = root / "x"
    box.mkdir(parents=True)
    (box / ".inflight.json.tmp").write_text("{}")
    run(["--from", "x", "list", "--json"])
    assert json.loads(capsys.readouterr().out) == []


# --------------------------------------------------------------------------- #
# watch --once
# --------------------------------------------------------------------------- #

def test_watch_once_replay(root, capsys):
    run(["--from", "b", "send", "--to", "x", "--subject", "s1"])
    run(["--from", "b", "send", "--to", "x", "--subject", "s2"])
    capsys.readouterr()
    run(["--from", "x", "watch", "--once", "--replay"])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2
    subs = {json.loads(l)["message"]["subject"] for l in lines}
    assert subs == {"s1", "s2"}


def test_watch_once_ack_moves_to_processed(root, capsys):
    run(["--from", "b", "send", "--to", "x", "--subject", "s"])
    capsys.readouterr()
    run(["--from", "x", "watch", "--once", "--replay", "--ack"])
    capsys.readouterr()
    assert list((root / "x").glob("*.json")) == []
    assert len(list((root / "x" / "processed").glob("*.json"))) == 1


def test_watch_once_drains_without_replay(root, capsys):
    # --once is a one-shot "drain now": it must emit what is already pending
    # even without --replay (otherwise it's a silent no-op).
    run(["--from", "b", "send", "--to", "x", "--subject", "s1"])
    run(["--from", "b", "send", "--to", "x", "--subject", "s2"])
    capsys.readouterr()
    run(["--from", "x", "watch", "--once"])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2
    subs = {json.loads(l)["message"]["subject"] for l in lines}
    assert subs == {"s1", "s2"}


def test_watch_corrupt_json_reported_once(root, capsys):
    # A non-conforming writer can leave invalid JSON. watch must report it,
    # not crash, and (with --once) mark it seen so it isn't replayed forever.
    box = root / "x"
    box.mkdir()
    (box / "2026-06-03T00-00-00-000000Z-b-abcdef.json").write_text("{not json")
    capsys.readouterr()
    run(["--from", "x", "watch", "--once"])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 1
    assert "error" in json.loads(lines[0])


# --------------------------------------------------------------------------- #
# roster / self-registration
# --------------------------------------------------------------------------- #

def test_self_register_on_list(root):
    # Running any command under my own name must create my inbox dir, so I
    # show up in the roster even before anyone has sent to me.
    assert not (root / "alpha").exists()
    run(["--from", "alpha", "list"])
    assert (root / "alpha").is_dir()


def test_register_command(root, capsys):
    run(["--from", "beta", "register"])
    out = capsys.readouterr().out
    assert (root / "beta").is_dir()
    assert "registered" in out


def test_roster_lists_peers(root, capsys):
    run(["--from", "alpha", "register"])          # self-register
    run(["--from", "alpha", "send", "--to", "gamma", "--subject", "hi"])  # sender + recipient appear
    capsys.readouterr()
    run(["roster"])
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "gamma" in out


def test_roster_json_marks_self_and_counts(root, capsys):
    run(["--from", "alpha", "send", "--to", "gamma", "--subject", "hi"])
    capsys.readouterr()
    run(["--from", "gamma", "roster", "--json"])
    data = json.loads(capsys.readouterr().out)
    by = {d["name"]: d for d in data}
    assert by["gamma"]["self"] is True
    assert by["alpha"]["self"] is False
    assert by["gamma"]["pending"] == 1   # gamma has the message alpha sent
    assert by["alpha"]["pending"] == 0


def test_roster_empty_root(root, capsys):
    # No inbox dirs yet — roster must not crash, just report none.
    run(["roster"])
    out = capsys.readouterr().out
    assert "no bots registered" in out


def test_roster_ignores_non_name_dirs(root, capsys):
    run(["--from", "alpha", "register"])
    (root / "Not A Bot!").mkdir()      # doesn't match NAME_RE
    capsys.readouterr()
    run(["roster", "--json"])
    names = {d["name"] for d in json.loads(capsys.readouterr().out)}
    assert "alpha" in names
    assert "Not A Bot!" not in names


def test_roster_shows_send_example_for_a_real_peer(root, capsys):
    # The roster should bridge discovery → action: a copy-pasteable send
    # pointed at someone *other* than me, so the bot doesn't reconstruct flags.
    run(["--from", "me", "register"])
    run(["--from", "me", "send", "--to", "maeve", "--subject", "hi"])
    capsys.readouterr()
    run(["--from", "me", "roster"])
    out = capsys.readouterr().out
    assert "send --to maeve --subject" in out   # real peer, not "me"


# --------------------------------------------------------------------------- #
# helpful errors (a fumbled `send` should teach, not just reject)
# --------------------------------------------------------------------------- #

def test_send_missing_args_prints_example(root, capsys):
    with pytest.raises(SystemExit) as exc:
        run(["--from", "me", "send"])      # missing --to and --subject
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "required" in err               # the real reason
    assert "example:" in err               # ... plus a fix to copy
    assert 'send --to maeve --subject "hi"' in err
