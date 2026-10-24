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
