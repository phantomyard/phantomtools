const {nip19, finalizeEvent, getPublicKey} = require("nostr-tools");
const {wrapEvent} = require("nostr-tools/nip17");
const {makeAuthEvent} = require("nostr-tools/nip42");
const fs = require("fs");
const cfg = JSON.parse(fs.readFileSync("./config.example.json", "utf8"));
const RELAY = cfg.nostr.relay;
const BRIDGE_PK = "00000000000000000000000000000000000000000000000000000000000000ff";
const {data: pacoSk} = nip19.decode(process.env.PACO_NSEC);
console.log("paco pk:", getPublicKey(pacoSk));
const wrapped = wrapEvent(pacoSk, {publicKey: BRIDGE_PK}, "[coordinacion-2026-08-08] hola puente, prueba de Paco", "Jitsi coordinacion-2026-08-08");
const ws = new WebSocket(RELAY);
let sent = false;
const timer = setTimeout(() => { console.log("RESULT: TIMEOUT"); ws.close(); process.exit(0); }, 10000);
const sendEvent = () => { if (sent) return; sent = true; ws.send(JSON.stringify(["EVENT", wrapped])); };
ws.onopen = () => { setTimeout(() => { if (!sent) sendEvent(); }, 800); };
ws.onmessage = (e) => {
  const m = JSON.parse(e.data.toString());
  console.log("MSG:", JSON.stringify(m).slice(0, 200));
  if (m[0] === "AUTH") {
    const ev = finalizeEvent(makeAuthEvent(RELAY, m[1]), pacoSk);
    ws.send(JSON.stringify(["AUTH", ev]));
    setTimeout(sendEvent, 300);
  } else if (m[0] === "OK") {
    if (m[1] === wrapped.id) { clearTimeout(timer); console.log("RESULT OK:", m[2], m[3] || ""); ws.close(); process.exit(0); }
  } else if (m[0] === "NOTICE") { clearTimeout(timer); console.log("RESULT NOTICE:", m[1]); ws.close(); process.exit(0); }
};
ws.onerror = (e) => { console.log("RESULT WSERR:", e.message); process.exit(0); };
