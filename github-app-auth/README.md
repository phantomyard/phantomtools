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
| `bin/gh` | Wrapper that injects the App token as `GH_TOKEN` so `gh api`/`gh issue`/`gh repo`… work; refuses `gh pr create` with a pointer to `create-pr-as-app` and passes `gh auth` straight through |
| `bin/git-push-as-app` | Push via GitHub API with `--dry-run` and `-f`/`--force` support; refuses history rewrites on the default branch |
| `bin/git-fetch-as-app` | Fetch via temporary authenticated remote; auto-cleans stale `__app_fetch_*` remotes on crash |
| `bin/git-pull-as-app` | Fetch + merge/rebase |
| `bin/git-clone-as-app` | Clone a GitHub repo with App auth; the discoverable entry point for clone (plain `git clone` also works via the credential helper) |
| `bin/list-repos-as-app` | List repositories accessible to the installation |
| `bin/create-pr-as-app` | Open a pull request via the REST API with the App identity |
| `bin/github-app-auth` | Control & diagnostics: `list` (discover commands), `doctor` (health checks with fixes), `refresh` (force a token refresh) |
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

### Discover what the wrapper can do

Not sure which commands exist? Ask the wrapper instead of opening this README:

```bash
github-app-auth list        # every wrapper command + a one-line description
git-push-as-app --help      # usage for any individual command
```

`list` enumerates the `bin/` directory live, so it stays accurate as commands
are added. Every `*-as-app` command accepts `-h`/`--help`.

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

### Cloning a repo

`install.sh` registers `git-credential-github-app` as the github.com credential
helper, so a **plain clone just works** — no special command:

```bash
git clone https://github.com/owner/repo
```

There is also a `git-clone-as-app` entry point. It exists mainly for
discoverability (it matches the `*-as-app` family bots look for) and works even
where the global credential helper isn't registered — it injects the App token
for the clone only, via an HTTP header, so the token never persists into the
cloned repo's `.git/config`:

```bash
git-clone-as-app owner/repo                       # shorthand → https://github.com/owner/repo
git-clone-as-app https://github.com/owner/repo target-dir
```

SSH URLs (`git@github.com:owner/repo.git`) and non-GitHub URLs pass straight
through to real git — those authenticate themselves.

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

### Using `gh` under the App

The `gh` wrapper makes the GitHub CLI usable on a bot host that has no `gh auth login`. It reads the installation token from `~/.github_env` and injects it as `GH_TOKEN`, so API-backed commands just work:

```bash
gh api repos/OWNER/REPO/pulls          # authenticated as the App
gh issue list --repo OWNER/REPO
gh repo view OWNER/REPO
```

What it deliberately does **not** do:

- `gh pr create` is refused with a usage message pointing to `create-pr-as-app` (an App token has no user to resolve the author/HEAD); it does not forward your arguments. Override with `GITHUB_APP_GH_ALLOW_PR_CREATE=1` if you really want raw `gh`.
- `gh auth …` passes straight through, untouched and **without** token injection, so a human can still log in normally — App auth lives in `~/.github_env`, not in gh's keyring (check it with `github-app-auth doctor`).
- If no App (`ghs_*`) token is loadable, the wrapper touches nothing: real `gh` runs with whatever auth you already have (a PAT, `gh auth login`, SSH). It never degrades a machine with a real login.

This only takes effect when `~/.local/bin` is **earlier** on `$PATH` than the system `gh` (same requirement as the `git` wrapper).

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

### Non-login sessions (`systemctl --user` and the bus)

`systemctl --user` needs `XDG_RUNTIME_DIR` to find the D-Bus socket. In a non-login session — a bot, or a shell reached via `sudo su -` — PAM doesn't set it, so the refresh timer looks dead and the manual workaround is `export XDG_RUNTIME_DIR=/run/user/$(id -u)`. Both `install.sh` and `github-app-auth doctor` now derive this automatically from `/run/user/<uid>` when user **linger** is enabled, so the export is no longer needed. If linger is off, the runtime dir doesn't exist and you'll be told to enable it:

```bash
sudo loginctl enable-linger "$USER"
```

### Drift reporting (`report-drift`)

The installed wrappers are symlinks into this repo, so the repo is the single
source of truth. `report-drift` catches the case where an installed copy was
edited *in place* — a diverged regular file instead of a symlink, invisible to
git and lost on the next `install.sh`. It scans **every** phantomtools tool
(anything with a `bin/` + `install.sh`), not just this one:

```bash
github-app-auth report-drift                       # scan all, file an issue per drift
github-app-auth report-drift --dry-run             # show diffs, file nothing
github-app-auth report-drift bot-inbox/bot-inbox   # limit to one wrapper
```

Each drift opens one de-duplicated issue on the repo (keyed by a stable
`<!-- report-drift:<tool>/<script> -->` marker, so re-running won't pile up
duplicates). It defaults to filing on the repo's `origin` remote — override with
`--repo owner/repo`. Filing needs the App's **Issues: read + write** permission.

## Requirements

- `openssl`, `python3`, `curl`, `git`
- A GitHub App with:
  - Contents: read + write
  - Installations: read
  - Pull requests: read + write *(only needed for `create-pr-as-app`)*
  - Issues: read + write *(only needed for `report-drift`)*
  - Installed on the target repos
- The App's private key file on disk

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
