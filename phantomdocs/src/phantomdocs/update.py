"""Self-update check (spec §16 Roadmap).

``pd update`` compares the installed version against the latest GitHub Release
of the configured repository (env ``PHANTOMDOCS_UPDATE_REPO``). Exit codes
follow the phantombot convention: 0 = up to date, 1 = update available,
2 = error. The install path lands once the tool is published as a release.
"""

from __future__ import annotations

import json
import urllib.request


def is_newer(latest: str, current: str) -> bool:
    """Compare two dotted version strings (``1.2.3``); True if latest > current."""

    def parse(version: str) -> list[int]:
        parts: list[int] = []
        for chunk in version.split("."):
            digits = "".join(c for c in chunk if c.isdigit())
            parts.append(int(digits) if digits else 0)
        return parts

    return parse(latest) > parse(current)


def latest_release(repo: str, token: str | None = None) -> str | None:
    """Latest release tag for a GitHub repo, or None on any failure."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        # Fixed https endpoint (api.github.com); the repo id is operator config.
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("tag_name")
    # Best-effort network probe: any failure (HTTP/TLS/timeout/JSON) means
    # "no release found", so a broad catch is intentional here.
    except Exception:  # noqa: BLE001
        return None
