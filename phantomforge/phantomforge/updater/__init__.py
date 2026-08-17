"""``pf update`` — self-update from GitHub Releases, phantombot-style.

PhantomForge ships as a git checkout (the ``bin/pf`` / ``bin/phantomforge``
wrappers symlinked by ``install.sh`` resolve the repo as the single source
of truth), so unlike phantombot there is no binary to download and swap:
an update is a fast-forward of the checkout to the latest released tag,
followed by a dependency refresh when the repo carries a venv.

Flag matrix (mirrors ``phantombot update`` so the same cron conventions
apply):

  (none)           confirm before updating
  --check          print "X newer than Y" or "up to date"; exit 0/2/1
  --force          skip the confirmation (cron-friendly)

Exit codes (chosen to be cron-alertable):
  0 — updated successfully, OR already on the latest version
  1 — error (network, not a git checkout, dirty tree, fast-forward refused)
  2 — update available but not installed (only with --check)

Repo coordinates are env-overridable (``PHANTOMFORGE_UPDATE_REPO=owner/name``)
so a future repo move can be staged without a rebuild. A ``GITHUB_TOKEN``
in the environment is honored for private repos / higher rate caps; like
phantombot, a rejected token (401) or rate-limit (403) transparently
retries once without auth before failing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

DEFAULT_REPO = "salvaalba-dev/phantomtools"
UPDATE_REPO_ENV = "PHANTOMFORGE_UPDATE_REPO"
# Marker file left in the repo root between `git merge` and a successful
# `pip install -e .` refresh. A crash (SIGKILL, power loss) in that window
# would otherwise leave the venv stale with no way to notice — a later
# `pf update` sees "Already on X" and never refreshes. The marker makes the
# pending refresh detectable and self-healing (see run_update).
PENDING_MARKER = ".pf-update-pending"
GITHUB_API = "https://api.github.com"
USER_AGENT = "phantomforge-update"
REQUEST_TIMEOUT = 30.0
GIT_TIMEOUT = 120
PIP_TIMEOUT = 300
NOTES_TRUNCATE = 800

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AVAILABLE = 2


@dataclass
class LatestRelease:
    """The release-target tuple for an update."""

    version: str
    tag: str
    published_at: str | None
    body: str


@dataclass
class FindResult:
    ok: bool
    release: LatestRelease | None
    error: str | None


HttpGetter = Callable[[str, str | None], tuple[int, object | None, str | None]]


# ---------------------------------------------------------------------------
# Repo / version discovery
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """Absolute path of the PhantomForge checkout this code lives in.

    ``updater/__init__.py`` sits three levels below the checkout root
    (``<root>/phantomforge/updater/``), so the root is the pyproject.toml
    ancestor — same convention ``cli._version`` relies on.
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "bin" / "pf"
        ).is_file():
            return candidate
    # Fallback (e.g. tests): the package parent's parent is the checkout.
    return here.parent.parent


