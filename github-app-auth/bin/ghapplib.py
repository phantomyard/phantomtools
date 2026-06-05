#!/usr/bin/env python3
import subprocess
import sys
import os
import json
import base64
import re
import shutil
import difflib
import urllib.request
import urllib.error
from datetime import datetime, timezone

def get_real_git(script_path=None):
    """Locate the real git binary, bypassing the wrapper.
    
    Order: $REAL_GIT → $GITHUB_APP_REAL_GIT → /usr/bin/git → PATH search.
    If script_path is provided, that directory is filtered out of PATH.
    """
    for env_var in ("REAL_GIT", "GITHUB_APP_REAL_GIT"):
        candidate = os.environ.get(env_var, "")
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    
    if os.path.isfile("/usr/bin/git") and os.access("/usr/bin/git", os.X_OK):
        return "/usr/bin/git"
    
    saved_path = os.environ.get("PATH", "")
    if script_path:
        self_dir = os.path.dirname(os.path.realpath(script_path))
        sanitized = os.pathsep.join(
            d for d in saved_path.split(os.pathsep)
            if d and os.path.realpath(d) != self_dir
        )
    else:
        sanitized = saved_path
        
    found = shutil.which("git", path=sanitized)
    if found:
        return found
    
    return None

def run_git(git_bin, args, text=True, **kwargs):
    cmd = [git_bin] + args
    result = subprocess.run(cmd, capture_output=True, text=text, check=True, **kwargs)
    return result

def get_token():
    env_file = os.path.expanduser("~/.github_env")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and os.path.exists(env_file):
        # The token file holds a live credential. If it's group/world-readable
        # something has loosened the permissions refresh-github-env.sh sets
        # (umask 077) — refuse to read it rather than trust a leaked token.
        mode = os.stat(env_file).st_mode
        if mode & 0o077:
            print(f"Error: refusing to read {env_file}: permissions are too open "
                  f"({oct(mode & 0o777)}). Run: chmod 600 {env_file}", file=sys.stderr)
            return ""
        with open(env_file) as f:
            for line in f:
                if line.startswith('export GITHUB_TOKEN='):
                    m = re.search(r'export GITHUB_TOKEN="([^"]+)"', line)
                    if m:
                        token = m.group(1)
                    break
    return token

def _bin_dir():
    """Directory holding this module and its sibling shell scripts."""
    return os.path.dirname(os.path.realpath(__file__))

def get_token_expiry():
    """Return the token's expiry as a timezone-aware datetime, or None if
    unknown. Reads GITHUB_TOKEN_EXPIRES_AT from the environment or, failing
    that, from ~/.github_env. 'Unknown' (None) is deliberate: an old env file
    written before expiry was persisted has no field, and callers must treat
    that as 'can't tell' rather than 'expired'."""
    raw = os.environ.get("GITHUB_TOKEN_EXPIRES_AT", "")
    if not raw:
        env_file = os.path.expanduser("~/.github_env")
        if os.path.exists(env_file):
            try:
                with open(env_file) as f:
                    for line in f:
                        if line.startswith("export GITHUB_TOKEN_EXPIRES_AT="):
                            m = re.search(
                                r'export GITHUB_TOKEN_EXPIRES_AT="([^"]*)"', line)
                            if m:
                                raw = m.group(1)
                            break
            except OSError:
                return None
    if not raw:
        return None
    # GitHub emits RFC 3339 like "2026-06-02T13:45:00Z". Python <3.11 chokes on
    # the trailing Z, so normalise it to an explicit UTC offset.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

def token_is_expired(skew_seconds=60):
    """True only if we KNOW the token is past (or within skew_seconds of) its
    expiry. Unknown expiry returns False — we don't refresh on a hunch; the
    401-retry path is the safety net for a token that's stale without us
    knowing."""
    exp = get_token_expiry()
    if exp is None:
        return False
    now = datetime.now(timezone.utc)
    return (exp - now).total_seconds() <= skew_seconds

