// ALTO-1 regression test (audit 462e62b): the bridge must cryptographically
// authenticate the ENTIRE NIP-17 chain before processing a gift-wrap.
//
// Without this, an observer of the relay can replay a legit gift-wrap by
// copying pubkey+content, re-stamping created_at and giving it a NEW id with
// an ARBITRARY sig — the bridge (dedup keyed by id, not content) would
// re-execute the command.
//
// This exercises unwrapAndVerifyGiftWrap(), the exact function
// handleIncomingGiftWrap() calls, NOT nostr-tools' unwrapEvent() (which the
// bridge no longer uses and which was the gap the original adversarial test
// left open).
const assert = require('assert');
const {generateSecretKey, getPublicKey, finalizeEvent, getEventHash, nip19} = require('nostr-tools');
const nip44 = require('nostr-tools/nip44');

const bridge = require('./bridge.js');
const {unwrapAndVerifyGiftWrap, CONFIG} = bridge;

// bridgeSk used by unwrapAndVerifyGiftWrap comes from the module's closure.
// Derive the bridge's pubkey from the same nsec so we can encrypt toward it.
const {data: bridgeSkHex} = nip19.decode(CONFIG.nostr.nsec);
const bridgePk = getPublicKey(bridgeSkHex);

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function makeLegitWrap(senderPriv, bridgePk) {
  const senderPub = getPublicKey(senderPriv);
  // rumor (kind:14) — DM from sender to bridge
  const rumor = {
    kind: 14,
    created_at: Math.floor(Date.now() / 1000),
    content: 'status',
    tags: [['p', bridgePk]],
    pubkey: senderPub,
  };
  rumor.id = getEventHash(rumor);
  // seal (kind:13) — signed by sender, encrypted to bridge
  const conv = nip44.getConversationKey(senderPriv, bridgePk);
  const seal = finalizeEvent({
    kind: 13,
    content: nip44.encrypt(JSON.stringify(rumor), conv),
    created_at: Math.floor(Date.now() / 1000),
    tags: [],
  }, senderPriv);
  // wrap (kind:1059) — signed by sender, encrypted to bridge
  const wrap = finalizeEvent({
    kind: 1059,
    content: nip44.encrypt(JSON.stringify(seal), conv),
    created_at: Math.floor(Date.now() / 1000),
    tags: [['p', bridgePk]],
  }, senderPriv);
  return wrap;
}

const senderPriv = generateSecretKey();

console.log('ALTO-1 (NIP-17 auth):');

t('wrap legítimo se autentica y devuelve el rumor', () => {
  const wrap = makeLegitWrap(senderPriv, bridgePk);
  const unwrapped = unwrapAndVerifyGiftWrap(wrap);
  assert.strictEqual(unwrapped.kind, 14);
  assert.strictEqual(unwrapped.pubkey, getPublicKey(senderPriv));
  assert.strictEqual(unwrapped.content, 'status');
});

// --- replay attempts: clone legit content, re-stamp, break the auth ---

t('clone con nuevo created_at + sig arbitraria -> RECHAZADO (firma)', () => {
  const wrap = makeLegitWrap(senderPriv, bridgePk);
  const cloned = JSON.parse(JSON.stringify(wrap));
  cloned.created_at = Math.floor(Date.now() / 1000) + 1; // re-stamp
  cloned.sig = '00'.repeat(64);                           // arbitrary sig
  // id must be recomputed for the clone to be self-consistent, else even the
  // id check trips; we want to prove the SIG check catches it first.
  cloned.id = getEventHash(cloned);
  assert.throws(() => unwrapAndVerifyGiftWrap(cloned), /firma inválida/);
});

t('clone con id arbitrario -> RECHAZADO (id no canónico)', () => {
  const wrap = makeLegitWrap(senderPriv, bridgePk);
  const cloned = JSON.parse(JSON.stringify(wrap));
  cloned.id = 'ab'.repeat(32); // arbitrary id, keeps created_at + sig
  assert.throws(() => unwrapAndVerifyGiftWrap(cloned), /id no canónico/);
});

