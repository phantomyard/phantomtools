# Crypto-agility: v1 → v2 migration policy

This document closes audit finding #5 ("crypto agility is improved but not
fully solved"): it defines how PhantomDocs moves from the current crypto suite
(v1) to a future suite (v2) **without rewriting historical evidence**.

## 1. Current state (v1)

- `CRYPTO_VERSION = 1` (signing.py): BIP-340 Schnorr over secp256k1 + SHA-256.
- The version is bound into **authenticated state**, not implementation
  convention:
  - every signed mutation envelope carries `crypto_version`;
  - every head seal envelope carries `crypto_version`;
  - the manifest header declares `cryptoVersion`;
  - every node records its own `cryptoVersion`.
- `verify` refuses any node/manifest declaring an unsupported version,
  fail-closed (tested in test_crypto_agility.py).

## 2. Why migration is additive (no rewrite)

The key property that makes v1→v2 safe is that the version is **per-node**,
not a global namespace flag. Each envelope self-describes its suite, so:

- **v1 remains verifiable forever** — a v2 client verifies a v1 node with v1
  primitives, because the node declares version 1.
- **v2 coexists with v1** — one namespace may contain a mix of v1 and v2
  nodes; each verifies under its own declared version.
- **No historical MACs/signatures are rewritten** — migration only changes
  how *new* mutations are signed; existing nodes are never touched.
- **Resumable after crash** — there is no single "migration pass" that can be
  interrupted; the version is self-describing per node, so a crash mid-rollout
  leaves a valid (mixed-version) namespace.
- **Downgrade is rejected** — the version is authenticated state, so an
  attacker cannot strip it to downgrade a v2 node to v1.
- **Old clients cannot misinterpret v2 state** — a v1 client reads
  `crypto_version=2` and refuses fail-closed rather than mis-parsing.

## 3. What remains before a v2 suite can be introduced

The current code pins `CRYPTO_VERSION = 1` and `verify` accepts *exactly* that
version. To actually adopt a v2 primitive, implement:

1. A **primitive registry** — `{version: {sign, verify, domain}}` — instead of
   the hard-coded `_SIGN_DOMAIN` / `_SEAL_DOMAIN` / coincurve calls.
2. `verify` accepts a **supported set** of versions (not a single constant)
   and dispatches each node's verification to the registry entry for its
   declared version.
3. Signing always uses the **latest** supported version.
4. A **mixed-namespace test** proving that a namespace containing v1 and v2
   nodes verifies, and that each node verifies under its own suite.

## 4. Trigger procedure (future, when a v2 primitive exists)

1. Add the v2 primitives + registry entry.
2. Bump `CRYPTO_VERSION` to 2 — new mutations/seals sign with v2.
3. Keep v1 in the verify-supported set forever.
4. Do **not** run a rewrite pass; historical nodes stay v1 and remain
   verifiable under v1 primitives.

## 5. Reference

- `CRYPTO_VERSION`, `_SIGN_DOMAIN`, `_SEAL_DOMAIN`,
  `mutation_envelope`/`seal_envelope`, `sign_mutation`/`verify_mutation`,
  `sign_seal`/`verify_seal` — phantomdocs/src/phantomdocs/signing.py
- `verify` crypto-version gate — phantomdocs/src/phantomdocs/cli.py
- tests — phantomdocs/tests/test_crypto_agility.py
