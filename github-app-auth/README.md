# github-app-auth

Use a **GitHub App** for `git push`, `git fetch`, and `git pull`.

GitHub App installation tokens (`ghs_*`) do not work over HTTPS git operations. This tool wraps the GitHub API so normal git commands work transparently.

## Why?

- **No PATs** — Personal Access Tokens expire, leak, and can't be scoped to an org.
- **Short-lived tokens** — GitHub App tokens auto-refresh every 50 minutes.
- **Self-healing** — a dead or expired token is refreshed automatically: API calls retry once on a `401`, and entry points refresh proactively when the token is missing or past its stored expiry.
- **Transparent** — Once installed, `git push origin main` just works.

## Install from source

```bash
git clone https://github.com/phantomyard/phantomtools.git ~/repos/phantomtools
cd ~/repos/phantomtools/github-app-auth
./install.sh
```

The repo is public, so no token is needed to clone.

Prerequisites in `~/.env`:
```bash
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=/home/you/.ssh/my-app.private-key.pem
```

`install.sh` lays down the binaries and the timer regardless, so you can install first and fill in `~/.env` afterwards.

## How it works

```
git push origin main
    ↓
~/.local/bin/git (wrapper) sees github.com remote
    ↓
git-push-as-app  →  GitHub API (blobs → trees → commits → ref update)
```

Token refresh (systemd user timer):
```
github-app-auth-refresh.timer  (every 50 min)
    ↓
refresh-github-env.sh
    ↓
github-token.sh  (JWT → installation token)
    ↓
~/.github_env
```

## Files

| File | Purpose |
|------|---------|
| `bin/git` | Wrapper placed in `~/.local/bin`; routes GitHub repos to `-as-app` variants |
| `bin/git-push-as-app` | Push via GitHub API with `--dry-run` and `-f`/`--force` support; refuses history rewrites on the default branch |
| `bin/git-fetch-as-app` | Fetch via temporary authenticated remote; auto-cleans stale `__app_fetch_*` remotes on crash |
| `bin/git-pull-as-app` | Fetch + merge/rebase |
| `bin/list-repos-as-app` | List repositories accessible to the installation |
| `bin/create-pr-as-app` | Open a pull request via the REST API with the App identity |
| `bin/github-app-auth` | Control & diagnostics: `doctor` (health checks with fixes) and `refresh` (force a token refresh) |
| `bin/git-credential-github-app` | Git credential helper reading `~/.github_env` |
| `bin/github-token.sh` | Generates JWT, finds installation ID, exchanges for access token |
| `bin/refresh-github-env.sh` | Timer wrapper that refreshes `~/.github_env` |
| `systemd/github-app-auth-refresh.timer` | systemd user timer |
| `systemd/github-app-auth-refresh.service` | systemd oneshot service |
| `install.sh` | Symlinks, timers, git config, prerequisite check, test |
| `uninstall.sh` | Reverses `install.sh` |

## Usage

Once installed, standard git commands work as usual for GitHub remotes:

```bash
git push origin main
git fetch origin
git pull origin main
```

### Force-push safety

The push wrapper refuses to rewrite history on the repository's **default
branch**. A plain `git push --force` (or any divergent push after a rebase)
to the default branch silently drops commits other people pushed — so it is
blocked with a message pointing you at a PR or a rebase-to-fast-forward.

The default branch is read live from the GitHub API, so this works whatever
it is named — `main`, `master`, `trunk`, `develop`, … no configuration needed.
Force-pushing **feature** branches is untouched, and ordinary fast-forwards to
the default branch always pass.

```bash
git push --force origin develop   # refused if develop is the default branch
git push --force origin feat/x    # fine — feature branch

# Deliberate override for the rare legitimate case:
GITHUB_APP_ALLOW_FORCE_DEFAULT=1 git-push-as-app origin develop
```

### Discover accessible repos

Check which repositories your App installation has access to:

```bash
# List all repos (name, visibility, permissions)
list-repos-as-app

# Get just the clone URLs (handy for automation)
list-repos-as-app --clone-urls

# JSON output for jq processing
list-repos-as-app --json | jq -r '.[].full_name'
```

If [phantombot](https://github.com/phantomyard/phantombot) is installed on this host, `install.sh` automatically captures a hint to its memory so the agent learns about this capability on its next turn — no manual prompting required.

### Create a pull request

Installation tokens can't drive `gh` the usual way, so use the wrapper to open PRs via the REST API:

```bash
# Title only; head defaults to the current branch, base to the repo default branch
create-pr-as-app "Anonymize IP in rate limiter"

# Explicit branches and a body
create-pr-as-app "Fix login redirect" --head fix/login --base main --body "Closes #42"

# Body from a file (or stdin with -), draft PR, JSON output for scripting
create-pr-as-app "WIP: refactor" --body-file pr-body.md --draft --json
```

A `403`/`404` here almost always means the App is missing the **Pull requests: Read & write** permission — `git push` works with only `Contents: write`, so a successful push followed by a failing PR points straight at it. The tool prints that hint instead of leaving you guessing.

### Diagnose & refresh

When auth misbehaves, don't poke the API with a raw token — ask the tool:

```bash
# Health check: config, token presence + file permissions, expiry, refresh
# timer, and live API reachability. Each failure prints the exact fix.
github-app-auth doctor

# Force a token refresh now (same path the systemd timer uses)
github-app-auth refresh
```

`doctor` exits non-zero if anything is broken, so it drops straight into scripts and CI. Most token trouble self-heals (a `401` triggers one refresh-and-retry, and a missing/expired token is refreshed before use), but `doctor` is the fast way to confirm *why* something failed instead of guessing.

## Requirements

- `openssl`, `python3`, `curl`, `git`
- A GitHub App with:
  - Contents: read + write
  - Installations: read
  - Pull requests: read + write *(only needed for `create-pr-as-app`)*
  - Installed on the target repos
- The App's private key file on disk

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
