"""CLI entry point: build, check, dashboard and reviewable publishing plans."""
import argparse
import json
from pathlib import Path
import sys

from . import core, connectors, mcp, server


def main(argv=None):
    parser = argparse.ArgumentParser(description="PhantomBlog static publisher and editorial dashboard")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "workspace", help="Blog workspace (default: workspace beside this tool, independent of cwd)")
    parser.add_argument("--json", action="store_true", help="Machine-readable result")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "build", "check", "dry-run", "recover-build", "status", "connections", "mcp"):
        sub.add_parser(name)
    dash = sub.add_parser("dashboard")
    dash.add_argument("--port", type=int, default=8787)
    dash.add_argument("--token-ref", default="env:PHANTOMBLOG_ADMIN_TOKEN")
    dash.add_argument("--persona")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("slug")
    prepare.add_argument("--language", choices=("en", "es"), default="en")
    prepare.add_argument("--deploy")
    prepare.add_argument("--share", action="append", default=[])
    publish = sub.add_parser("publish")
    publish.add_argument("--plan", type=Path, required=True)
    publish.add_argument("--approve", required=True, help="Reviewed plan approval fingerprint")
    recovery = sub.add_parser("reconcile", help="Reconcile an uncertain external result after checking the provider")
    recovery.add_argument("operation")
    selection = recovery.add_mutually_exclusive_group(required=True)
    selection.add_argument("--confirmed-receipt")
    selection.add_argument("--confirmed-not-sent", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "init":
            result = core.initialize(root)
        elif args.command in ("build", "check", "dry-run"):
            result = core.build(root, args.command)
        elif args.command == "recover-build":
            result = core.build(root, "build", recover=True)
        elif args.command == "status":
            model, revision = core.load(root)
            result = {"revision": revision, "articles": len(model["articles"]), "connections": list(model["connections"]), "publishing": connectors.execution_state(root)}
        elif args.command == "connections":
            result = {"connections": connectors.connection_status(root)}
        elif args.command == "prepare":
            result = connectors.prepare(root, args.slug, args.language, args.deploy, args.share)
        elif args.command == "publish":
            result = connectors.execute(root, core.decode(args.plan.read_bytes()), args.approve)
        elif args.command == "reconcile":
            result = connectors.reconcile(root, args.operation, args.confirmed_receipt, args.confirmed_not_sent)
        elif args.command == "mcp":
            mcp.serve(root)
            return 0
        else:
            token = server.token_from_reference(args.token_ref, args.persona)
            httpd = server.make_server(root, token, args.port)
            print(f"PhantomBlog dashboard: http://127.0.0.1:{httpd.server_port}/\nSign in with your configured dashboard token. Ctrl+C stops the server.", file=sys.stderr)
            try:
                httpd.serve_forever()
            finally:
                httpd.server_close()
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else json.dumps(result, ensure_ascii=False))
        if args.command == "check" and (result["changed"] or result["deleted"]):
            return 1
        if args.command == "publish" and result.get("errors"):
            return 3
        return 0
    except (core.Invalid, connectors.ExternalFailure, OSError, KeyError, TypeError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, (core.Invalid, connectors.ExternalFailure)) else "Invalid or unavailable workspace; inspect source paths"
        print(json.dumps({"error": message}) if args.json else message, file=sys.stdout if args.json else sys.stderr)
        return 3 if isinstance(exc, connectors.ExternalFailure) else 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
