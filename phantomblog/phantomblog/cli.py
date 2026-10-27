"""CLI entry point: build, check, dashboard and reviewable publishing plans."""

import argparse
import json
import sys
from pathlib import Path

from . import access, connectors, core, mcp, server


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PhantomBlog static publisher and editorial dashboard"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "workspace",
        help="Blog workspace (default: workspace beside this tool, independent of cwd)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable result")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "init",
        "build",
        "check",
        "dry-run",
        "recover-build",
        "status",
        "connections",
        "mcp",
    ):
        sub.add_parser(name)
    dash = sub.add_parser("dashboard")
    dash.add_argument("--port", type=int, default=8787)
    dash.add_argument(
        "--public-origin", help="Delivery origin served by a trusted HTTPS proxy"
    )
    dash.add_argument(
        "--allow-identity",
        action="append",
        default=[],
        help="Allowed PhantomChat/Nostr public key (lowercase hex); repeat per identity",
    )
    link = sub.add_parser(
        "access-link",
        help="Request a one-time link with an already-signed NIP-98 event",
    )
    selection = link.add_mutually_exclusive_group(required=True)
    selection.add_argument("--signed-event", type=Path)
    selection.add_argument("--persona", help="Explicit runtime persona id")
    link.add_argument(
        "--persona-dir", type=Path, help="Persona store containing <id>/identity.json"
    )
    link.add_argument(
        "--public-origin", help="Must match the dashboard delivery origin"
    )
    link.add_argument("--port", type=int, default=8787)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("slug")
    prepare.add_argument("--language", choices=("en", "es"), default="en")
    prepare.add_argument("--deploy")
    prepare.add_argument("--share", action="append", default=[])
    publish = sub.add_parser("publish")
    publish.add_argument("--plan", type=Path, required=True)
    publish.add_argument(
        "--approve", required=True, help="Reviewed plan approval fingerprint"
    )
    recovery = sub.add_parser(
        "reconcile",
        help="Reconcile an uncertain external result after checking the provider",
    )
    recovery.add_argument("operation")
    selection = recovery.add_mutually_exclusive_group(required=True)
    selection.add_argument("--confirmed-receipt")
    selection.add_argument("--confirmed-not-sent", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "access-link":
            origin = access.public_origin(args.public_origin, args.port)
            if args.persona:
                event = access.persona_event(args.persona, args.persona_dir, args.port)
            else:
                if args.persona_dir:
                    raise core.Invalid("--persona-dir requires --persona")
                with args.signed_event.open("rb") as source:
                    encoded_event = source.read(8193)
                if len(encoded_event) > 8192:
                    raise core.Invalid("Signed access request is too large")
                event = core.decode(encoded_event)
            result = access.request_link(event, args.port, origin)
            print(json.dumps({"link": result}) if args.json else result)
            return 0
        if args.command == "init":
            result = core.initialize(root)
        elif args.command in ("build", "check", "dry-run"):
            result = core.build(root, args.command)
        elif args.command == "recover-build":
            result = core.build(root, "build", recover=True)
        elif args.command == "status":
            model, revision = core.load(root)
            result = {
                "revision": revision,
                "articles": len(model["articles"]),
                "connections": list(model["connections"]),
                "publishing": connectors.execution_state(root),
            }
        elif args.command == "connections":
            result = {"connections": connectors.connection_status(root)}
        elif args.command == "prepare":
            result = connectors.prepare(
                root, args.slug, args.language, args.deploy, args.share
            )
        elif args.command == "publish":
            result = connectors.execute(
                root, core.decode(args.plan.read_bytes()), args.approve
            )
        elif args.command == "reconcile":
            result = connectors.reconcile(
                root, args.operation, args.confirmed_receipt, args.confirmed_not_sent
            )
        elif args.command == "mcp":
            mcp.serve(root)
            return 0
        else:
            httpd = server.make_server(
                root,
                args.port,
                access_identities=args.allow_identity,
                public_origin=args.public_origin,
            )
            if not args.allow_identity:
                print(
                    "Dashboard access is disabled: no --allow-identity is configured. "
                    "Restart with --allow-identity <64-hex-pubkey> to enable signed links.",
                    file=sys.stderr,
                )
            print(
                f"PhantomBlog dashboard: http://127.0.0.1:{httpd.server_port}/\nRequest a one-time access link through your configured identity signer. Ctrl+C stops the server.",
                file=sys.stderr,
            )
            try:
                httpd.serve_forever()
            finally:
                httpd.server_close()
            return 0
        print(
            json.dumps(result, ensure_ascii=False, indent=2)
            if args.json
            else json.dumps(result, ensure_ascii=False)
        )
        if args.command == "check" and (result["changed"] or result["deleted"]):
            return 1
        if args.command == "publish" and result.get("errors"):
            return 3
        return 0
    except (
        core.Invalid,
        connectors.ExternalFailure,
        OSError,
        KeyError,
        TypeError,
        UnicodeError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(exc, (core.Invalid, connectors.ExternalFailure))
            else "Invalid or unavailable workspace; inspect source paths"
        )
        print(
            json.dumps({"error": message}) if args.json else message,
            file=sys.stdout if args.json else sys.stderr,
        )
        return 3 if isinstance(exc, connectors.ExternalFailure) else 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
