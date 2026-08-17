// E2E: inyectar mensaje en sala como participante (nick fijo 35955eec) usando la cuenta bridge
const tls = require("tls");
const origTlsConnect = tls.connect;
tls.connect = function (...args) {
  if (args[0] && typeof args[0] === "object") args[0] = { ...args[0], rejectUnauthorized: false };
  return origTlsConnect.apply(this, args);
};
const {client, xml} = require("@xmpp/client");

// Uso: XMPP_PASSWORD=... node e2e-inject.js [room] [msg] [nick]
const ROOM = process.argv[2] || "coordinacion-2026-08-08-2";
const MSG = process.argv[3] || "test";
const NICK = process.argv[4] || "35955eec";
const XMPP_PASSWORD = process.env.XMPP_PASSWORD || require("./config.json").xmpp.password;

const xmpp = client({
  service: "xmpp://127.0.0.1:5222",
  domain: "auth.meet.example.com",
  username: "bridge",
  password: XMPP_PASSWORD,
});

xmpp.on("error", e => { console.error("xmpp error:", e.message); process.exit(1); });
xmpp.on("online", async () => {
  const roomJid = ROOM + "@conference.meet.example.com";
  await xmpp.send(xml("presence", {to: roomJid + "/" + NICK}, xml("x", {xmlns: "http://jabber.org/protocol/muc"})));
  await new Promise(r => setTimeout(r, 2000));
  await xmpp.send(xml("message", {to: roomJid, type: "groupchat"}, xml("body", {}, MSG)));
  console.log("enviado a", roomJid, "como", NICK, ":", MSG);
  await new Promise(r => setTimeout(r, 2500));
  process.exit(0);
});
xmpp.start().catch(e => { console.error("start error:", e.message); process.exit(1); });
