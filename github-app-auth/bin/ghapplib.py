#!/usr/bin/env python3
import subprocess
import sys
import os
import json
import base64
import re
import shutil
import urllib.request
import urllib.error

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
        with open(env_file) as f:
            for line in f:
                if line.startswith('export GITHUB_TOKEN='):
                    m = re.search(r'export GITHUB_TOKEN="([^"]+)"', line)
                    if m:
                        token = m.group(1)
                    break
    return token

class GitHubAppClient:
    def __init__(self, owner, repo, token, git_bin):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.git_bin = git_bin
        self.api_base = f"https://api.github.com/repos/{owner}/{repo}"
        self.remote_object_cache = set()

    def api_request(self, method, endpoint, data=None):
        url = f"{self.api_base}/{endpoint}" if not endpoint.startswith("http") else endpoint
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
            # Re-read body for error reporting
            try:
                body = e.read().decode("utf-8")
            except:
                body = "(could not read error body)"
            print(f"API error: {e.code} {e.reason}", file=sys.stderr)
            print(body, file=sys.stderr)
            raise

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

def determine_push_strategy(local_sha, remote_sha, remote_known_locally, is_ancestor, branch_exists_remote, force=False):
    """Determine which commits to push and how.
    Returns: (commits_to_push, parent_for_first, needs_force, preserve_parents)
    """
    if force:
        return ([local_sha], "", True, False)
    
    if is_ancestor:
        # Normal fast-forward
        return (None, remote_sha, False, True) # None means needs rev-list
        
    if branch_exists_remote and not remote_known_locally:
        # Recreated SHA scenario
        return ([local_sha], remote_sha, False, False)
        
    if branch_exists_remote:
        # Truly divergent
        return ([local_sha], "", True, False)

    # New branch: caller computes the rev-list of commits not yet on the remote
    # (e.g. `local_sha --not --remotes=<remote>`) and preserves the original
    # parent chain. Returning ([local_sha], ...) here would push only the tip
    # commit as an orphan, severing it from main.
    return (None, "", False, True)

def list_installation_repositories(token):
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
            try:
                body = e.read().decode("utf-8")
            except:
                body = "(could not read error body)"
            print(f"API error: {e.code} {e.reason}", file=sys.stderr)
            print(body, file=sys.stderr)
            raise
    return repos