def refresh_token():
    """Regenerate the installation token by running refresh-github-env.sh,
    which rewrites ~/.github_env (mode 0600). Returns the fresh token, or ""
    on failure (diagnostics go to stderr). This is the single sanctioned
    self-heal hook — call it at most once per failure, never in a loop."""
    script = os.path.join(_bin_dir(), "refresh-github-env.sh")
    if not (os.path.isfile(script) and os.access(script, os.X_OK)):
        print(f"Error: cannot refresh token — {script} missing or not executable",
              file=sys.stderr)
        return ""
    print("Refreshing GitHub App token...", file=sys.stderr)
    try:
        subprocess.run(["bash", script], check=True,
                       stdout=subprocess.DEVNULL, stderr=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"Error: token refresh failed (exit {e.returncode})", file=sys.stderr)
        return ""
    # The just-rewritten file is the source of truth; drop any stale process
    # env so get_token() reads the fresh value off disk.
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GITHUB_TOKEN_EXPIRES_AT", None)
    return get_token()

def ensure_token():
    """Return a usable token, refreshing first if it's missing or known-expired.
    Use this from entry-point scripts instead of get_token() so a dead or
    absent token self-heals instead of dead-ending in a 401 loop."""
    token = get_token()
    if not token or token_is_expired():
        refreshed = refresh_token()
        if refreshed:
            token = refreshed
    return token

def parse_owner_repo(repo_url):
    """Extract (owner, repo) from an HTTPS or SSH GitHub remote URL.
    Returns None if the URL isn't a github.com remote."""
    m = re.search(r'github\.com[/:]([^/]+)/(.+?)(?:\.git)?$', repo_url)
    if not m:
        return None
    return m.group(1), m.group(2)

