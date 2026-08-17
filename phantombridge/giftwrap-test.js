// Simular un mensaje de sala del bridge -> gift wrap NIP-17 hacia paco
const {wrapEvent} = require("nostr-tools/lib/cjs/nip17.js");
const {nip19, getPublicKey, finalizeEvent} = require("nostr-tools");
const CONFIG = require("./config.json");

const bridgeSk = nip19.decode(CONFIG.nostr.nsec).data;
const pacoPk = CONFIG.agents.paco;

function makeAuthEvent(relay, challenge) {
  return {
    kind: 22242,
    created_at: Math.floor(Date.now()/1000),
    tags: [["relay", relay], ["challenge", challenge]],
    content: "",
    pubkey: getPublicKey(bridgeSk)
  };
}

async function main() {
  const content = "[coordinacion-2026-08-08-2] 35955eec: TEST-FINAL-MAQUI-por-favor-responde";
  const wrapped = wrapEvent(bridgeSk, {publicKey: pacoPk}, content, "Jitsi coordinacion-2026-08-08-2");
  console.log("gift wrap creado:", wrapped.id, "kind:", wrapped.kind, "para:", wrapped.tags.find(t=>t[0]==="p")?.[1]?.slice(0,10));

  const ws = new WebSocket(CONFIG.nostr.relay);
  let sent = false;
  const timer = setTimeout(() => { console.log("TIMEOUT publicando"); ws.close(); process.exit(1); }, 15000);

  const sendEvent = () => {
    if (sent) return;
    sent = true;
    ws.send(JSON.stringify(["EVENT", wrapped]));
    console.log("EVENT enviado:", wrapped.id.slice(0,10), "a", CONFIG.nostr.relay);
  };

  ws.onopen = () => {
    console.log("conectado, esperando AUTH o 800ms...");
    setTimeout(() => { if (!sent) sendEvent(); }, 800);
  };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data.toString());
    if (m[0] === "AUTH") {
      console.log("AUTH challenge, firmando...");
      const ev = finalizeEvent(makeAuthEvent(CONFIG.nostr.relay, m[1]), bridgeSk);
      ws.send(JSON.stringify(["AUTH", ev]));
      setTimeout(sendEvent, 300);
    } else if (m[0] === "OK") {
      console.log("OK:", m[1].slice(0,10), m[2], m[3]||"");
      if (m[1] === wrapped.id) { clearTimeout(timer); setTimeout(()=>{ws.close(); process.exit(0);}, 500); }
    } else if (m[0] === "NOTICE") {
      console.log("NOTICE:", m[1]);
    } else {
      console.log("msg:", m[0], JSON.stringify(m[1]||"").slice(0,50));
    }
  };
  ws.onerror = (e) => { console.log("WS error:", e.message); };
}
main();
