process.umask(0o077);
// Tests unitarios del routing DM↔DM (modo nostr) — sin relay, sin XMPP.
// Uso: node test-routing.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');

// The bridge.js module reads the config at require(). We create a test config
// con nsec real para que nip19.decode no falle (modo nostr, sin XMPP).
const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');
const TEST_NSEC = nip19.nsecEncode(generateSecretKey());
const TEST_DIR = path.join(__dirname, '.test-tmp');
fs.mkdirSync(TEST_DIR, {recursive: true});
fs.writeFileSync(path.join(TEST_DIR, 'config.json'), JSON.stringify({
  mode: 'nostr',
  nick: 'secretario',
  httpPort: 18090,
  nostr: {relay: 'ws://127.0.0.1:19999', nsec: TEST_NSEC},
  agents: {
    roberto: getPublicKey(generateSecretKey()),
    alma: getPublicKey(generateSecretKey()),
    paco: getPublicKey(generateSecretKey()),
    pepa: getPublicKey(generateSecretKey()),
  },
  routing: {
    permissions: {
      roberto: ['alma', 'paco', 'pepa'],
      alma: ['roberto'],
      paco: ['*'],
    },
    default: 'deny',
  },
}, null, 2));

// Load the module pointing to the test config (argv[2] or PHANTOMBRIDGE_CONFIG)
process.env.PHANTOMBRIDGE_CONFIG = path.join(TEST_DIR, 'config.json');
const {parseRouteTarget, routingAllowed} = require('./bridge.js');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.error('  FAIL:', name, '-', e.message); }
}

// Permisos de prueba: roberto puede hablar con alma/paco/pepa; alma solo con roberto
const PERMS = {
  roberto: ['alma', 'paco', 'pepa'],
  alma: ['roberto'],
  paco: ['*'],
};

console.log('parseRouteTarget:');
t('"@alma texto" -> {to: alma, text: texto}', () => {
  const r = parseRouteTarget('@alma REQUEST aquaponics-united-20260811-0003');
  assert.strictEqual(r.to, 'alma');
  assert.strictEqual(r.text, 'REQUEST aquaponics-united-20260811-0003');
});
t('"@pepa hola" -> to pepa', () => {
  const r = parseRouteTarget('@pepa hola mundo');
  assert.strictEqual(r.to, 'pepa');
  assert.strictEqual(r.text, 'hola mundo');
});
t('"texto sin @ no es ruta"', () => {
  assert.strictEqual(parseRouteTarget('hola mundo'), null);
});
t('"@alma" sin texto no es ruta', () => {
  assert.strictEqual(parseRouteTarget('@alma'), null);
});
t('"@ALMA texto" case-insensitive', () => {
  const r = parseRouteTarget('@ALMA texto');
  assert.strictEqual(r.to, 'alma');
});
t('"@alma texto con [sala] dentro"', () => {
  const r = parseRouteTarget('@alma [sala] hola');
  assert.strictEqual(r.to, 'alma');
  assert.strictEqual(r.text, '[sala] hola');
});

console.log('routingAllowed:');
t('roberto -> alma permitido', () => {
  assert.strictEqual(routingAllowed('roberto', 'alma', PERMS, 'deny'), true);
});
t('roberto -> elena NO permitido (sin regla, default deny)', () => {
  assert.strictEqual(routingAllowed('roberto', 'elena', PERMS, 'deny'), false);
});
t('alma -> roberto permitido', () => {
  assert.strictEqual(routingAllowed('alma', 'roberto', PERMS, 'deny'), true);
});
t('alma -> paco NO permitido', () => {
  assert.strictEqual(routingAllowed('alma', 'paco', PERMS, 'deny'), false);
});
t('paco -> cualquiera permitido (wildcard *)', () => {
  assert.strictEqual(routingAllowed('paco', 'elena', PERMS, 'deny'), true);
});
t('self -> self siempre denegado', () => {
  assert.strictEqual(routingAllowed('roberto', 'roberto', PERMS, 'allow'), false);
});
t('sin regla + default allow -> permitido', () => {
  assert.strictEqual(routingAllowed('elena', 'alma', PERMS, 'allow'), true);
});
t('empty permissions + default deny -> denied', () => {
  assert.strictEqual(routingAllowed('roberto', 'alma', {}, 'deny'), false);
});

console.log(`\n${passed} passed, ${failed} failed`);
fs.rmSync(TEST_DIR, {recursive: true, force: true});
process.exit(failed ? 1 : 0);