class GitHubAppClient:
    def __init__(self, owner, repo, token, git_bin):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.git_bin = git_bin
        self.api_base = f"https://api.github.com/repos/{owner}/{repo}"
        self.remote_object_cache = set()

    def api_request(self, method, endpoint, data=None, _allow_refresh=True):
        if endpoint.startswith("http"):
            url = endpoint
        elif endpoint:
            url = f"{self.api_base}/{endpoint}"
        else:
            # Empty endpoint → the repo resource itself (no trailing slash).
            url = self.api_base
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
            body = json.dumps(data).encode("utf-8")
            req.add_header("Content-Length", str(len(body)))
            req.data = body

        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # A 401 means the token died (expired / rotated) mid-flight. Refresh
            # exactly once and retry — _allow_refresh=False on the retry call
            # guarantees we never loop. Only 401 triggers this: a 403 is a
            # permission problem and a 404 is a missing resource; refreshing the
            # token would not help either, so we leave them alone (the wrapper
            # has been "too eager" before — keep the self-heal narrow).
            if e.code == 401 and _allow_refresh:
                new_token = refresh_token()
                if new_token and new_token != self.token:
                    self.token = new_token
                    return self.api_request(method, endpoint, data,
                                            _allow_refresh=False)
            # Re-read body for error reporting
            try:
                body = e.read().decode("utf-8")
            except:
                body = "(could not read error body)"
            print(f"API error: {e.code} {e.reason}", file=sys.stderr)
            print(body, file=sys.stderr)
            raise

    def get_default_branch(self):
        info = self.api_request("GET", "")
        return info.get("default_branch", "main")

    def create_pull_request(self, head, base, title, body=None, draft=False):
        """Open a pull request via the REST API using the App identity.

        head/base are branch names (or owner:branch for cross-fork heads).
        A 403/404 here almost always means the App lacks the `Pull requests:
        write` permission — git push works with only `Contents: write`, so a
        working push but failing PR is the classic symptom. We surface that
        explicitly so the next bot doesn't go installing gh out of confusion.
        """
        payload = {"head": head, "base": base, "title": title, "draft": draft}
        if body is not None:
            payload["body"] = body
        try:
            return self.api_request("POST", "pulls", payload)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                print(
                    "Hint: PR creation got "
                    f"{e.code} — does the GitHub App have the 'Pull requests: "
                    "Read & write' permission, and has the installation "
                    "accepted it? (git push needs only 'Contents: write', so a "
                    "working push with a failing PR points straight at this.)",
                    file=sys.stderr,
                )
            raise

    def create_issue(self, title, body, labels=None):
        """Open an issue via the REST API using the App identity.

        A 403/404 here usually means the App lacks the `Issues: write`
        permission (or the installation hasn't accepted it) — same family of
        symptom as the PR helper above, so we surface it the same way instead
        of letting the bot guess.
        """
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        try:
            return self.api_request("POST", "issues", payload)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                print(
                    f"Hint: issue creation got {e.code} — does the GitHub App "
                    "have the 'Issues: Read & write' permission, and has the "
                    "installation accepted it?",
                    file=sys.stderr,
                )
            raise

    def find_open_issue_by_marker(self, marker):
        """Return the first open issue whose body contains `marker`, else None.

        Used to de-dup automated issues: each report carries a stable HTML
        marker comment, so we never open a second issue for a drift that is
        already tracked. The /issues endpoint returns pull requests too, so we
        skip anything carrying a `pull_request` key.
        """
        issues = self.api_request("GET", "issues?state=open&per_page=100")
        for it in issues:
            if "pull_request" in it:
                continue
            if marker in (it.get("body") or ""):
                return it
        return None

    def _upload_blob(self, sha):
        """Upload a single blob by its local SHA, return the remote blob SHA.
        GitHub blob SHAs are content-addressed, so this is idempotent."""
        content = run_git(self.git_bin, ["cat-file", "-p", sha], text=False).stdout
        encoding = "utf-8"
        try:
            text_content = content.decode("utf-8")
        except UnicodeDecodeError:
            text_content = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        blob_resp = self.api_request("POST", "git/blobs",
                                     {"content": text_content, "encoding": encoding})
        return blob_resp["sha"]

    def upload_tree(self, tree_sha, base_tree_sha=None):
        # 1. Fast path: check if this exact tree SHA is already on GitHub
        if tree_sha in self.remote_object_cache:
            return tree_sha
        try:
            self.api_request("GET", f"git/trees/{tree_sha}?recursive=0")
            self.remote_object_cache.add(tree_sha)
            return tree_sha
        except urllib.error.HTTPError as e:
            # 404: tree object simply absent.
            # 422: GitHub returns this for unknown / unresolvable SHAs on the
            # git data API (e.g. SHA exists nowhere on the remote yet).
            # Either way, fall through and rebuild the tree via the API.
            if e.code not in (404, 422):
                raise

        # 2. Incremental path: if we know a base tree that is already on the
        # remote, only upload the blobs that actually changed relative to it.
        # This turns an O(whole repo) push into O(diff) — critical for real
        # repos where a one-file change otherwise re-uploads hundreds of blobs.
        if base_tree_sha and base_tree_sha != tree_sha:
            try:
                return self._upload_tree_incremental(tree_sha, base_tree_sha)
            except (urllib.error.HTTPError, subprocess.CalledProcessError, RuntimeError):
                # Base tree not on remote, diff failed, or result mismatched —
                # fall back to the full rebuild below.
                pass

        # 3. Full rebuild: upload every blob in the tree.
        return self._upload_tree_full(tree_sha)

    def _upload_tree_incremental(self, tree_sha, base_tree_sha):
        # Base must already exist on the remote; raises (caught by caller) if not.
        self.api_request("GET", f"git/trees/{base_tree_sha}?recursive=0")

        # -z gives NUL-separated records, immune to path-quoting surprises.
        raw = run_git(self.git_bin,
                      ["diff-tree", "-r", "-z", "--no-commit-id",
                       base_tree_sha, tree_sha]).stdout
        tokens = raw.split("\0")
        entries = []
        i = 0
        while i < len(tokens):
            meta = tokens[i]
            if not meta.startswith(":"):
                i += 1
                continue
            path = tokens[i + 1]
            i += 2
            # meta: ":<old_mode> <new_mode> <old_sha> <new_sha> <status>"
            old_mode, new_mode, _old_sha, new_sha, status = meta[1:].split()
            if status == "D":
                # Deletion: sha=None tells GitHub to drop the path from base_tree.
                entries.append({"path": path, "mode": old_mode,
                                "type": "blob", "sha": None})
            elif new_mode == "160000":
                # Submodule pointer — reference the commit SHA directly.
                entries.append({"path": path, "mode": "160000",
                                "type": "commit", "sha": new_sha})
            else:
                entries.append({"path": path, "mode": new_mode,
                                "type": "blob", "sha": self._upload_blob(new_sha)})

        if not entries:
            # No diff means the trees are identical.
            self.remote_object_cache.add(tree_sha)
            return tree_sha

        resp = self.api_request("POST", "git/trees",
                                {"base_tree": base_tree_sha, "tree": entries})
        if resp["sha"] != tree_sha:
            # The overlay didn't reconstruct the exact target tree — bail so the
            # caller falls back to a full, guaranteed-correct rebuild.
            raise RuntimeError(
                f"incremental tree mismatch: got {resp['sha']}, want {tree_sha}")
        self.remote_object_cache.add(tree_sha)
        return tree_sha

    def _upload_tree_full(self, tree_sha):
        entries = []
        ls_tree = run_git(self.git_bin, ["ls-tree", "-r", tree_sha]).stdout.strip()
        if not ls_tree:
            resp = self.api_request("POST", "git/trees", {"tree": []})
            return resp["sha"]

        for line in ls_tree.split("\n"):
            parts = line.split("\t", 1)
            if len(parts) < 2:
                continue
            meta = parts[0].split()
            mode, obj_type, sha, path = meta[0], meta[1], meta[2], parts[1]

            if obj_type == "blob":
                entries.append({
                    "path": path,
                    "mode": mode,
                    "type": "blob",
                    "sha": self._upload_blob(sha)
                })
            elif obj_type == "commit":
                entries.append({
                    "path": path,
                    "mode": "160000",
                    "type": "commit",
                    "sha": sha
                })

        tree_data = self.api_request("POST", "git/trees", {"tree": entries})
        self.remote_object_cache.add(tree_data["sha"])
        return tree_data["sha"]

