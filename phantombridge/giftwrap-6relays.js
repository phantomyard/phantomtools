// Publica el MISMO gift wrap (firmado con clave bridge, pubkey exterior = bridge) a los 6 relays
const nostr = require("nostr-tools");
const { nip19, getPublicKey, finalizeEvent } = nostr;
const nip44 = require("nostr-tools/lib/cjs/nip44.js");
const CONFIG = require("./config.json");

const SEAL = 13, GIFT_WRAP = 1059;
const bridgeSk = nip19.decode(CONFIG.nostr.nsec).data;
const pacoPk = CONFIG.agents.paco;
const bridgePk = getPublicKey(bridgeSk);

const RELAYS = [
  "ws://relay.example.invalid:7777",
  "wss://relay.damus.io",
  "wss://nos.lol",
  "wss://relay.primal.net",
  "wss://nostr.mom",
  "wss://nostr.data.haus"
];

function makeAuthEvent(relay, challenge) {
  return { kind: 22242, created_at: Math.floor(Date.now()/1000),
    tags: [["relay", relay], ["challenge", challenge]], content: "", pubkey: bridgePk };
}

async function main() {
  const nowTs = Math.floor(Date.now()/1000);
  const content = "[coordinacion-2026-08-08-2] 35955eec: PRUEBA-6RELAYS-por-favor-responde";

  const rumor = { kind: 14, created_at: nowTs, content, tags: [["p", pacoPk]], pubkey: bridgePk };
  const seal = finalizeEvent({
    kind: SEAL,
    content: nip44.encrypt(JSON.stringify(rumor), nip44.getConversationKey(bridgeSk, pacoPk)),
    created_at: nowTs, tags: []
  }, bridgeSk);
  const wrap = finalizeEvent({
    kind: GIFT_WRAP,
    content: nip44.encrypt(JSON.stringify(seal), nip44.getConversationKey(bridgeSk, pacoPk)),
    created_at: nowTs,
    tags: [["p", pacoPk]]
  }, bridgeSk);

  console.log("wrap:", wrap.id.slice(0,12), "pubkey exterior:", wrap.pubkey.slice(0,10), "created:", nowTs);

  for (const relay of RELAYS) {
    await new Promise((resolve) => {
      const ws = new WebSocket(relay);
      let sent = false;
      const timer = setTimeout(() => { console.log(relay, "TIMEOUT"); try{ws.close()}catch{}; resolve(); }, 12000);
      const sendEvent = () => { if (!sent) { sent = true; ws.send(JSON.stringify(["EVENT", wrap])); console.log(relay, "EVENT enviado"); } };
      ws.onopen = () => { setTimeout(() => { if (!sent) sendEvent(); }, 800); };
      ws.onmessage = (e) => {
        const m = JSON.parse(e.data.toString());
        if (m[0] === "AUTH") {
          ws.send(JSON.stringify(["AUTH", finalizeEvent(makeAuthEvent(relay, m[1]), bridgeSk)]));
          setTimeout(sendEvent, 300);
        } else if (m[0] === "OK") {
          console.log(relay, "OK:", m[1].slice(0,12), m[2], m[3]||"");
          if (m[1] === wrap.id) { clearTimeout(timer); setTimeout(()=>{try{ws.close()}catch{}; resolve();}, 300); }
        } else if (m[0] === "NOTICE") { console.log(relay, "NOTICE:", m[1]); }
      };
      ws.onerror = () => { console.log(relay, "WS ERROR"); clearTimeout(timer); resolve(); };
    });
  }
  console.log("DONE");
}
main();
