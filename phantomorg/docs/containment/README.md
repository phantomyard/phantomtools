# Containment evidence contracts

These contracts start roadmap phase A0. They describe evidence; they do not
change a host, grant access, or declare a boundary. The containment program
roadmap itself is not yet part of this repository; this directory is its A0
scope.

The workflow deliberately produces two separate artifacts:

1. `inventory-v1.schema.json` records observed, reported, and unreachable hosts,
   stores, workload identities, maintenance surfaces, unknowns, and exclusions.
2. `boundary-declaration-v1.schema.json` records the owner's later authorization
   for one specific host and fixes the boundaries, workload identities, and
   stores covered by that declaration.

An inventory is never an authorization. A boundary declaration must refer to an
inventory digest and enumerate its scope. Hostname-only authorization is invalid.
Acceptance remains per host; unreachable machines and unknown stores stay explicit
exclusions rather than silently becoming covered.

## Evidence rules

- Use synthetic identifiers in repository fixtures. Never commit real names,
  npubs, email addresses, hostnames, IP addresses, room names, credentials, or
  secret values.
- `observed` means the operator obtained evidence from the named host during the
  recorded collection window. `reported` means another source supplied the fact.
  `unreachable` means no direct evidence was collected.
- Record credential *references and authority* when relevant, never credential
  values. A reference may be a redacted vault item name or an operator-held
  evidence record.
- Each store instance has one candidate boundary. A mixed-boundary store is marked
  `requires_split`; it cannot pass G1 in that state.
- Inventory every maintenance surface. Route-and-credential denial evidence is
  required at G1 only for surfaces classified with privileged effect on the
  accepted interim host.
- A shared store may contain records for several workload identities in one
  boundary. G1 does not claim per-principal record isolation within that store.

## Validation

From `phantomorg/`:

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/test_containment_contracts.py -q
```

The examples are contract fixtures, not a suggested production topology.

## Limits of A0

The contracts validate the shape of the evidence, not its meaning. A document
that validates is still only a document, and a boundary declaration is a claim
until it is checked. In particular, A0 does not establish that:

- the `inventory_digest` refers to a real, previously collected inventory, or
  that the declared `store_ids` and `workload_id` values appear in it;
- the declaration's `exclusions` cover everything the inventory marked
  `unreachable` or unknown — an empty `exclusions` list is valid, so acceptance
  must compare the two documents;
- a store marked `requires_split` is kept out of a boundary — the G1 gate that
  rejects a mixed-boundary store lives outside these schemas;
- a `credential_references` entry is a reference rather than a value — any
  string validates, so keeping secret material out remains a review duty;
- `authorized_by` is authentic — the field is free text, and a declaration is
  not a capability until the owner's authorization is signed or anchored (for
  example, to the signed, append-only machinery used elsewhere in the
  ecosystem).

Treat a validating declaration as something to verify, never as access granted.
