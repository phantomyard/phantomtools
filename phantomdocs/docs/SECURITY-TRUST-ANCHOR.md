# Trust anchor & seal key lifecycle

This document defines the *operational* security procedure around the
namespace trust anchor (the `--org-pubkey`) and the organization **seal key**.
It closes audit finding #6: "root seal trust procedure must be operationally
defined", and the follow-up requirement to document seal-key rotation as
separate from actor-key rotation (issue #76).

## 1. The trust anchor: where `org-pubkey` comes from

The namespace root MAC is:

```
root_mac = H( org_id || org_pubkey || namespace )     # identity.py
```

The `org_pubkey` is therefore **part of the namespace's cryptographic
identity** — not merely an operational key. `verify --org-pubkey` recomputes
the root MAC from that key and checks the head seal against it. If an attacker
could substitute their own `org_pubkey`, they could forge a root and a seal
over arbitrary content.

**Rule (fail-closed):** the trusted `org_pubkey` MUST come from an *external,
out-of-band* trust source — never from the repository/namespace being
verified. Concretely:

```
trusted org key (out-of-band)
        ↓
pd verify --org-pubkey <npub>
        ↓
recompute root MAC   → must equal manifest.rootMac
        ↓
verify head seal     → seal must be made by that key
        ↓
verify head state    → sealedHeadSeq == headSeq (no advance past the seal)
```

In the PhantomOrg model the external source is the organization's Nostr
identity: the `npub` declared in `org.yaml` and provisioned by `po build`.
The operator obtains that `npub` from the org's own key ceremony / vault, and
passes it to `verify` out-of-band — it is never read from the manifest or the
namespace under verification.

## 2. Two keys, two roles

- **`org_pubkey` (identity key)** — baked into `root_mac`; it *is* the
  namespace identity. It MUST NOT rotate: rotating it changes the root MAC and
  invalidates every node MAC (a new namespace).
- **seal key** — signs the head seal (`rootMac` + `headSeq` + `headMac` +
  `auditSeq` + `auditHead`). This is an *operational* key and should rotate on
  compromise, loss, or staff turnover.

These must be tracked as separate lifecycles. Actor-key rotation (#76) is a
third, distinct lifecycle (`key_valid_at` over actor `keys` in `org.yaml`).

## 3. Seal key rotation lifecycle

The namespace header carries the lifecycle (issue #104):

- **`sealIdentityNpub`** — the org **identity** key, recorded at the first
  seal. It is baked into `root_mac` and therefore never rotates;
  `verify --org-pubkey` cross-checks it against the operator's out-of-band
  anchor, so a manifest that declares its own seal identity is rejected.
- **`sealKeys`** — one record per org-authorized seal key:
  `{npub, valid_from, valid_until, revoked_at, delegation}`. `delegation` is
  the identity key's signature over
  `{identity_npub, root_mac, seal_npub, valid_from, valid_until, revoked_at}`,
  so an entry is trustworthy only if the anchor authorized it: the history is
  never self-attested (anyone who can edit the manifest could otherwise
  declare a key of their own and re-seal a forged head).
- **`seals`** — the append-only seal history, one event per `pd seal`:
  `{npub, ts, cs, headMac, auditSeq, auditHead, sig, requireSignatures,
  cryptoVersion}`. A re-seal at the same head appends; the latest matching
  event is the live seal.

**Generation** — a new seal keypair is generated off-namespace.

**Rotation (re-seal)** — the identity key authorizes the new seal key, then
that key seals the head:

```bash
# A mutation advanced the head; authorize a new seal key and rotate to it.
pd seal --nsec-file seal-b.nsec --org-nsec-file org-identity.nsec --root ./docs

# Later re-seals with an already-authorized key need no org-key ceremony.
pd seal --nsec-file seal-b.nsec --root ./docs
```

`root_mac` is unchanged — only the sealing key rotates. Seals made under
earlier keys stay verifiable under the key that made them: `pd verify`
re-checks every recorded event, and `pd seal-keys` shows the chain.

**Revocation** — authorized by the identity key (a revocation the anchor did
not sign is refused), and fail-closed: a seal made by that key at or after
`revoked_at` is rejected. Seals made *before* the revocation stay valid — a
compromised key does not retroactively invalidate the evidence of earlier
heads — but the revoked key can no longer seal, so the namespace picks up a
new authorized key with `pd seal --org-nsec-file`.

```bash
pd revoke-seal-key <npub> --org-nsec-file org-identity.nsec --root ./docs
```

**Verification** — `verify --org-pubkey` checks the head seal against the seal
key that was valid *at the seal timestamp* (analogous to `key_valid_at` for
actors, #76), not against a single fixed key, and re-verifies the whole
history.

**What cannot rotate** — `org_pubkey` itself is part of `root_mac`, so
rotating it is a namespace re-issue (`pd init`), never a header edit.

## 4. Status

- **Implemented (issue #104):** seal-key history (`manifest.seals`),
  identity-key delegations (`manifest.sealKeys`), `pd seal` rotation (and
  refusal to seal with an unauthorized key), `pd revoke-seal-key`,
  `pd seal-keys`, and `pd verify` checking the seal key valid at the seal
  timestamp plus re-verifying every recorded seal.
- **Legacy (pre-#104) manifests:** carry no history, so `verify` keeps the
  original rule — the single `sealPubkey` must be the org identity key.
- **Out of scope:** encryption-at-rest (SPEC §14, decision 4).

## 5. Reference

- `pd seal`, `pd revoke-seal-key`, `pd seal-keys`, `verify --org-pubkey` —
  phantomdocs/src/phantomdocs/cli.py
- seal-key history helpers (`record_seal_event`, `seal_key_valid_at`) —
  phantomdocs/src/phantomdocs/manifest.py
- `seal_envelope` / `sign_seal` / `verify_seal` / `delegation_envelope` /
  `sign_delegation` / `verify_delegation` — phantomdocs/src/phantomdocs/signing.py
- `root_mac` — phantomdocs/src/phantomdocs/identity.py
