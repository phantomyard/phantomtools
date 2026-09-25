# G2 contract delivery — implementation candidate

This is the first G2 component, not a G2 acceptance or a published contract
release. G1 merged in #119; the A0 collector/validator followed in #120.
The private host evidence reported in #119 covers five traffic-bearing
workloads on one authorized host and explicitly excludes other workloads,
adapters, shared secrets and semantic indexes. It is not global coverage.

## Consumer verification

`tools/verify_phantom_contract.py` is a reference verifier to be reviewed and
pinned in the consumer repository **before** any candidate archive is opened.
It requires Python 3.10+ and `cryptography==46.0.5`. It never imports candidate
code, extracts files, downloads keys, changes a lock, or installs a release.

```sh
python tools/verify_phantom_contract.py \
  --archive work/phantom-policy-1.0.0.tar.gz \
  --manifest work/contract-manifest.json \
  --signature work/contract-manifest.sig \
  --lock phantom-contract.lock.json \
  --anchor trust/phantom-contract-authority.pub
```

Exit 0 and JSON `ok: true` mean these exact bytes passed verification against
the supplied lock, anchor and system UTC time. Nonzero and `ok: false` deny
the operation. Failure diagnostics omit candidate contents and filesystem
paths. The fixtures demonstrate success and failure with synthetic ephemeral
keys; no production authority is generated or provisioned here.

## Wire contract

The manifest is a UTF-8 JSON object. Its exact bytes are signed with Ed25519
over `b"phantom-policy-contract-release/v1\x00" + manifest_bytes`.
The signature file contains 64 raw bytes; the anchor file contains 32 raw
Ed25519 public-key bytes. Key ID/fingerprint is `sha256:` followed by the
lowercase SHA-256 hex digest of those 32 bytes. Digests elsewhere use the same
prefix. Duplicate JSON keys and unknown top-level fields are rejected.

Required manifest fields:

- `format_version`: integer 1; `authority`: `phantomyard/phantomtools`.
- `source_commit`: full upstream commit; `versions`: exact supported tuple below.
- `assets`: exact `archive`, `manifest`, `signature` URLs under
  `https://github.com/phantomyard/phantomtools/releases/download/phantom-policy-v1.0.0/`,
  respectively `phantom-policy-1.0.0.tar.gz`, `contract-manifest.json`,
  `contract-manifest.sig`. These are proposed locators, not existing releases.
- `archive_digest`, `key_id`, nonnegative integer `sequence`, `issued_at`,
  `expires_at` (UTC Unix seconds, issuance inclusive, expiry exclusive).
- `files`: complete map of relative file paths to digests. The gzip tar archive
  contains only ordinary file entries: no directory headers, links, special
  files, sparse files, PAX attributes, traversal, case aliases or device names.
  Limits: 16 MiB compressed, 64 MiB total content, 4096 entries, 240-character paths.

The consumer lock repeats `format_version`, `authority`, `source_commit`,
`versions`, `assets`, `archive_digest` and adds `manifest_digest`,
`consumer_commit`, `verifier_commit` (full reviewed commits),
`anchor_fingerprint`, `anchor_provisioning: "producer_under_owner_review"`,
`owner_review` (review reference), and `minimum_sequence`. It rejects older
sequences and pins one exact release. Updating any pin or sequence floor is a
reviewed consumer change; a workload must not be able to rewrite or roll back
the lock, anchor, verifier or installed release. The consumer/verifier commits
identify previously reviewed implementation revisions, avoiding a self-hashing
lock commit. Their ancestry/review must be checked in release acceptance.

The proposed version tuple is `contract_schema=1.0.0`, `fixture_suite=1.0.0`,
`reference_evaluator=0.1.0`, `signature_envelope=1`. These are compatibility
identifiers, not a claim that an evaluator or full fixture suite exists yet.
An unknown tuple fails closed. Tests generate a complete example lock and
manifest; no fake production digests, review references or keys are committed.

## Trust and integration limits

The canonical upstream release operation supplies the public key. Salvador
Alba reviews its bytes/fingerprint; the consumer maintainer records that review
and pin in reviewed consumer history. No independent provisioning channel is
claimed. Initial authenticity retains trust in the producer and owner review;
a malicious correctly signed release is still possible. The verifier validates
the reference's presence, not whether a human actually reviewed it.

A fork may prepare a candidate. Upstream must accept its source/provenance,
rebuild or independently verify it, and publish the final signed assets under
`phantomyard/phantomtools`. Consumers pin those upstream bytes and commits.
Matching coordinates inside a manifest alone cannot prove GitHub publication.

The CLI assumes operator-trusted UTC and protected consumer files. It does not
implement disconnected leases, suspend/restart clock anchoring, revocations,
or durable runtime sequence state. Do not use it as an offline access broker.
A future installer must consume the same verified bytes without a replacement
race, stage safely, and activate atomically. No extraction permission follows
for a separately reread path just because this CLI returned success.

## Remaining G2 acceptance work

1. Publish the actual upstream contract/evaluator/fixtures; populate the final
   digest, source/consumer/verifier tuple and owner-reviewed key record.
2. Obtain Phantombot maintainer acceptance and land its own non-empty required
   conformance job in **Phantombot's repository** before runtime integration.
   This producer CI job cannot substitute for that maintainer-owned gate.
3. Add boundary bundle signatures binding target, policy revision, compiler/
   template version and complete outputs; enforce installation and prompt/tool
   verification with cross-boundary canaries. Contract signature authority must
   not be reused as document or persona-bundle authority.
4. Implement authenticated broker decisions, finite signed leases, bounded
   offline mode, protected audit receipts, revocation and fail-closed recovery.
5. Enforce per-principal read/write/discovery in shared boundary stores,
   including direct backend/index access. Sponsor dual membership grants no
   implicit delegation: persona and explicit delegation authority intersect.
6. Complete each host's production placement, capacity, VM and admin-plane
   trust evidence. Same-host containers do not satisfy the VM target.

Adapters remain disabled/excluded until their own enforcement passes. G2
acceptance also requires runtime canaries and observed host evidence. None of
these pending items is made complete by the tests in this directory.

## Development

```sh
python -m pip install -r phantom-policy/requirements-dev.txt
python -m pytest phantom-policy/tests -q
ruff check phantom-policy
ruff format --check phantom-policy
```
