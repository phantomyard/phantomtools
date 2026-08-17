// Capture the MUC join error (for diagnosis)
const tls = require('tls');
const origTlsConnect = tls.connect;
tls.connect = function (...args) {
  if (args[0] && typeof args[0] === 'object') args[0] = { ...args[0], rejectUnauthorized: false };
  return origTlsConnect.apply(this, args);
};
const {client, xml} = require('@xmpp/client');

const ROOM = process.argv[2] || 'sala-puente-test';
const PASSWORD = process.env.TEST_PW;

const xmpp = client({
  service: 'xmpp://127.0.0.1:5222',
  domain: 'auth.meet.example.com',
  username: process.env.TEST_USER,
  password: PASSWORD,
});

xmpp.on('error', e => { console.error('xmpp error:', e.message); process.exit(1); });
xmpp.on('stanza', (stanza) => {
  const s = stanza.toString();
  if (s.includes('error') || s.includes('presence')) {
    console.log('STANZA RECIBIDO:', s);
  }
});
xmpp.on('online', async () => {
  console.log('online');
  const roomJid = ROOM + '@conference.meet.example.com';
  await xmpp.send(xml('presence', {to: roomJid + '/testbridge'}, xml('x', {xmlns: 'http://jabber.org/protocol/muc'})));
  console.log('presence enviada a', roomJid + '/testbridge');
  await new Promise(r => setTimeout(r, 5000));
  process.exit(0);
});
xmpp.start().catch(e => { console.error('start error:', e.message); process.exit(1); });
