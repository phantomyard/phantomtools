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

- **Generation** — a new seal keypair is generated off-namespace; its pubkey
  is recorded in the namespace's seal history (not inline in a node).
- **Rotation (re-seal)** — at a given head, sign a new seal with the new key,
  recording a `valid_from` timestamp. Historical seals remain verifiable under
  the key that made them.
- **Revocation** — a seal key carries a `revoked_at`; seals made by that key
  after the revocation point are rejected.
- **Verification** — `verify --org-pubkey` checks the head seal against the
  seal key that was valid *at the seal timestamp* (analogous to
  `key_valid_at` for actors), not against a single fixed key.

## 4. Current status (v1) and gap

- **v1 behavior:** the seal key **is** the org key — `pd seal` derives
  `sealPubkey` from the org nsec, and `verify` requires
  `sealPubkey == org_pubkey`. There is a single `sealPubkey`/`signedRootMac`/
  `sealedHeadSeq` triple in the manifest header; no history, no rotation.
- **Gap:** there is no seal-key history or rotation. Re-sealing with a
  different key today fails `verify` (`seal_pubkey != pubkey_hex`), and
  changing `org_pubkey` itself would change `root_mac` (a new namespace).
- **To close:** (a) this procedure document (done); (b) code: a seal-key
  history in the manifest header + `key_valid_at`-style verification for the
  seal key (tracked as a follow-up issue).

## 5. Reference

- `pd seal` — phantomdocs/src/phantomdocs/cli.py
- `verify --org-pubkey` — phantomdocs/src/phantomdocs/cli.py
- `seal_envelope` / `sign_seal` / `verify_seal` — phantomdocs/src/phantomdocs/signing.py
- `root_mac` — phantomdocs/src/phantomdocs/identity.py
