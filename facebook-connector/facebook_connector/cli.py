import argparse
import json
from pathlib import Path
import sys
from . import __version__, mcp
from .core import Connector, Invalid, ExternalFailure, decode, validate

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Facebook Page connector. Credentials are injected by Phantombot; publishing is disabled by default.')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config', type=Path, default=ROOT / 'workspace' / 'facebook.json')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'check', 'verify-page', 'check-access', 'mcp'):
        sub.add_parser(name)
    for name in ('prepare', 'publish'):
        command = sub.add_parser(name)
        command.add_argument('--request', type=Path, required=True, help='JSON containing url, message, contentHash and idempotencyKey')
        if name == 'publish':
            command.add_argument('--approve', required=True, help='Fingerprint returned by prepare, after reviewing the exact plan')
    command = sub.add_parser('operation-status'); command.add_argument('--key', required=True)
    command = sub.add_parser('reconcile')
    command.add_argument('--key', required=True)
    choice = command.add_mutually_exclusive_group(required=True)
    choice.add_argument('--post-id'); choice.add_argument('--confirmed-not-sent', action='store_true')
    command.add_argument('--operator-confirmed', action='store_true', required=True, help='Only after checking Facebook history; unavailable over MCP')
    args = parser.parse_args(argv)
    try:
        connector = Connector(args.config)
        if args.command == 'mcp':
            mcp.serve(connector); return 0
        if args.command in ('status', 'check'):
            result = connector.status()
        elif args.command == 'verify-page':
            result = connector.page()
        elif args.command == 'check-access':
            result = connector.access()
        elif args.command == 'operation-status':
            result = connector.operation(args.key)
        elif args.command == 'reconcile':
            result = connector.reconcile(args.key, args.post_id, args.confirmed_not_sent)
        else:
            if args.request.stat().st_size > 50_000:
                raise Invalid('Request too large')
            request = decode(args.request.read_bytes())
            if not isinstance(request, dict) or set(request) != set(mcp.PUBLICATION):
                raise Invalid('Request needs exactly url, message, contentHash and idempotencyKey')
            values = {'url': request['url'], 'message': request['message'], 'content_hash': request['contentHash'], 'key': request['idempotencyKey']}
            result = connector.plan(**values) if args.command == 'prepare' else connector.publish(**values, approval=args.approve)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except (Invalid, ExternalFailure, OSError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc) if isinstance(exc, (Invalid, ExternalFailure)) else 'Unable to read or write connector files'}, ensure_ascii=False), file=sys.stderr)
        return 3 if isinstance(exc, ExternalFailure) else 2


if __name__ == '__main__':
    sys.exit(main())
