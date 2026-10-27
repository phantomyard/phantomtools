"""Opt-in installed Phantombot test: temporary registration, disabled publishing.

No Facebook credential is loaded, no Meta request is made, and no post is sent.
"""
import argparse
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from facebook_connector import core


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--persona', required=True)
    args = parser.parse_args()
    if not args.executable.is_absolute() or not args.executable.is_file() or args.executable.suffix.lower() in ('.bat','.cmd'):
        parser.error('Use an existing absolute executable path')
    if not __import__('re').fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', args.persona):
        parser.error('Use an explicit persona')
    tool_root = Path(__file__).resolve().parents[1]
    server_id = 'facebook-fixture-' + uuid.uuid4().hex[:12]
    checks = []
    directory = tempfile.mkdtemp(prefix='facebook-acceptance-')
    clean = True
    try:
        path = Path(directory) / 'facebook.json'
        core.atomic(path, {'version':1, 'pageId':None, 'apiVersion':None, 'tokenEnv':'FACEBOOK_UNUSED_SYNTHETIC_TOKEN', 'allowPublishing':False, 'articleOrigins':['https://example.com']})
        def runtime(command):
            result = subprocess.run([str(args.executable), 'mcp', *command, '--persona', args.persona], capture_output=True, encoding='utf-8', timeout=60)
            if result.returncode:
                raise RuntimeError('Runtime command failed: ' + command[0])
            return result.stdout
        def call(name, arguments=None):
            return json.loads(runtime(['call',server_id,name,'--args',json.dumps(arguments or {}, ensure_ascii=False)]))
        registered = False
        try:
            argv = [str(tool_root / 'bin/phantom-facebook'), '--config', str(path), 'mcp']
            if any(',' in v for v in [sys.executable,*argv]):
                raise RuntimeError('Registration does not support commas in executable paths')
            registered = True  # An interrupted add can have an unknown outcome.
            runtime(['add', server_id, '--stdio','--command',sys.executable,'--args',','.join(argv)])
            description = runtime(['describe',server_id])
            assert all(name in description for name in ('facebook_status','facebook_verify_page','facebook_check_page_access','facebook_prepare_article','facebook_publish_article','facebook_operation_status'))
            checks.append('six tools discovered through installed runtime')
            value = call('facebook_status')
            value = value.get('structuredContent') or json.loads(value['content'][0]['text'])
            assert value['publishingEnabled'] is False and value['accountVerified'] is False
            checks.append('setup status works without Facebook credentials')
            request = {'url':'https://example.com/a','message':'Prueba de revisión en español','contentHash':'a'*64,'idempotencyKey':'b'*64}
            value = call('facebook_publish_article',request)
            assert value.get('isError') is True
            assert not path.with_name(path.name + '.operations.json').exists()
            checks.append('publication refused before configuration with no output or external calls')
        finally:
            if registered:
                try:
                    runtime(['remove', server_id])
                except Exception:
                    clean = False
                    print('Cleanup requires operator attention; retained fixture: ' + directory + '; registration: ' + server_id, file=sys.stderr)
                    raise
    finally:
        if clean:
            shutil.rmtree(directory)
    print(json.dumps({'passed':checks,'registrationRemoved':True,'externalActions':False},ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
