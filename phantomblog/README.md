# PhantomBlog

Static bilingual publishing with a private editorial dashboard and agent proposal
workflow. Python 3.11+, standard library only at runtime. Public websites receive
HTML, CSS and image files; they need no Python service or new build step.

Development home: `salvaalba-dev/phantomyard-tools/phantomblog`. A future upstream
submission to `phantomyard/phantomtools` is a separate human decision. No runtime
behaviour, installer or generated URL depends on the GitHub owner name.

## What works in v0.1

- Articles, bilingual categories, dates, drafts, featured styling and image uploads.
- Supplied English/Spanish HTML bodies, inline or in body source folders. No AI
  writing or translation happens during a build.
- Site → category → article design defaults, three layouts, section ordering,
  scoped article CSS and a shared HTML template editor.
- Deterministic archives of 12 records, localized metadata, raster social previews,
  language links and visitor share destinations.
- Validation before writes, source revision checks, manifest ownership and atomic
  file replacement. Existing legacy pages keep their bytes and URLs.
- Authenticated loopback dashboard; MCP stdio tools read/build/propose. Humans
  review agent proposals and control external publication.
- Configurable MCP deployment, social and PhantomChat messaging adapters. The
  reviewed workflow builds, deploys if selected, verifies exact live HTML and then
  shares, with per-connection receipts and duplicate protection.

Platform names in the dashboard are **configuration choices**, not bundled account
connections. A working MCP server, provider authorization, and its actual tool
schema must be configured on the target Phantombot machine. This checkout contains
no credentials and has not posted to any platform. PhantomChat opens its existing
client and prepares article context; in-dashboard sending requires a verified
messaging MCP adapter. It does not embed or copy the GPL PhantomChat client.

## Start locally

From the `phantomblog/` directory:

```sh
python bin/phantomblog --root /absolute/path/to/my-blog init
python bin/phantomblog --root /absolute/path/to/my-blog --json dry-run
python bin/phantomblog --root /absolute/path/to/my-blog build
python bin/phantomblog --root /absolute/path/to/my-blog check
```

Replace the path with your workspace. The default workspace is beside this tool,
not relative to the caller's current directory. Explicit relative `--root` paths
are resolved from the caller. Every content/asset/output path is resolved from
that workspace. On Windows use `python bin\phantomblog` or `bin\phantomblog.cmd`.
No package installation is required. `pip install .` is optional.

Set an administrator token through your process manager or Phantombot vault.
For a standalone development session, inject a random token into
`PHANTOMBLOG_ADMIN_TOKEN` without saving it in the repo. Then:

```sh
python bin/phantomblog --root /absolute/path/to/my-blog dashboard
# Or resolve it through the existing persona vault:
python bin/phantomblog --root /absolute/path/to/my-blog dashboard \
  --token-ref vault:PHANTOMBLOG_ADMIN_TOKEN --persona editorial
```

Open `http://127.0.0.1:8787/` and sign in with that token. The service binds only
to loopback and checks authentication, Host and Origin. For remote access use an
authenticated tunnel retaining that URL; exposing this HTTP listener is not a
supported production setup. Tokens never go in URLs or browser storage.

## Authoring and integration

Read [the agent workflow](docs/AUTHORING.md), [connector configuration](docs/CONNECTORS.md),
[architecture and ownership](docs/ARCHITECTURE.md), and [verification](docs/VERIFICATION.md).

Sources live in `blog.json`, optional `bodies/`, `assets/` and `templates/`.
Generated files live in `public/`. Commit sources **and** generated files to your
website repo. Your existing GitHub-to-server sync can publish them unchanged.
PhantomBlog itself never silently commits, pushes, merges or changes server cron.

Exit codes: `0` success; `1` stale outputs from `check`; `2` invalid input, unsafe
ownership or unavailable local source; `3` external integration failure or partial
sharing failure; `130` interrupted. `check` and `dry-run` write no files and make
no network calls. Their results do not mean a page is live.

## Install and test

Linux/macOS: `./install.sh` symlinks the reviewed checkout's wrapper into
`~/.local/bin` (or `$PREFIX/bin`). It refuses to overwrite divergent installed
copies. Windows: add this tool's `bin` folder to PATH through system settings.

```sh
python -m unittest discover -s tests -v
node --check phantomblog/resources/dashboard.js
python tests/preview.py --port 8790
```

The preview creates a disposable test workspace with clearly labelled synthetic
articles, images and a synthetic login token declared in `tests/preview.py`.
It performs no deployments or messages. Stop it with Ctrl+C. Fixtures are never
installed into a real publication.

MIT. See [LICENSE](LICENSE).
