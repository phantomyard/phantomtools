const { nip19, finalizeEvent, getPublicKey } = require("nostr-tools");
const { makeAuthEvent } = require("nostr-tools/nip42");
const { wrapEvent } = require("nostr-tools/nip17");
// Uso: RELAY=ws://... AGENT_NSEC=nsec... BRIDGE_NSEC=nsec... node test-relay-auth.js
const RELAY = process.env.RELAY || "ws://127.0.0.1:7777";
const AGENT_NSEC = process.env.AGENT_NSEC;
const BRIDGE_NSEC = process.env.BRIDGE_NSEC;
if (!AGENT_NSEC || !BRIDGE_NSEC) { console.error("faltan AGENT_NSEC/BRIDGE_NSEC"); process.exit(2); }
const agentSk = nip19.decode(AGENT_NSEC).data;
const bridgePk = getPublicKey(nip19.decode(BRIDGE_NSEC).data);
console.log("pepa pk:", getPublicKey(agentSk));
const wrapped = wrapEvent(agentSk, { publicKey: bridgePk }, "join [formacion-2026-08-07-prueba-dm2]", "TestAuth");
const ws = new WebSocket(RELAY);
let authed = false, sent = false;
ws.onopen = () => { console.log("ws open"); ws.send(JSON.stringify(["EVENT", wrapped])); };
ws.onmessage = (e) => {
  const m = JSON.parse(e.data.toString());
  console.log("<<", JSON.stringify(m).slice(0, 200));
  if (m[0] === "AUTH" && !authed) {
    authed = true;
    const authEv = finalizeEvent(makeAuthEvent(RELAY, m[1]), agentSk);
    ws.send(JSON.stringify(["AUTH", authEv]));
    setTimeout(() => { if (!sent) { sent = true; ws.send(JSON.stringify(["EVENT", wrapped])); } }, 400);
  }
  if (m[0] === "OK") { console.log("OK:", m[2]); ws.close(); process.exit(0); }
  if (m[0] === "NOTICE") { console.log("NOTICE:", m[2]); ws.close(); process.exit(1); }
};
ws.onerror = (e) => { console.error("ws error", e.message); process.exit(1); };
setTimeout(() => { console.log("timeout"); process.exit(2); }, 12000);
