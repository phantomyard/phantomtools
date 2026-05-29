# github-app-auth

Use a **GitHub App** for `git push`, `git fetch`, and `git pull`.

GitHub App installation tokens (`ghs_*`) do not work over HTTPS git operations. This tool wraps the GitHub API so normal git commands work transparently.

## Why?

- **No PATs** — Personal Access Tokens expire, leak, and can't be scoped to an org.
- **Short-lived tokens** — GitHub App tokens auto-refresh every 50 minutes.
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
| `bin/git-push-as-app` | Push via GitHub API with `--dry-run` and `-f`/`--force` support |
| `bin/git-fetch-as-app` | Fetch via temporary authenticated remote; auto-cleans stale `__app_fetch_*` remotes on crash |
| `bin/git-pull-as-app` | Fetch + merge/rebase |
| `bin/list-repos-as-app` | List repositories accessible to the installation |
| `bin/create-pr-as-app` | Open a pull request via the REST API with the App identity |
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
