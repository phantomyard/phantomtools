// Test DM join/leave commands against the bridge via NIP-17 gift-wrap.
// Reuses the bridge's own wrapEvent so the message is identical to production.
// Usage: node test-dm-join.js <nsec-agente> <nsec-bridge> <comando>
//   comando: "join [sala]" | "leave [sala]" | "grabaciones" ...
const { nip19, finalizeEvent, getPublicKey } = require('nostr-tools');
const { wrapEvent } = require('nostr-tools/nip17');
const { makeAuthEvent } = require('nostr-tools/nip42');

const agentNsec = process.argv[2];
const bridgeNsec = process.argv[3];
const command = process.argv.slice(4).join(' ');
if (!agentNsec || !bridgeNsec || !command) {
  console.error('uso: node test-dm-join.js <nsec-agente> <nsec-bridge> <comando>');
  process.exit(1);
}

const RELAY = process.env.RELAY || 'ws://127.0.0.1:7777';
const agentSk = nip19.decode(agentNsec).data;
const bridgePk = getPublicKey(nip19.decode(bridgeNsec).data);

const WebSocket = globalThis.WebSocket;
function publishViaWs(event) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(RELAY);
    const timer = setTimeout(() => { ws.close(); reject(new Error('timeout')); }, 10000);
    let sent = false;
    const sendEvent = () => {
      if (sent) return;
      sent = true;
      ws.send(JSON.stringify(['EVENT', event]));
    };
    ws.onopen = () => {
      // NIP-42: el relay puede pedir AUTH antes de aceptar; si en 800ms no llega, publicar directo
      setTimeout(() => { if (!sent) sendEvent(); }, 800);
    };
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data.toString());
      if (m[0] === 'AUTH') {
        const authEv = finalizeEvent(makeAuthEvent(RELAY, m[1]), agentSk);
        ws.send(JSON.stringify(['AUTH', authEv]));
        setTimeout(sendEvent, 300);
      } else if (m[0] === 'OK' && m[1] === event.id) {
        clearTimeout(timer); ws.close(); resolve(m[2]);
      } else if (m[0] === 'NOTICE') {
        clearTimeout(timer); ws.close(); reject(new Error(m[1]));
      }
    };
    ws.onerror = () => { clearTimeout(timer); reject(new Error('ws error')); };
  });
}

async function main() {
  console.log('enviando comando:', command, '-> puente', bridgePk.slice(0, 8));
  const wrapped = wrapEvent(agentSk, { publicKey: bridgePk }, command, 'TestJoin');
  const ok = await publishViaWs(wrapped);
  console.log('publicado OK:', ok);
  // El puente procesa y responde por DM al agente; esperamos a que el relay lo reciba.
  // (The reply arrives at the relay; the real agent would see it in its PhantomChat.)
  await new Promise(r => setTimeout(r, 5000));
  console.log('done. (la respuesta del puente llega como DM al agente)');
  process.exit(0);
}

main().catch(e => { console.error('error:', e.message); process.exit(1); });