# =============================================================================
# Wrapper-drift detection
# -----------------------------------------------------------------------------
# The installed copies in ~/.local/bin are meant to be symlinks into this repo.
# When someone edits the installed copy in place (turning a symlink into a
# regular, diverged file) the change is invisible to version control and lost
# on the next install. report-drift surfaces that: it walks every tool in the
# repo, compares each installed wrapper to its repo source, and (optionally)
# opens a de-duplicated issue so the drift gets folded back in.
# =============================================================================

DRIFT_MARKER_FMT = "<!-- report-drift:{key} -->"


def drift_marker(tool, script):
    """Stable HTML-comment marker for one wrapper, used to de-dup issues."""
    return DRIFT_MARKER_FMT.format(key=f"{tool}/{script}")


def unified_drift(installed_text, repo_text, installed_label, repo_label):
    """Return a unified diff (installed → repo) or "" if identical.

    Pure function — takes the two file contents as strings, touches no
    filesystem — so the drift logic is unit testable without a real install.
    """
    if installed_text == repo_text:
        return ""
    diff = difflib.unified_diff(
        installed_text.splitlines(keepends=True),
        repo_text.splitlines(keepends=True),
        fromfile=installed_label,
        tofile=repo_label,
    )
    return "".join(diff)


def discover_tool_wrappers(repo_root):
    """Yield (tool, script, abs_repo_path) for every installable wrapper.

    An "installable" subproject is any immediate subdirectory of repo_root that
    has both a bin/ directory and an install.sh — the same shape both current
    tools share — so new tools are picked up automatically with no edit here.
    Only regular files inside bin/ count (a tool's bin/ may hold a library or a
    __pycache__ dir; those aren't wrappers, but comparing them is harmless if
    install.sh symlinked them too).
    """
    out = []
    try:
        tools = sorted(os.listdir(repo_root))
    except OSError:
        return out
    for tool in tools:
        tool_dir = os.path.join(repo_root, tool)
        bin_dir = os.path.join(tool_dir, "bin")
        if not os.path.isdir(bin_dir):
            continue
        if not os.path.isfile(os.path.join(tool_dir, "install.sh")):
            continue
        for name in sorted(os.listdir(bin_dir)):
            if name.startswith(".") or name == "__pycache__":
                continue
            path = os.path.join(bin_dir, name)
            if os.path.isfile(path):
                out.append((tool, name, path))
    return out


def classify_wrapper(repo_path, installed_path):
    """Compare one installed wrapper against its repo source.

    Returns (status, diff):
      missing — nothing is installed at installed_path (tool not installed).
      ok      — installed is a symlink resolving to repo_path, or a regular
                file byte-identical to it.
      foreign — installed is a symlink pointing somewhere else (not our repo);
                we never touch or report on someone else's tool.
      drift   — installed is a regular file whose contents differ from repo.
    `diff` is non-empty only for status == "drift".
    """
    if not os.path.lexists(installed_path):
        return "missing", ""
    if os.path.islink(installed_path):
        target = os.path.realpath(installed_path)
        if target == os.path.realpath(repo_path):
            return "ok", ""
        # A dangling symlink (target gone) or one pointing elsewhere is not a
        # diverged copy of our file — leave it alone.
        return "foreign", ""
    # Regular file with our name: compare contents.
    try:
        with open(installed_path, "r", errors="replace") as f:
            installed_text = f.read()
        with open(repo_path, "r", errors="replace") as f:
            repo_text = f.read()
    except OSError:
        return "foreign", ""
    if installed_text == repo_text:
        return "ok", ""
    return "drift", ""


