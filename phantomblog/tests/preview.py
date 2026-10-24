"""Disposable visual QA workspace. No real credentials, deployments or posts."""

import argparse
import sys
import tempfile
import threading
from pathlib import Path

from coincurve import PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures import create

from phantomblog import access, core, mcp, server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument(
        "--proposal-demo",
        action="store_true",
        help="Seed a clearly synthetic pending bilingual title proposal",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="phantomblog-fixture-") as folder:
        root = Path(folder)
        create(root, 13)
        core.build(root)
        if args.proposal_demo:
            model, revision = core.load(root)
            model["articles"][0]["en"]["title"] = (
                "Reviewed fixture <img src=x onerror=alert(1)>"
            )
            model["articles"][0]["es"]["title"] = "Prueba de revisión en español"
            mcp.call(
                root, "phantomblog_propose", {"revision": revision, "model": model}
            )
        key = PrivateKey()
        store = root / "personas"
        identity = store / "preview" / "identity.json"
        identity.parent.mkdir(parents=True)
        identity.write_text(__import__("json").dumps({"nsec": key.secret.hex()}))
        httpd = server.make_server(
            root, args.port, access_identities=[key.public_key_xonly.format().hex()]
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        print(f"Fixture dashboard: http://127.0.0.1:{httpd.server_port}/", flush=True)
        print(
            access.request_link(
                access.persona_event("preview", store, httpd.server_port),
                httpd.server_port,
            ),
            flush=True,
        )
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
