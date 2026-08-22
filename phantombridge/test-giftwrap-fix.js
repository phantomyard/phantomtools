// Verifies that the bridge's new logic (2 steps with check seal.pubkey==rumor.pubkey)
// rejects the malicious wrap with rumor.pubkey != seal.pubkey
const {generateSecretKey, getPublicKey, finalizeEvent, getEventHash} = require('nostr-tools');
const nip44 = require('nostr-tools/nip44');

// === keys ===
const bridgePriv = generateSecretKey(), bridgePub = getPublicKey(bridgePriv);
const legitAgentPub = getPublicKey(generateSecretKey());
const attackerPriv = generateSecretKey(), attackerPub = getPublicKey(attackerPriv);

// === helper: reproduces EXACTLY the bridge's new logic ===
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
// legit helper: normal wrap (rumor.pubkey == seal.pubkey)
function legitWrap(priv, bridgePub) {
  const rumor = {kind:14, created_at:Math.floor(Date.now()/1000), content:'status', tags:[['p',bridgePub]], pubkey:getPublicKey(priv)};
  rumor.id = getEventHash(rumor);
  const ck = nip44.getConversationKey(priv, bridgePub);
  const seal = finalizeEvent({kind:13, content:nip44.encrypt(JSON.stringify(rumor), ck), created_at:Math.floor(Date.now()/1000), tags:[]}, priv);
  return finalizeEvent({kind:1059, content:nip44.encrypt(JSON.stringify(seal), ck), created_at:Math.floor(Date.now()/1000), tags:[['p',bridgePub]]}, priv);
}
// malicious helper: rumor.pubkey=legitAgent but seal/wrap signed by attacker
function maliciousWrap(attackerPriv, attackerPub, legitAgentPub, bridgePub) {
  const rumor = {kind:14, created_at:Math.floor(Date.now()/1000), content:'status', tags:[['p',bridgePub]], pubkey:legitAgentPub};
  rumor.id = getEventHash(rumor);
  const ck = nip44.getConversationKey(attackerPriv, bridgePub);
  const seal = finalizeEvent({kind:13, content:nip44.encrypt(JSON.stringify(rumor), ck), created_at:Math.floor(Date.now()/1000), tags:[]}, attackerPriv);
  return finalizeEvent({kind:1059, content:nip44.encrypt(JSON.stringify(seal), ck), created_at:Math.floor(Date.now()/1000), tags:[['p',bridgePub]]}, attackerPriv);
}

let ok = true;
const check = (n,c) => { console.log((c?'✅':'❌'), n); if(!c) ok=false; };

// case 1: LEGITIMATE wrap -> accepted (rumor.pubkey == seal.pubkey)
const legit = legitWrap(attackerPriv, bridgePub); // any legit agent
const r1 = bridgeVerifyDecrypt(legit, bridgePriv);
check('legit wrap is accepted (rumor==seal)', r1.ok && r1.unwrapped.content === 'status');

// case 2: MALICIOUS wrap (rumor=legitAgent, seal=attacker) -> REJECTED
const mal = maliciousWrap(attackerPriv, attackerPub, legitAgentPub, bridgePub);
const r2 = bridgeVerifyDecrypt(mal, bridgePriv);
check('malicious wrap (rumor.pubkey != seal.pubkey) REJECTED', !r2.ok && r2.reason === 'mismatch');

// case 3: invalid wrap (garbage) -> REJECTED without crash
const r3 = bridgeVerifyDecrypt({content:'no-es-json', pubkey:legitAgentPub}, bridgePriv);
check('invalid wrap rejected without crash', !r3.ok);

console.log('');
console.log(ok ? '✅ FIX VERIFIED: the bridge\'s new identity verification blocks rumor.pubkey!=seal.pubkey spoofing' : '❌ FAILED');
