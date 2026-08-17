// FINAL TEST: canonical rumor.id + created_at=now + WRAP SIGNED BY BRIDGE (outer=bridge)
// If phantombot filters by outer pubkey against allowed_npubs, THIS should be processed.
const nostr = require("nostr-tools");
const { nip19, getPublicKey, finalizeEvent, getEventHash } = nostr;
const nip44 = require("nostr-tools/lib/cjs/nip44.js");
const CONFIG = require("./config.json");

const SEAL = 13, GIFT_WRAP = 1059;
const bridgeSk = nip19.decode(CONFIG.nostr.nsec).data;
const pacoPk = CONFIG.agents.paco;

function makeAuthEvent(relay, challenge) {
  return { kind: 22242, created_at: Math.floor(Date.now()/1000),
    tags: [["relay", relay], ["challenge", challenge]], content: "", pubkey: getPublicKey(bridgeSk) };
}

async function main() {
  const nowTs = Math.floor(Date.now()/1000);
  const content = "[coordinacion-2026-08-08-2] 35955eec: TEST-OUTER-BRIDGE-responde-por-favor";

  // rumor with canonical id (like createRumor)
  const rumor = { kind: 14, created_at: nowTs, content, tags: [["p", pacoPk]], pubkey: getPublicKey(bridgeSk) };
  rumor.id = getEventHash(rumor);

  // seal firmado por bridge
  const seal = finalizeEvent({
    kind: SEAL,
    content: nip44.encrypt(JSON.stringify(rumor), nip44.getConversationKey(bridgeSk, pacoPk)),
    created_at: nowTs, tags: []
  }, bridgeSk);

  // WRAP signed by BRIDGE (not ephemeral): conversation (bridge, paco)
  const wrap = finalizeEvent({
    kind: GIFT_WRAP,
    content: nip44.encrypt(JSON.stringify(seal), nip44.getConversationKey(bridgeSk, pacoPk)),
    created_at: nowTs,
    tags: [["p", pacoPk]]
  }, bridgeSk);

  console.log("canonical rumor.id:", rumor.id === getEventHash(rumor));
  console.log("wrap.pubkey = bridge?", wrap.pubkey === getPublicKey(bridgeSk), "| wrap:", wrap.id.slice(0,16), "created:", wrap.created_at, "=now?", wrap.created_at === nowTs);

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