def read_local_version(root: Path) -> str | None:
    """Read the ``project.version`` from the checkout's pyproject.toml."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        return None
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) else None


def is_git_checkout(root: Path) -> bool:
    """True when ``root`` (or an ancestor) is inside a git work tree."""
    try:
        # fixed arg list, no shell — safe by construction
        proc = subprocess.run(  # nosec B603 B607
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def remote_origin_repo(root: Path) -> str | None:
    """Normalize ``remote.origin.url`` to ``owner/name`` (GitHub only)."""
    try:
        # fixed arg list, no shell — safe by construction
        proc = subprocess.run(  # nosec B603 B607
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = proc.stdout.strip() if proc.returncode == 0 else ""
    # git@github.com:owner/repo.git | https://github.com/owner/repo[.git]
    # https://user:token@github.com/owner/repo.git (credentials embedded)
    match = re.match(
        r"(?:git@|https?://(?:[^/@]+@)?)github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$",
        url,
    )
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def _numeric_parts(version: str) -> list[int]:
    """Leading integers of each dot-separated component (PEP 440-ish)."""
    parts: list[int] = []
    for component in version.split("."):
        match = re.match(r"(\d+)", component)
        parts.append(int(match.group(1)) if match else 0)
    return parts


def version_cmp(a: str, b: str) -> int:
    """Compare dotted versions. Returns -1/0/1. ``0.4.10 > 0.4.9``."""
    pa, pb = _numeric_parts(a), _numeric_parts(b)
    for x, y in zip(pa, pb):
        if x != y:
            return -1 if x < y else 1
    if len(pa) < len(pb):
        return -1
    if len(pa) > len(pb):
        return 1
    return 0


# ---------------------------------------------------------------------------
# GitHub Releases discovery
# ---------------------------------------------------------------------------


def _build_headers(token: str | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "accept": "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": USER_AGENT,
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _http_get_json(
    url: str, token: str | None, timeout: float = REQUEST_TIMEOUT
) -> tuple[int, object | None, str | None]:
    """GET ``url`` returning ``(status, json_body, error)``."""
    request = urllib.request.Request(url, headers=_build_headers(token))
    try:
        # fixed https://api.github.com URL, no user-controlled scheme
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return response.status, json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return exc.code, None, None
    except Exception as exc:  # noqa: BLE001 — network/parse errors all map to a message
        return 0, None, str(exc)


def find_latest_release(
    repo: str, token: str | None, http_get: HttpGetter | None = None
) -> FindResult:
    """Hit GitHub's ``/releases/latest`` and return the version + notes.

    Mirrors phantombot's githubReleases.ts: auth header only when a
    GITHUB_TOKEN is present; a 401/403 with a token retries once without
    it (an org-scoped token is often rejected for public repos) before
    failing with a clear message.
    """
    get = http_get or _http_get_json
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    had_token = bool(token)

    status, body, error = get(url, token)
    if error is not None:
        return FindResult(False, None, f"network error reaching {url}: {error}")

    retried_unauth = False
    if had_token and status in (401, 403):
        status, body, error = get(url, None)
        retried_unauth = True

    if status == 403:
        return FindResult(
            False,
            None,
            "GitHub API rate-limited (60/h) even after retrying without "
            "GITHUB_TOKEN. Try again later."
            if retried_unauth
            else "GitHub API rate-limited (60/h unauth). Set GITHUB_TOKEN in env to lift the cap.",
        )
    if status == 401:
        return FindResult(
            False,
            None,
            f"GitHub API HTTP 401 from {url} (token rejected; is GITHUB_TOKEN "
            "scoped to a different org or repo?)",
        )
    if status == 404:
        return FindResult(
            False,
            None,
            f"no releases found at {repo} — is the repo name right, and is it "
            "accessible (a private repo needs GITHUB_TOKEN)?",
        )
    if status != 200:
        return FindResult(False, None, f"GitHub API HTTP {status} from {url}")

    if not isinstance(body, dict):
        return FindResult(False, None, "GitHub API returned non-JSON")
    tag_name = body.get("tag_name")
    if not isinstance(tag_name, str):
        return FindResult(False, None, "GitHub API response missing tag_name")
    version = tag_name.removeprefix("v")
    published_at = body.get("published_at")
    release_body = body.get("body")
    return FindResult(
        True,
        LatestRelease(
            version=version,
            tag=tag_name,
            published_at=published_at if isinstance(published_at, str) else None,
            body=release_body if isinstance(release_body, str) else "",
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Apply: git fast-forward + venv refresh
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> tuple[int, str, str]:
    # fixed arg list, no shell — safe by construction
    proc = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _dirty_paths(root: Path) -> list[str]:
    code, stdout, _ = _git(root, "status", "--porcelain")
    if code != 0:
        return []
    # Untracked files (??) don't block a fast-forward; everything else does.
    return [
        line[3:]
        for line in stdout.splitlines()
        if line.strip() and not line.startswith("??")
    ]


def apply_update(root: Path, tag: str) -> tuple[bool, str]:
    """Fast-forward the checkout to ``tag``; refuse on dirty/diverged state.

    Returns ``(ok, message)``. The update is refused (not destructive) when
    the working tree has tracked modifications, so a user's local edits are
    never clobbered.
    """
    dirty = _dirty_paths(root)
    if dirty:
        return False, (
            "working tree has uncommitted changes; commit or stash them first:\n  "
            + "\n  ".join(dirty[:5])
        )

    code, _, stderr = _git(root, "fetch", "origin", "--tags")
    if code != 0:
        return False, f"git fetch failed: {stderr}"

    code, _, stderr = _git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}")
    if code != 0:
        return False, f"tag {tag} not found after fetch: {stderr}"

    # Write the pending marker BEFORE the merge: if we crash between the
    # merge and the venv refresh, the next `pf update` repairs it (G).
    _write_pending_marker(root, tag)
    code, _, stderr = _git(root, "merge", "--ff-only", "--", tag)
    if code != 0:
        # No HEAD change happened — drop the marker so a later `pf update`
        # does not run a pointless repair refresh.
        _clear_pending_marker(root)
        return False, (
            f"could not fast-forward to {tag}: {stderr} — the branch has "
            "diverged (local commits) or the merge would not be a fast-forward."
        )
    return True, f"updated to {tag}"


def pending_update_tag(root: Path) -> str | None:
    """Return the tag recorded in the pending-refresh marker, if any."""
    marker = root / PENDING_MARKER
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _write_pending_marker(root: Path, tag: str) -> None:
    try:
        (root / PENDING_MARKER).write_text(tag + "\n", encoding="utf-8")
    except OSError:
        # Best-effort: a read-only repo root must not block the update
        # itself; the marker is only a repair hint.
        pass


def _clear_pending_marker(root: Path) -> None:
    try:
        (root / PENDING_MARKER).unlink(missing_ok=True)
    except OSError:
        pass


def _venv_python(root: Path) -> Path | None:
    """Repo venv interpreter path, or None when the repo has no venv.

    Layout differs by OS: POSIX uses ``.venv/bin/python``, Windows uses
    ``.venv/Scripts/python.exe``.
    """
    if os.name == "nt":
        candidate = root / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = root / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else None


def refresh_venv(root: Path) -> tuple[bool, str]:
    """``pip install -e .`` when the repo has a venv; no-op otherwise.

    Non-fatal: a failed dependency refresh is reported as a warning, the
    update itself already succeeded.
    """
    venv_python = _venv_python(root)
    if venv_python is None:
        return True, ""
    try:
        # fixed arg list, no shell — safe by construction
        proc = subprocess.run(  # nosec B603 B607
            [str(venv_python), "-m", "pip", "install", "-e", "."],
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT,
            check=False,
            # The checkout root, not the caller's cwd: `pf update` run from
            # /tmp or any other directory must install THIS repo (C2).
            cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        return False, "pip install -e . timed out"
    if proc.returncode != 0:
        tail = proc.stderr.strip()[-500:]
        return False, f"pip install -e . failed:\n{tail}"
    return True, "venv dependencies refreshed"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…[release notes truncated]"


def run_update(
    *,
    check: bool = False,
    force: bool = False,
    repo_override: str | None = None,
    root: Path | None = None,
    token: str | None = None,
    http_get: HttpGetter | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run the update flow; returns the process exit code (0/1/2)."""
    out = out or sys.stdout
    err = err or sys.stderr
    root = root or repo_root()
    token = os.environ.get("GITHUB_TOKEN") if token is None else token

    if not is_git_checkout(root):
        err.write(
            f"update failed: {root} is not a git checkout. `pf update` needs "
            "the repo on disk (install.sh installs from a checkout).\n"
        )
        return EXIT_ERROR

    origin_repo = remote_origin_repo(root)
    repo = repo_override or os.environ.get(UPDATE_REPO_ENV) or origin_repo
    if not repo:
        err.write(
            "update failed: no GitHub remote found. Add remote.origin or set "
            # false positive, not SQL — plain env-var message
            f"{UPDATE_REPO_ENV}=owner/name.\n"  # nosec B608
        )
        return EXIT_ERROR
    # Supply-chain guard (adversarial review, v0.5.5): the checkout's git
    # origin and the repository consulted on GitHub must be the same. Without
    # this, PHANTOMFORGE_UPDATE_REPO pointing at repo B while the checkout's
    # origin is repo A would announce B's release but install the same-named
    # tag from A — different code than advertised.
    if origin_repo is None:
        err.write(
            "update failed: no git remote.origin in the checkout — refusing "
            "to proceed without a known origin (set remote.origin).\n"
        )
        return EXIT_ERROR
    if repo != origin_repo:
        err.write(
            f"update failed: update repository {repo} does not match git "
            f"origin {origin_repo} — refusing to install a tag from a "
            "different repository than the one consulted.\n"
        )
        return EXIT_ERROR

    local_version = read_local_version(root)
    if local_version is None:
        err.write("update failed: cannot read version from pyproject.toml\n")
        return EXIT_ERROR

    result = find_latest_release(repo, token, http_get=http_get)
    if not result.ok or result.release is None:
        err.write(f"update check failed: {result.error}\n")
        return EXIT_ERROR
    release = result.release

    pending = pending_update_tag(root)
    if pending:
        if check:
            out.write(
                f"warning: a previous update ({pending}) left the venv "
                "refresh pending — run `pf update` to repair it.\n"
            )
        else:
            out.write(f"repairing pending update ({pending}): refreshing venv...\n")
            venv_ok, venv_message = refresh_venv(root)
            if venv_message:
                out.write(venv_message + "\n")
            if venv_ok:
                _clear_pending_marker(root)
                out.write("pending update repaired.\n")
            else:
                err.write(
                    "warning: pending venv refresh failed — run `pip install "
                    "-e .` manually or retry `pf update`.\n"
                )

    if version_cmp(release.version, local_version) <= 0:
        out.write(f"Already on {release.tag} (local {local_version}).\n")
        return EXIT_OK

    if check:
        out.write(f"Update available: {local_version} → {release.version}\n")
        out.write(f"  release: {release.tag}\n")
        if release.published_at:
            out.write(f"  published: {release.published_at}\n")
        return EXIT_AVAILABLE

    if not force:
        out.write(
            f"Update available: {local_version} → {release.version} ({release.tag})\n"
        )
        if release.body:
            out.write("--- release notes ---\n")
            out.write(_truncate(release.body, NOTES_TRUNCATE) + "\n")
        try:
            answer = input("Install this update? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            out.write("update cancelled.\n")
            return EXIT_OK

    ok, message = apply_update(root, release.tag)
    if not ok:
        err.write(f"install failed: {message}\n")
        return EXIT_ERROR
    out.write(message + "\n")

    venv_ok, venv_message = refresh_venv(root)
    if venv_message:
        out.write(venv_message + "\n")
    if not venv_ok:
        err.write(
            "warning: dependency refresh failed — run `pip install -e .` "
            "manually if commands start failing.\n"
        )
    else:
        _clear_pending_marker(root)

    new_local = read_local_version(root)
    out.write(
        f"PhantomForge updated: {local_version} → {new_local or release.version}\n"
    )
    return EXIT_OK
