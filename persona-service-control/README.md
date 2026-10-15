# Persona service control

A standalone Linux/systemd utility for one fixed service-start operation per
contained identity binding. Python 3.10 or later and the standard library suffice.
It does not depend on AU, Phantombot or PhantomOrg and contains no installation's
persona names, ports, credentials or browser paths.

Status: review component. This utility does not install itself, enable units,
implement network isolation or establish deployment readiness. Validation and
rendering are read-only; only the explicitly invoked runtime client/handler path
requests the configured service start after a separate approved installation.

## Trust boundary

Each installation supplies a root-managed JSON binding with exactly five fields:

```json
{
  "version": 1,
  "id": "example-browser",
  "account": "pb-example",
  "uid": 12345,
  "unit": "chrome-example.service"
}
```

These are illustrative values, not an existing account or a usable deployment.
The numeric UID must match the current account database, be nonzero and remain
pinned. Unknown fields, duplicate keys, invalid identifiers and UID drift fail
closed. Each binding permits only its one service; requests cannot select a unit,
port, command, environment or credential. Root remains a trusted administrator.

The canonical binding path is `/etc/persona-service-control/<id>.json`. The file
must be regular, root-owned, at most 16 KiB, and mode 0644 or another supported
read-only-to-nonowners mode. It contains public installation metadata, no secrets.
Use 0644 when the contained client must read it. Every parent directory must be
root-owned and not writable by group or others. Symlinks are rejected throughout
the path using descriptor-relative, no-follow opens. FIFO and other special
files, untrusted owners and changes during a read are rejected.

The AF_UNIX socket path derives from the binding ID:
`/run/persona-service-control/<id>.sock`. It is root-owned, mode 0660, with the
account's primary group as its socket group. The handler additionally checks
kernel peer credentials against the pinned UID, so another user in that group
does not acquire access. The client checks both socket metadata and a root peer.
All code and units must also be root-owned and not writable by contained accounts.

## Commands and lifecycle

After an operator has separately reviewed and installed the code and binding:

```sh
/usr/bin/python3 -I /usr/local/libexec/persona_service_control.py validate \
  --config /etc/persona-service-control/example-browser.json
/usr/bin/python3 -I /usr/local/libexec/persona_service_control.py render \
  --config /etc/persona-service-control/example-browser.json
```

`validate` reads metadata and prints validation status. `render` prints a JSON
map of proposed socket/service files; it creates no files and always reports
`deployable: false` and `enforcement_applied: false`. The renderer requires the
canonical configuration path. Proposed units use `/usr/bin/python3` and the
program path above; these are Linux deployment prerequisites to verify locally.

Review the generated files and use `systemd-analyze verify` before asking to
install or enable them. Code and binding installation, daemon reload, activation,
changes to clients and removal of obsolete privileges are separate operator
actions, not implicit command effects. Rollback must be designed for the complete
installation. Nothing here copies, opens or changes browser profiles.

The configured identity's root-managed tool launcher can invoke:

```sh
/usr/bin/python3 -I /usr/local/libexec/persona_service_control.py client \
  --config /etc/persona-service-control/example-browser.json
```

An already approved, enabled socket activates a separate root handler for each
connection. The client does not elevate through sudo; it can retain
NoNewPrivileges. The generated handler also retains NoNewPrivileges, has an empty
capability set, and uses a private network namespace with only AF_UNIX enabled.
It asks the host system manager to start the configured service. That service's
own namespace, hardening and profile remain governed by its existing unit.

The wire request is exactly `ENSURE\n` followed by write EOF. Identity is checked
before reading it. Frames have a strict byte limit and absolute two-second
deadline. The fixed `systemctl --no-ask-password start <configured-unit>` call has
a 25-second timeout, no shell and a fresh minimal environment. Its output is
discarded. The socket bounds concurrency; each handler has a 35-second lifetime.
No caller data or session data is logged by the program.

`STARTED\n` means only that the system-manager command returned zero. It does not
prove browser health or login, and never grants application/query approval.
`ERROR\n`, `DENIED\n` or a connection failure are failures. A client timeout does
not prove that PID 1 cancelled a start job. A repeated request starts the same
configured unit; it does not restart an already active service. After success,
the real tool must check its own server in the correct namespace.

## Relation to organization policy

PhantomOrg can declare service owners, consumers and approvers. Its compiled
policy and installation plan remain inputs to operator review. This utility
does not modify org.yaml, consume approval receipts or automatically infer that
a binding is permitted by an organization. The deployment adapter must validate
the binding against the approved service policy before enabling it. This work
does not yet provide an automatic policy-to-runtime adapter.

Service-start permission, application authorization and query approval are
separate. In particular, a persona that can directly access its authenticated
browser can bypass a purely instructional per-query rule. Do not claim technical
query enforcement from this controller or from generated instructions.

Network containment also remains separate: all real clients, browsers, scheduled
jobs and idle observers must be integrated with the identity namespace together.
Shared services, DNS/NAT, host escape paths, persistent enrollment and rollback
require a complete installation plan. A working control socket is not proof of
isolation or a reason to weaken existing service hardening.

## Tests

```sh
python -m unittest discover -s persona-service-control/tests -v
```

Linux tests cover strict metadata, wrong peers, fixed arguments, malformed frames,
absolute deadlines, errors and a real AF_UNIX exchange with NoNewPrivs enabled.
The service manager is always fake; no real service or authenticated session is
accessed. Root-only tests use disposable directories under /root to test actual
ownership, symlinks, unsafe parents, FIFOs and oversized configuration files.
They are skipped without root; CI runs them separately with root privileges.
Generated units are verified in CI without loading, enabling or starting them.

Primary references:

- https://github.com/systemd/systemd/blob/v255/man/systemd.socket.xml
- https://github.com/systemd/systemd/blob/v255/man/systemd.exec.xml
- https://www.kernel.org/doc/html/latest/userspace-api/no_new_privs.html