def scan_drift(repo_root, local_bin):
    """Walk every repo wrapper and classify its installed copy.

    Returns a list of records: {tool, script, repo_path, installed_path,
    status, diff}. Pure orchestration over discover_tool_wrappers /
    classify_wrapper — kept here (not in the CLI) so behaviour is one place.
    """
    records = []
    for tool, script, repo_path in discover_tool_wrappers(repo_root):
        installed_path = os.path.join(local_bin, script)
        status, _ = classify_wrapper(repo_path, installed_path)
        diff = ""
        if status == "drift":
            with open(installed_path, "r", errors="replace") as f:
                installed_text = f.read()
            with open(repo_path, "r", errors="replace") as f:
                repo_text = f.read()
            diff = unified_drift(
                installed_text, repo_text,
                f"installed: {installed_path}",
                f"repo: {tool}/bin/{script}",
            )
        records.append({
            "tool": tool,
            "script": script,
            "repo_path": repo_path,
            "installed_path": installed_path,
            "status": status,
            "diff": diff,
        })
    return records


def determine_push_strategy(local_sha, remote_sha, remote_known_locally, is_ancestor, branch_exists_remote, force=False):
    """Determine which commits to push and how.
    Returns: (commits_to_push, parent_for_first, needs_force, preserve_parents)
    """
    if force:
        return (None, "", True, True)
    
    if is_ancestor:
        # Normal fast-forward
        return (None, remote_sha, False, True) # None means needs rev-list
        
    if branch_exists_remote and not remote_known_locally:
        # Recreated-SHA scenario: a previous App-push rebuilt our commits with
        # new SHAs on the remote, so remote_sha is unknown locally and there is
        # NO sound local rev-list for "what's new" (no local commit matches a
        # remote SHA). Returning None would make the caller run
        # `rev-list remote_sha..local_sha` against a SHA it doesn't have (crash)
        # or `--not --remotes` which re-pushes the entire history as duplicates.
        # Instead, push the local tip onto the recreated remote tip: for the
        # common single-new-commit case this is exactly right; for multiple new
        # commits it squashes them, which beats crashing or duplicating history.
        return ([local_sha], remote_sha, False, False)
        
    if branch_exists_remote:
        # Truly divergent
        return (None, "", True, True)

    # New branch: caller computes the rev-list of commits not yet on the remote
    # (e.g. `local_sha --not --remotes=<remote>`) and preserves the original
    # parent chain. Returning ([local_sha], ...) here would push only the tip
    # commit as an orphan, severing it from main.
    return (None, "", False, True)

def force_push_to_default_blocked(branch, default_branch, force, needs_force,
                                  override=False):
    """Whether this push must be refused to protect the repo's default branch.

    A history rewrite — an explicit -f, or a divergent push that needs_force —
    onto the default branch silently drops shared commits (the rebase+force
    that wiped a collaborator's work). Block it unless the caller set an
    explicit override. Non-force fast-forwards and the recreated-SHA re-sync
    (needs_force=False) are never blocked, keeping the guard narrow. Pure
    function so it stays unit-testable alongside determine_push_strategy.
    """
    if override:
        return False
    if not (force or needs_force):
        return False
    return branch == default_branch

def ensure_user_systemd_env(env=None, runtime_dir=None, uid=None, dir_exists=None):
    """Make the user-level systemd bus reachable for `systemctl --user`.

    In a non-login session (a bot, a `sudo su -` shell) PAM does not set
    XDG_RUNTIME_DIR, so `systemctl --user` can't find the D-Bus socket and
    every timer check fails — the manual `export XDG_RUNTIME_DIR=/run/user/$(id -u)`
    dance. Mirror what phantombot does: if XDG_RUNTIME_DIR is already set, leave
    it alone; otherwise derive /run/user/<uid> and, when that dir exists (linger
    is on), set XDG_RUNTIME_DIR + DBUS_SESSION_BUS_ADDRESS so spawned
    subprocesses inherit a working bus. If the dir is missing, linger isn't
    enabled — report the reason with the fix instead of guessing.

    Returns (ready, auto_set, runtime_dir, reason). Params are injectable so the
    decision stays unit-testable without touching the host's real /run/user.
    """
    if env is None:
        env = os.environ
    if dir_exists is None:
        dir_exists = os.path.isdir

    if env.get("XDG_RUNTIME_DIR"):
        return (True, False, env["XDG_RUNTIME_DIR"], None)

    if uid is None:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            return (False, False, None,
                    "cannot determine uid (os.getuid unavailable — non-POSIX?)")
        uid = getuid()

    if runtime_dir is None:
        runtime_dir = f"/run/user/{uid}"

    if not dir_exists(runtime_dir):
        user = env.get("USER", "$USER")
        return (False, False, None,
                f"{runtime_dir} does not exist — enable linger first: "
                f"sudo loginctl enable-linger {user}")

    env["XDG_RUNTIME_DIR"] = runtime_dir
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    return (True, True, runtime_dir, None)

