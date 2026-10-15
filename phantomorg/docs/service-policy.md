# Portable service policy

PhantomOrg validates optional service intent and exports organization-scoped JSON
for a deployment adapter. This is not OS enforcement, a runtime approval receipt
or a credential store. Existing persona builds and deployments are unchanged.

This fragment uses actors from the bundled verdant-aquaponics example. It is not
an AU deployment policy:

```yaml
service_policy:
  version: 1
  default_access: deny
  services:
    - id: browser
      kind: private
      owner_actor: dana
      consumers: [{kind: actor, id: dana}]
      approval: per-request
      approvers: [lucia, marco]
    - id: view
      kind: shared
      consumers: []
      approval: none
      approvers: []
    - id: worker
      kind: shared
      consumers: [{kind: service, id: view}]
      approval: none
      approvers: []
```

Private services have exactly their owning actor as consumer. Approvers do not
acquire session access. Hierarchy, memory scopes, categories and tool strings do
not create grants. Shared services are separate logical principals with explicit
actor or shared-service consumers. IDs are distinguished by kind and scoped to
the organization. Empty consumers declares no incoming access. Self-consumers
are rejected; mutual service dependencies are allowed.

Per-request approval requires explicit actor approvers; `none` requires an empty
list. This does not prove approval, decide whether approvers mean any/all, or
define expiry, delegation or revocation. A runtime must reject approval-dependent
operations until a separately validated approval protocol and valid request
authorization exist. This feature does not implement that runtime protocol.

```bash
po validate --org org.yaml
po service-policy --org org.yaml --out ./dist/service-policy.json
```

Export is explicit, independent of `po build`/`po deploy`. Missing policies,
unknown references/fields, duplicates and malformed declarations fail without
replacing an existing output. Automation must check exit status and never deploy
the previous artifact after a failed export. Output cannot replace org.yaml.

The manifest includes format version, organization identity, actor IDs, canonical
service declarations and a SHA-256 of the compact, sorted-key JSON payload before
adding `policy_sha256`. Arrays are normalized. This digest is not a signature or
proof of provenance/enforcement. Installation consumers must verify the trusted
source, expected organization, supported format and current policy, and reject
missing or unsupported intent.

Linux accounts, units, ports, DNS, routes, namespaces, credentials and profile
paths belong to installation bindings. A future adapter must cover all actor
processes and clients, preserve shared dependencies, deny undeclared connections
and produce a verified plan before applying changes with operator approval.
No deployment adapter or network isolation is provided by this feature.

## Offline installation review

`po service-plan` reviews installation declarations and an operator-supplied
snapshot. It does not inspect the running host, validate a Linux unit, install
files, issue approvals or change networking. A plan always has `deployable: false`
and `enforcement_applied: false`. Structurally consistent input yields
`review-required`, never `ready`. Blockers yield `blocked` and exit code 2, with
diagnostics written to the output; malformed input fails with exit code 1 and
preserves the previous output. Exit code 0 still requires all listed reviews.

```bash
po service-plan --org org.yaml --bindings installation.json \
  --inventory inventory.json --out dist/service-plan.json
```

Bindings version 1 has exactly these fields:

| Field | Meaning |
|---|---|
| `version` | Integer 1 |
| `organization_id` | Exact organization ID from the trusted source model |
| `policy_sha256` | Exact digest from the current service-policy export |
| `host_id` | Explicit installation host identifier |
| `actors` | One binding for every actor in this organization |
| `services` | One binding for every declared service |

Each actor binding has `id`, `account` and nonempty `units`. Each service binding
also has `endpoint`. A unit is `{"name":"bot.service","scope":"system"}` or
`{"name":"bot.service","scope":"user"}`; user units are scoped by their binding
account. An endpoint is `{"transport":"tcp","address":"127.0.0.1","port":9000}`.
UDP and literal IPv6 loopback are also supported. Wildcard/public addresses,
hostnames, UNIX sockets and uninstantiated systemd templates are rejected by this
initial planner. Rejection is not a recommendation to convert an existing socket.

The snapshot has exactly `version`, `host_id`, `accounts` and `units`. Accounts
are `{"name":"pb-dana","uid":1007}` records. Observed units have exactly `name`,
`scope`, `account`, `namespace` (an observed identifier), and boolean `active`.
Include dormant service units and units from user managers, not just listeners
or running main units. The snapshot is evidence supplied by the operator, not
proof that collection is complete or trustworthy. These sample identifiers and
UIDs are illustrative; do not copy them into a live installation.

Private services must share their owner's account and proposed namespace.
Different actor/shared-service domains cannot share a UID, including account
aliases. Root, unknown accounts, unconfirmed units, duplicate unit assignments,
endpoint collisions and unmapped units under contained accounts block review.
Other accounts' units are reported as outside scope, never modified or claimed
to be isolated. A service requiring per-request approval always blocks until a
verified runtime authorization adapter exists; declarations are insufficient.

Namespace proposals use the complete SHA-256 of a canonical JSON tuple of
organization, principal kind and ID. Display labels do not determine namespace
identity. Explicit consumer edges are preserved with `gateway_verified: false`;
they do not authorize a host port forward. Input digests identify the reviewed
bindings and snapshot. Consumers must still verify source provenance and freshness.

This first planner covers a single host containing the organization's full actor
set. Distributed placement requires a later explicit contract; omitted actors
are not silently assumed to be remote. The review checklist retains DNS/NAT,
authenticated shared gateways, all account processes, user managers, socket
activation, hardening, profile preservation, IPv6/host/peer paths and rollback.
No executable drop-ins or firewall rules are generated.
