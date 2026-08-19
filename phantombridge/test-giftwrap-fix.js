// Verifica que la nueva logica del bridge (2 pasos con check seal.pubkey==rumor.pubkey)
// rechaza el wrap malicioso con rumor.pubkey != seal.pubkey
const {generateSecretKey, getPublicKey, finalizeEvent, getEventHash} = require('nostr-tools');
const nip44 = require('nostr-tools/nip44');

// === claves ===
const bridgePriv = generateSecretKey(), bridgePub = getPublicKey(bridgePriv);
const legitAgentPub = getPublicKey(generateSecretKey());
const attackerPriv = generateSecretKey(), attackerPub = getPublicKey(attackerPriv);

// === helper: reproduce EXACTAMENTE la logica nueva del bridge ===
function bridgeVerifyDecrypt(giftWrap, bridgeSk) {
  try {
    const seal = JSON.parse(nip44.decrypt(giftWrap.content, nip44.getConversationKey(bridgeSk, giftWrap.pubkey)));
    const unwrapped = JSON.parse(nip44.decrypt(seal.content, nip44.getConversationKey(bridgeSk, seal.pubkey)));
    if (!unwrapped || unwrapped.pubkey !== seal.pubkey) {
      return {ok: false, reason: 'mismatch'};
    }
    return {ok: true, unwrapped};
  } catch (e) {
    return {ok: false, reason: 'invalid', err: e.message};
  }
}
// helper legit: wrap normal (rumor.pubkey == seal.pubkey)
function legitWrap(priv, bridgePub) {
  const rumor = {kind:14, created_at:Math.floor(Date.now()/1000), content:'status', tags:[['p',bridgePub]], pubkey:getPublicKey(priv)};
  rumor.id = getEventHash(rumor);
  const ck = nip44.getConversationKey(priv, bridgePub);
  const seal = finalizeEvent({kind:13, content:nip44.encrypt(JSON.stringify(rumor), ck), created_at:Math.floor(Date.now()/1000), tags:[]}, priv);
  return finalizeEvent({kind:1059, content:nip44.encrypt(JSON.stringify(seal), ck), created_at:Math.floor(Date.now()/1000), tags:[['p',bridgePub]]}, priv);
}
// helper malicious: rumor.pubkey=legitAgent pero seal/wrap firmados por attacker
function maliciousWrap(attackerPriv, attackerPub, legitAgentPub, bridgePub) {
  const rumor = {kind:14, created_at:Math.floor(Date.now()/1000), content:'status', tags:[['p',bridgePub]], pubkey:legitAgentPub};
  rumor.id = getEventHash(rumor);
  const ck = nip44.getConversationKey(attackerPriv, bridgePub);
  const seal = finalizeEvent({kind:13, content:nip44.encrypt(JSON.stringify(rumor), ck), created_at:Math.floor(Date.now()/1000), tags:[]}, attackerPriv);
  return finalizeEvent({kind:1059, content:nip44.encrypt(JSON.stringify(seal), ck), created_at:Math.floor(Date.now()/1000), tags:[['p',bridgePub]]}, attackerPriv);
}

let ok = true;
const check = (n,c) => { console.log((c?'✅':'❌'), n); if(!c) ok=false; };

// caso 1: wrap LEGITIMO -> aceptado (rumor.pubkey == seal.pubkey)
const legit = legitWrap(attackerPriv, bridgePub); // un agente legit cualquiera
const r1 = bridgeVerifyDecrypt(legit, bridgePriv);
check('wrap legítimo se acepta (rumor==seal)', r1.ok && r1.unwrapped.content === 'status');

// caso 2: wrap MALICIOSO (rumor=legitAgent, seal=attacker) -> RECHAZADO
const mal = maliciousWrap(attackerPriv, attackerPub, legitAgentPub, bridgePub);
const r2 = bridgeVerifyDecrypt(mal, bridgePriv);
check('wrap malicioso (rumor.pubkey != seal.pubkey) RECHAZADO', !r2.ok && r2.reason === 'mismatch');

// caso 3: wrap invalido (garbage) -> RECHAZADO sin crash
const r3 = bridgeVerifyDecrypt({content:'no-es-json', pubkey:legitAgentPub}, bridgePriv);
check('wrap inválido rechazado sin crash', !r3.ok);

console.log('');
console.log(ok ? '✅ FIX VERIFICADO: la nueva verificacion de identidad del bridge bloquea el spoofing rumor.pubkey!=seal.pubkey' : '❌ FALLO');
