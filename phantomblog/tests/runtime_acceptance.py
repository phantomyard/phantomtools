"""Opt-in real MCP acceptance. Synthetic workspace; no external publication.

Temporarily adds one uniquely named stdio server to the selected persona and
removes only that registration on exit. Never changes the default persona.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures import create

from phantomblog import connectors, core


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--persona", required=True)
    args = parser.parse_args()
    if not core.SLUG.fullmatch(args.persona):
        parser.error("Use an explicit lowercase persona name")
    os.environ["PHANTOMBLOG_PHANTOMBOT_EXECUTABLE"] = args.executable
    runner = connectors.Runner()
    workspace = Path(tempfile.mkdtemp(prefix="phantomblog-acceptance-"))
    server_id = "phantomblog-test-" + uuid.uuid4().hex[:12]
    registered = False
    clean = True
    checks = []

    def runtime(command):
        result = subprocess.run(
            [runner.executable, "mcp", *command, "--persona", args.persona],
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Runtime command failed: " + command[0])
        return result.stdout

    def call(name, arguments=None):
        result = runner.call(
            {"server": server_id, "persona": args.persona},
            {"tool": name},
            arguments or {},
        )
        if result.get("isError"):
            raise RuntimeError("MCP tool returned an error: " + name)
        return core.decode(
            next(c["text"] for c in result["content"] if c["type"] == "text")
        )

    try:
        create(workspace, 13)
        wrapper = Path(__file__).resolve().parents[1] / "bin" / "phantomblog"
        argv = [str(wrapper), "--root", str(workspace), "mcp"]
        if any("," in value for value in [sys.executable, *argv]):
            raise RuntimeError(
                "Runtime registration cannot encode paths containing commas"
            )
        runtime(
            [
                "add",
                server_id,
                "--stdio",
                "--command",
                sys.executable,
                "--args",
                ",".join(argv),
            ]
        )
        registered = True
        tools = runner.describe({"server": server_id, "persona": args.persona})
        names = {t["name"] for t in tools["tools"]}
        assert names == {
            t["name"] for t in __import__("phantomblog.mcp", fromlist=["TOOLS"]).TOOLS
        }
        checks.append("real runtime tool discovery")
        snapshot = call("phantomblog_snapshot")
        assert snapshot["revision"] == core.load(workspace)[1]
        assert snapshot["model"]["articles"][0]["es"]["title"] == "Artículo de prueba 1"
        checks.append("bilingual snapshot through MCP")
        before = core.load(workspace)[1]
        call("phantomblog_build", {"dryRun": True})
        assert not (workspace / "public").exists()
        checks.append("read-only dry run")
        call("phantomblog_build")
        manifest = (workspace / "public" / core.MANIFEST).read_bytes()
        call("phantomblog_build")
        assert (workspace / "public" / core.MANIFEST).read_bytes() == manifest
        assert (workspace / "public" / "blog-es-page-2.html").is_file()
        call("phantomblog_validate")
        checks.append("deterministic build and bilingual pagination")
        model = snapshot["model"]
        model["site"]["name"] = "Reviewed synthetic proposal"
        proposal = call("phantomblog_propose", {"revision": before, "model": model})
        assert proposal["applied"] is False and proposal["reviewRequired"] is True
        assert core.load(workspace)[1] == before
        checks.append("proposal saved without applying")
        plan = call(
            "phantomblog_prepare_publish", {"slug": "fixture-000", "language": "es"}
        )
        assert plan["liveVerified"] is False and plan["shares"] == []
        checks.append("prepare distinguishes generated URL from live verification")
    finally:
        if registered:
            try:
                runtime(["remove", server_id])
            except (OSError, subprocess.SubprocessError, RuntimeError):
                clean = False
                print(
                    "Cleanup requires operator attention; retained fixture:",
                    workspace,
                    file=sys.stderr,
                )
        if clean:
            shutil.rmtree(workspace)
        else:
            raise RuntimeError("Could not remove disposable registration " + server_id)
    print(
        json.dumps(
            {
                "passed": checks,
                "registrationRemoved": True,
                "workspaceRemoved": True,
                "externalActions": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
