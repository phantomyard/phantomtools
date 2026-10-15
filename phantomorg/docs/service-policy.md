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