t('kind!=1059 -> RECHAZADO', () => {
  const wrap = makeLegitWrap(senderPriv, bridgePk);
  const bad = JSON.parse(JSON.stringify(wrap));
  bad.kind = 14;
  bad.id = getEventHash(bad);
  assert.throws(() => unwrapAndVerifyGiftWrap(bad), /kind != 1059/);
});

t('wrap con firma real pero contenido manipulado no descifra -> RECHAZADO', () => {
  const wrap = makeLegitWrap(senderPriv, bridgePk);
  const tampered = JSON.parse(JSON.stringify(wrap));
  // keep sig/id/auth of the outer wrap valid, but corrupt the ciphertext
  // content so seal/rumor cannot be recovered cleanly
  tampered.content = nip44.encrypt('garbage-not-valid-json', nip44.getConversationKey(senderPriv, bridgePk));
  tampered.id = getEventHash(tampered);
  tampered.sig = finalizeEvent(tampered, senderPriv).sig;
  tampered.updated = true; // (harmless; finalizeEvent above used the object)
  assert.throws(() => unwrapAndVerifyGiftWrap(tampered));
});

// --- seal-chain enforcement ---
t('seal con id no canónico -> RECHAZADO', () => {
  const senderPub = getPublicKey(senderPriv);
  const conv = nip44.getConversationKey(senderPriv, bridgePk);
  const rumor = {
    kind: 14, created_at: Math.floor(Date.now() / 1000), content: 'status',
    tags: [['p', bridgePk]], pubkey: senderPub,
  };
  rumor.id = getEventHash(rumor);
  const seal = finalizeEvent({
    kind: 13,
    content: nip44.encrypt(JSON.stringify(rumor), conv),
    created_at: Math.floor(Date.now() / 1000),
    tags: [],
  }, senderPriv);
  seal.id = 'cd'.repeat(32); // break the seal id, keep the rest
  const wrap = finalizeEvent({
    kind: 1059,
    content: nip44.encrypt(JSON.stringify(seal), conv),
    created_at: Math.floor(Date.now() / 1000),
    tags: [['p', bridgePk]],
  }, senderPriv);
  assert.throws(() => unwrapAndVerifyGiftWrap(wrap), /seal id no canónico/);
});

t('rumor con pubkey != seal.pubkey -> RECHAZADO (spoofing identidad)', () => {
  const attackerPriv = generateSecretKey();
  const attackerPub = getPublicKey(attackerPriv);
  const conv = nip44.getConversationKey(attackerPriv, bridgePk);
  // rumor DECLARES to be the legit sender, but the seal is signed by attacker
  const rumor = {
    kind: 14, created_at: Math.floor(Date.now() / 1000), content: 'status',
    tags: [['p', bridgePk]], pubkey: getPublicKey(senderPriv), // declared = legit
  };
  rumor.id = getEventHash(rumor);
  const seal = finalizeEvent({
    kind: 13,
    content: nip44.encrypt(JSON.stringify(rumor), conv),
    created_at: Math.floor(Date.now() / 1000),
    tags: [],
  }, attackerPriv); // signed by attacker -> seal.pubkey = attacker
  const wrap = finalizeEvent({
    kind: 1059,
    content: nip44.encrypt(JSON.stringify(seal), conv),
    created_at: Math.floor(Date.now() / 1000),
    tags: [['p', bridgePk]],
  }, attackerPriv);
  assert.strictEqual(seal.pubkey, attackerPub);
  assert.strictEqual(rumor.pubkey, getPublicKey(senderPriv));
  assert.notStrictEqual(seal.pubkey, rumor.pubkey);
  assert.throws(() => unwrapAndVerifyGiftWrap(wrap), /rumor.pubkey != seal.pubkey/);
});

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
