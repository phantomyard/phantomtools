// Enviar mensaje groupchat a una sala (simula participante humano)
const tls = require('tls');
const origTlsConnect = tls.connect;
tls.connect = function (...args) {
  if (args[0] && typeof args[0] === 'object') args[0] = { ...args[0], rejectUnauthorized: false };
  return origTlsConnect.apply(this, args);
};
const {client, xml} = require('@xmpp/client');

const ROOM = process.argv[2] || 'sala-puente-test';
const MSG = process.argv[3] || 'Hola desde el test';
const PASSWORD = process.env.TEST_PW;

const xmpp = client({
  service: 'xmpp://127.0.0.1:5222',
  domain: 'auth.meet.example.com',
  username: 'testbridge',
  password: PASSWORD,
});

xmpp.on('error', e => { console.error('xmpp error:', e.message); process.exit(1); });
xmpp.on('online', async () => {
  const roomJid = ROOM + '@conference.meet.example.com';
  // join con child MUC
  await xmpp.send(xml('presence', {to: roomJid + '/testbridge'}, xml('x', {xmlns: 'http://jabber.org/protocol/muc'})));
  await new Promise(r => setTimeout(r, 1500));
  await xmpp.send(xml('message', {to: roomJid, type: 'groupchat'}, xml('body', {}, MSG)));
  console.log('mensaje enviado a', roomJid, ':', MSG);
  await new Promise(r => setTimeout(r, 3000));
  process.exit(0);
});
xmpp.start().catch(e => { console.error('start error:', e.message); process.exit(1); });
