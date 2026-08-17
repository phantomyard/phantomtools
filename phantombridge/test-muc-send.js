// Test Fase A: enviar mensaje a la MUC como testbridge y verificar reflejo del puente
// Uso: node test-muc-send.js <room> <text>
const tls = require('tls');
const origTlsConnect = tls.connect;
tls.connect = function (...args) {
  if (args[0] && typeof args[0] === 'object') args[0] = { ...args[0], rejectUnauthorized: false };
  return origTlsConnect.apply(this, args);
};
const {client, xml} = require('@xmpp/client');

const ROOM = process.argv[2] || 'sala-puente-test';
const TEXT = process.argv[3] || 'Hola desde testbridge (Fase A del puente)';
const PASSWORD = process.env.TEST_PW;

const xmpp = client({
  service: 'xmpp://127.0.0.1:5222',
  domain: 'auth.meet.example.com',
  username: 'testbridge',
  password: PASSWORD,
});

xmpp.on('error', e => { console.error('xmpp error:', e.message); process.exit(1); });
xmpp.on('online', async () => {
  console.log('online como testbridge');
  // unirse a la sala
  const roomJid = ROOM + '@conference.meet.example.com';
  await xmpp.send(xml('presence', {to: roomJid + '/testbridge'}));
  console.log('unido a', roomJid);
  await new Promise(r => setTimeout(r, 2000));
  await xmpp.send(xml('message', {to: roomJid, type: 'groupchat'}, xml('body', {}, TEXT)));
  console.log('mensaje enviado:', TEXT);
  await new Promise(r => setTimeout(r, 2000));
  process.exit(0);
});
xmpp.start().catch(e => { console.error('start error:', e.message); process.exit(1); });
