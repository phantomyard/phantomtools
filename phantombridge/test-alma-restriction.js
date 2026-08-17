// Permission restriction test: Alma (restricted to almaponia-*) tries to speak in a non-allowed room
// Usage: node test-alma-restriction.js <nsec-alma> <mensaje>
const { nip19, finalizeEvent, getPublicKey } = require("nostr-tools");
const { wrapEvent, unwrapEvent } = require("nostr-tools/nip17");
const { makeAuthEvent } = require("nostr-tools/nip42");

const RELAY = "ws://relay.example.invalid:7777";
const BRIDGE_PK = "00000000000000000000000000000000000000000000000000000000000000ff";
const agentSk = nip19.decode(process.argv[2]).data;
const agentPk = getPublicKey(agentSk);
const msg = process.argv[3];
const wrapped = wrapEvent(agentSk, { publicKey: BRIDGE_PK }, msg, "Comando");
console.log("agente:", agentPk.slice(0, 12), "| mensaje:", msg);

const ws = new WebSocket(RELAY);
let sent = false, eosed = false;
const t = setTimeout(() => { console.log("TIMEOUT (sin respuesta del bridge)"); process.exit(2); }, 20000);

const sendCmd = () => { if (sent) return; sent = true; ws.send(JSON.stringify(["EVENT", wrapped])); console.log("[evento] publicado"); };

ws.onopen = () => {
  ws.send(JSON.stringify(["REQ", "t", { kinds: [1059], "#p": [agentPk] }]));
  setTimeout(sendCmd, 800);
};
ws.onmessage = (e) => {
  const m = JSON.parse(e.data.toString());
  if (m[0] === "AUTH") {
    ws.send(JSON.stringify(["AUTH", finalizeEvent(makeAuthEvent(RELAY, m[1]), agentSk)]));
    ws.send(JSON.stringify(["REQ", "t", { kinds: [1059], "#p": [agentPk] }]));
    setTimeout(sendCmd, 300);
  } else if (m[0] === "EOSE") {
    eosed = true;
  } else if (m[0] === "EVENT" && m[2].kind === 1059 && m[2].id !== wrapped.id && eosed) {
    try {
      const unwrapped = unwrapEvent(m[2], agentSk);
      console.log("=== RESPUESTA DEL BRIDGE ===");
      console.log(unwrapped.content);
      clearTimeout(t);
      process.exit(0);
    } catch (err) {}
  } else if (m[0] === "OK" && m[1] === wrapped.id) {
    console.log("[ok] evento aceptado por relay");
  }
};
