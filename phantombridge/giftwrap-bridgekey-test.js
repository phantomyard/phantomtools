// Gift wrap NIP-17 con wrap EXTERIOR firmado con la clave del BRIDGE (no efimera)
const nostr = require("nostr-tools");
const { nip19, getPublicKey, finalizeEvent } = nostr;
const nip44 = require("nostr-tools/lib/cjs/nip44.js");
const CONFIG = require("./config.json");

const SEAL = 13, GIFT_WRAP = 1059;
const bridgeSk = nip19.decode(CONFIG.nostr.nsec).data;
const pacoPk = CONFIG.agents.paco;
const bridgePk = getPublicKey(bridgeSk);

function makeAuthEvent(relay, challenge) {
  return { kind: 22242, created_at: Math.floor(Date.now()/1000),
    tags: [["relay", relay], ["challenge", challenge]], content: "", pubkey: bridgePk };
}

async function main() {
  const nowTs = Math.floor(Date.now()/1000);
  const content = "[coordinacion-2026-08-08-2] 35955eec: PRUEBA-BRIDGEKEY-por-favor-responde";

  // rumor (kind 14) con created_at = now
  const rumor = { kind: 14, created_at: nowTs, content, tags: [["p", pacoPk]], pubkey: bridgePk };
  // seal (kind 13) con created_at = now, firmado por el bridge
  const seal = finalizeEvent({
    kind: SEAL,
    content: nip44.encrypt(JSON.stringify(rumor), nip44.getConversationKey(bridgeSk, pacoPk)),
    created_at: nowTs, tags: []
  }, bridgeSk);
  // wrap (kind 1059) con created_at = now, FIRMADO CON LA CLAVE DEL BRIDGE (no efimera)
  const wrap = finalizeEvent({
    kind: GIFT_WRAP,
    content: nip44.encrypt(JSON.stringify(seal), nip44.getConversationKey(bridgeSk, pacoPk)),
    created_at: nowTs,
    tags: [["p", pacoPk]]
  }, bridgeSk);

  console.log("wrap creado:", wrap.id.slice(0,12), "pubkey EXTERIOR:", wrap.pubkey.slice(0,10), "(bridge:", bridgePk.slice(0,10) + ")");

  const ws = new WebSocket(CONFIG.nostr.relay);
  let sent = false;
  const timer = setTimeout(() => { console.log("TIMEOUT"); ws.close(); process.exit(1); }, 15000);
  const sendEvent = () => { if (!sent) { sent = true; ws.send(JSON.stringify(["EVENT", wrap])); console.log("EVENT enviado"); } };
  ws.onopen = () => { setTimeout(() => { if (!sent) sendEvent(); }, 800); };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data.toString());
    if (m[0] === "AUTH") {
      ws.send(JSON.stringify(["AUTH", finalizeEvent(makeAuthEvent(CONFIG.nostr.relay, m[1]), bridgeSk)]));
      setTimeout(sendEvent, 300);
    } else if (m[0] === "OK") {
      console.log("OK:", m[1].slice(0,12), m[2], m[3]||"");
      if (m[1] === wrap.id) { clearTimeout(timer); setTimeout(()=>{ws.close(); process.exit(0);}, 500); }
    } else if (m[0] === "NOTICE") { console.log("NOTICE:", m[1]); }
  };
  ws.onerror = (e) => { console.log("WS error:", e.message); };
}
main();