def list_installation_repositories(token, _allow_refresh=True):
    url = "https://api.github.com/installation/repositories"
    repos = []
    while url:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                repos.extend(data.get("repositories", []))

                # Pagination
                url = None
                link_header = resp.headers.get("Link")
                if link_header:
                    # Format: <https://api.github.com/...>; rel="next", ...
                    links = link_header.split(",")
                    for link in links:
                        if 'rel="next"' in link:
                            m = re.search(r'<(.*)>', link)
                            if m:
                                url = m.group(1)
                                break
        except urllib.error.HTTPError as e:
            # Same one-shot self-heal as api_request: a 401 means a dead token,
            # so refresh once and restart the listing from page one with the
            # fresh token. Restarting (rather than resuming) keeps it simple and
            # is harmless — the call is idempotent.
            if e.code == 401 and _allow_refresh:
                new_token = refresh_token()
                if new_token and new_token != token:
                    return list_installation_repositories(
                        new_token, _allow_refresh=False)
            try:
                body = e.read().decode("utf-8")
            except:
                body = "(could not read error body)"
            print(f"API error: {e.code} {e.reason}", file=sys.stderr)
            print(body, file=sys.stderr)
            raise
    return repos



# --- Wrapper discovery ------------------------------------------------------
# So a bot can ask "what can this wrapper do?" at runtime instead of guessing
# from the *-as-app naming convention or having to open the README. The list is
# derived live from bin/, so it can't drift out of date as commands are added.

# Scripts that exist but aren't a command a caller should invoke directly:
# the library module, the git shim, and the credential/refresh plumbing that
# git and the systemd timer drive on your behalf.
_DISCOVERY_HIDDEN = {
    "ghapplib.py",            # imported module, not a command
    "github-app-auth",        # the dispatcher itself; subcommands listed separately
    "git",                    # transparent shim; you just run `git`
    "git-credential-github-app",  # git calls this, not you
    "github-token.sh",        # internal: prints a raw token
    "refresh-github-env.sh",  # internal: driven by `refresh` / the timer
}


def wrapper_summary(text):
    """Extract a one-line description from a script's header.

    Handles both a Python `\"\"\"docstring\"\"\"` and a `#`-comment banner:
    returns the first meaningful line (skipping the shebang, blank lines, and
    pure separator rules like `# ====`). Returns "" if nothing usable is found.
    Pure function — takes the file text, touches no filesystem — so it's unit
    testable.
    """
    lines = text.splitlines()

    # Python docstring: content between the first pair of triple quotes.
    joined = "\n".join(lines)
    m = re.search(r'(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', joined, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            s = line.strip()
            if s:
                return s

    # Shell/comment banner: first real text in the leading comment block.
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#!"):
            continue
        if not s.startswith("#"):
            break  # hit code before any description — there's no banner
        s = s.lstrip("#").strip()
        if not s or set(s) <= set("=-"):  # blank or a separator rule
            continue
        return s
    return ""


def list_wrappers(bin_dir=None):
    """Return [(name, summary), ...] for the caller-facing wrapper commands.

    Enumerates executables in bin/, drops the internal plumbing in
    _DISCOVERY_HIDDEN, and pairs each with its header one-liner. Sorted by name
    so output is stable.
    """
    bin_dir = bin_dir or _bin_dir()
    out = []
    try:
        names = os.listdir(bin_dir)
    except OSError:
        return out
    for name in sorted(names):
        if name in _DISCOVERY_HIDDEN or name.startswith("."):
            continue
        path = os.path.join(bin_dir, name)
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            continue
        try:
            with open(path) as f:
                text = f.read(4096)
        except OSError:
            continue
        out.append((name, wrapper_summary(text)))
    return out
