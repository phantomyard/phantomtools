# Classification transition preparation

## Current capability: read-only planning

`pd classification-plan` reports a proposed whole-document category change.
It does not reclassify documents, modify permissions, register references,
read original content, sign a transition or create a migration credential.
Successful exit means a report was produced, not that a transition is approved
or that content, signatures, audit records or trust anchors were verified.

```bash
pd classification-plan urn:sample-org:doc:record.txt \
  --expected-mac <current-version-mac> \
  --expected-category category-2 \
  --category category-4-sample \
  --reason 'Restrict this document history to the project audience' \
  --org-yaml /trusted/org.yaml --root /document-store
```

The command uses the existing authenticated actor resolution. It requires write
access under both the source and target categories with the unchanged owners.
It refuses missing documents, folders, stale current MACs or source categories,
unchanged categories, undeclared targets, broken structure, mixed historical
categories/owners and source references shared by different document identities.
Category access comes exclusively from PhantomOrg's existing resolver.

The JSON report includes:

- The document identity, current MAC and retained version/content hashes.
- The source/target categories and unchanged owners.
- Canonical JSON digests of the observed manifest and organization policy.
- Actors who would gain, lose or retain read access under the proposed category.
- `history_scope: all-versions`, `execution_available: false` and explicit blockers.

Protect reports as private operational data: they include document identities,
hashes, owner IDs and reader IDs. They describe a snapshot only. Digests are
stale-state comparison guards, not signatures or evidence of authorization.
The reader-impact lists contain native PhantomOrg actors. They do not establish
human/counterpart access: the viewer must separately evaluate its authoritative
human ACL and project scope. A persona's access must not be reported as proof
that its human counterpart can access the same records.
No new service, daemon, credentials or independent ACL registry is introduced.

## Intended historical behavior

A future transition should change the effective category of the logical
document and its entire retained history, including reads by old version MAC,
tags, search, version listings, originals and assistant context. Otherwise an
old MAC would bypass a restriction, or an authorized project reader would still
be unable to read the historical material explicitly included in the transition.

Original content, content hashes, version MACs, source references, owners and
existing signatures remain unchanged. Reclassification is an access-policy
event, not a content edition. Content rollback must retain the effective access
category; reversing a category requires another authorized policy event.

Reader impact is a proposal for review. A category number alone does not define
whether access becomes broader or narrower; the actual PhantomOrg grant graph
determines which actors gain and lose access. No automatic category inheritance
or project membership should be invented to make a transition succeed.

## Required before any write capability

1. Define a native append-only, signed classification event that binds the
   document identity, retained history, old/new categories, unchanged owners,
   source identity, expected current state, organization policy hash, actor,
   timestamp, predecessor and audit sequence. Do not edit signed nodes in place.
2. Specify the store protocol/capability gate before introducing such events.
   An older reader or writer must refuse a store it cannot interpret. Silently
   ignoring a classification event would preserve the superseded permissions.
3. Centralize effective-category resolution in PhantomDocs for every read/write
   path, reference deduplication, verification, rollback, sealing and recovery.
   Historical source references must resolve consistently without re-registering
   content. Conflicting identities or incomplete events must fail closed.
4. Require an authenticated custodian with source and target write authority;
   verify all retained signatures, audit and trust anchors. Re-read and recompute
   every guard under the native store lock. Never execute a report by trusting
   its actor, reader-impact list or claimed approval status.
5. Specify atomic commit, crash recovery and idempotent retry semantics. A policy
   event must not become partially effective. A changed policy, current version,
   source identity or history invalidates the reviewed plan.
6. Update consumers to use the effective classification, including ingestion,
   the viewer collector, original-file authorization and assistant retrieval.
   Unsupported events must stop consumption, not fall back to old categories.
7. Test old-MAC access, history/search/originals, unauthorized actors, tampering,
   replay, stale state, crashes, retry, content rollback and older-client refusal
   before rehearsing an exact batch on an isolated store copy.

Only after those changes and an explicit operational review should any live
category or project document-type policy change. Normal ingestion must keep
rejecting classification conflicts in the meantime. Pending workflow markers
must not be deleted to force duplicate registration.

## Candidate signed event contract

`classification_events.py` provides a closed, versioned envelope, Schnorr
signature primitives and record/history digests. It does not provide a command
to apply events, persist them, authorize a signer or change a reader's category.
The current store format and all existing read/write paths remain unchanged.

The envelope is UTF-8 JSON with sorted object keys, compact separators,
ASCII-escaped strings and no non-finite numbers. All fields below are required;
unknown fields are rejected. Category IDs and hash strings are already canonical,
not normalized after signing. Owners must be sorted, unique, non-empty strings.

| Fields | Binding |
| --- | --- |
| `version`, `kind`, `history_scope` | Exact integer `1`, `reclassify`, `all-versions` |
| `org`, `namespace`, `root_mac` | Organization and exact store identity |
| `urn` | Non-empty document URN belonging to `org` |
| `actor` | Claimed native actor; binding to the signing key is checked separately |
| `from_category`, `target_category`, `owners` | Different categories and unchanged ownership |
| `expected_manifest_digest`, `expected_current_mac` | Complete reviewed snapshot and current version |
| `policy_hash`, `history_digest` | Authoritative organization policy and complete retained history |
| `seq`, `audit_seq` | Exact positive integers, never booleans or floats |
| `prev_audit_head`, `previous_event` | Audit and classification predecessors, nullable only when absent |
| `reason`, `issued_at` | Bounded review reason and valid UTC timestamp at second precision |

All hashes and predecessor IDs use lowercase 64-hex strings. `prev_audit_head`
must be null exactly when `audit_seq` is 1. Other sequence/predecessor consistency
must be established against the locked store by the future application service.
The timestamp is part of permanent evidence, not a temporary-link expiry window.

The signed message is SHA-256 of
`b"phantomdocs-classification-event-v1\x00" + canonical_payload`.
Signing uses the existing coincurve dependency and BIP-340 Schnorr/x-only keys;
no new identity, credential store or cryptographic dependency is introduced.
This domain differs from existing mutation signatures and access-link protocols.

A signed record has exactly `payload`, `sigPubkey` and `sig`. Its predecessor ID
is SHA-256 of `b"phantomdocs-classification-record-v1\x00" + canonical_record`,
after validating signature integrity. The complete record, including its signer
and signature, is bound. A history digest is SHA-256 of
`b"phantomdocs-classification-history-v1\x00" + canonical_history`, where history
is the non-empty list of complete retained nodes in verified oldest-to-newest
chain order. Original nodes, including signatures and locations, are not rewritten.

### Integrity is not permission

`verify_classification_signature()` only establishes cryptographic integrity.
A valid signature can still claim another actor, use an obsolete policy, refer
to unrelated history or request an unauthorized change. The future native
service must bind the key to the authenticated actor, apply the existing key
lifecycle/trust-anchor rules and source/target ACL, and independently reconstruct
all snapshot/history/audit guards under the store lock. It must not trust the
event's claims, the planner's actor-impact list or an asserted approval flag.

No caller may use these primitives to append records to the current manifest or
to publish an effective category. Store-version gating, durable atomic commit,
replay/retry handling, recovery, sealing and consistent reader/collector behavior
remain prerequisites. Existing signed nodes and their categories continue to be
interpreted exactly as before until that complete operation is implemented and
reviewed. No live reclassification is enabled by this cryptographic contract.
