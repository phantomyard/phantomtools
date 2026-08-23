process.umask(0o077);
// Unit tests for DM↔DM routing (nostr mode) — no relay, no XMPP.
// Usage: node test-routing.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');

// The bridge.js module reads the config at require(). We create a test config
// with a real nsec so nip19.decode does not fail (nostr mode, no XMPP).
const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');
const TEST_NSEC = nip19.nsecEncode(generateSecretKey());
process.env.PHANTOMBRIDGE_TEST_NSEC = TEST_NSEC;
const TEST_DIR = path.join(__dirname, '.test-tmp');
fs.mkdirSync(TEST_DIR, {recursive: true});
fs.writeFileSync(path.join(TEST_DIR, 'config.json'), JSON.stringify({
  mode: 'nostr',
  nick: 'secretario',
  httpPort: 18090,
  nostr: {relay: 'ws://127.0.0.1:19999', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'},
  agents: {
    carol: getPublicKey(generateSecretKey()),
    dave: getPublicKey(generateSecretKey()),
    alice: getPublicKey(generateSecretKey()),
    bob: getPublicKey(generateSecretKey()),
  },
  routing: {
    permissions: {
      carol: ['dave', 'alice', 'bob'],
      dave: ['carol'],
      alice: ['*'],
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

// Permisos de prueba: carol puede hablar con dave/alice/bob; dave solo con carol
const PERMS = {
  carol: ['dave', 'alice', 'bob'],
  dave: ['carol'],
  alice: ['*'],
};

console.log('parseRouteTarget:');
t('"@dave texto" -> {to: dave, text: texto}', () => {
  const r = parseRouteTarget('@dave REQUEST example-org-20250101-0003');
  assert.strictEqual(r.to, 'dave');
  assert.strictEqual(r.text, 'REQUEST example-org-20250101-0003');
});
t('"@bob hola" -> to bob', () => {
  const r = parseRouteTarget('@bob hola mundo');
  assert.strictEqual(r.to, 'bob');
  assert.strictEqual(r.text, 'hola mundo');
});
t('"texto sin @ no es ruta"', () => {
  assert.strictEqual(parseRouteTarget('hola mundo'), null);
});
t('"@dave" sin texto no es ruta', () => {
  assert.strictEqual(parseRouteTarget('@dave'), null);
});
t('"@DAVE texto" case-insensitive', () => {
  const r = parseRouteTarget('@DAVE texto');
  assert.strictEqual(r.to, 'dave');
});
t('"@dave texto con [sala] dentro"', () => {
  const r = parseRouteTarget('@dave [sala] hola');
  assert.strictEqual(r.to, 'dave');
  assert.strictEqual(r.text, '[sala] hola');
});

console.log('routingAllowed:');
t('carol -> dave permitido', () => {
  assert.strictEqual(routingAllowed('carol', 'dave', PERMS, 'deny'), true);
});
t('carol -> erin NO permitido (sin regla, default deny)', () => {
  assert.strictEqual(routingAllowed('carol', 'erin', PERMS, 'deny'), false);
});
t('dave -> carol permitido', () => {
  assert.strictEqual(routingAllowed('dave', 'carol', PERMS, 'deny'), true);
});
t('dave -> alice NO permitido', () => {
  assert.strictEqual(routingAllowed('dave', 'alice', PERMS, 'deny'), false);
});
t('alice -> cualquiera permitido (wildcard *)', () => {
  assert.strictEqual(routingAllowed('alice', 'erin', PERMS, 'deny'), true);
});
t('self -> self siempre denegado', () => {
  assert.strictEqual(routingAllowed('carol', 'carol', PERMS, 'allow'), false);
});
t('sin regla + default allow -> permitido', () => {
  assert.strictEqual(routingAllowed('erin', 'dave', PERMS, 'allow'), true);
});
t('empty permissions + default deny -> denied', () => {
  assert.strictEqual(routingAllowed('carol', 'dave', {}, 'deny'), false);
});

console.log(`\n${passed} passed, ${failed} failed`);
fs.rmSync(TEST_DIR, {recursive: true, force: true});
process.exit(failed ? 1 : 0);
