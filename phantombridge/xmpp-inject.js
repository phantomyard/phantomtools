const {client, xml, jid} = require("@xmpp/client");
const fs = require("fs");
const cfg = JSON.parse(fs.readFileSync("./config.json", "utf8"));
const ROOM = "coordinacion-2026-08-08@conference.meet.example.com";
const NICK = "diag-test";

const xmpp = client({
  service: cfg.xmpp.service,
  domain: cfg.xmpp.domain,
  username: cfg.xmpp.username,
  password: cfg.xmpp.password,
  rejectUnauthorized: false
});

xmpp.on("status", (s) => console.log("status:", s));
xmpp.on("error", (e) => console.log("XMPP ERROR:", e && e.message ? e.message : e));
xmpp.on("offline", () => console.log("offline"));

xmpp.on("online", async (address) => {
  console.log("online como", address.toString());
  try {
    await xmpp.send(xml("presence", {to: jid(ROOM + "/" + NICK)}));
    console.log("presence enviada");
    await new Promise(r => setTimeout(r, 3000));
    await xmpp.send(xml("message", {to: ROOM, type: "groupchat"}, xml("body", {}, "diag-test: hello from the diagnostic script")));
    console.log("mensaje enviado a la sala");
    await new Promise(r => setTimeout(r, 2000));
    process.exit(0);
  } catch (e) {
    console.log("ERROR:", e.message);
    process.exit(1);
  }
});

setTimeout(() => { console.log("TIMEOUT global"); process.exit(2); }, 25000);
xmpp.start().catch(e => console.log("START ERROR:", e.message));
