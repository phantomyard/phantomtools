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

// Test permissions: carol can talk to dave/alice/bob; dave only to carol
const PERMS = {
  carol: ['dave', 'alice', 'bob'],
  dave: ['carol'],
  alice: ['*'],
};

console.log('parseRouteTarget:');
t('"@dave text" -> {to: dave, text: text}', () => {
  const r = parseRouteTarget('@dave REQUEST example-org-20250101-0003');
  assert.strictEqual(r.to, 'dave');
  assert.strictEqual(r.text, 'REQUEST example-org-20250101-0003');
});
t('"@bob hello" -> to bob', () => {
  const r = parseRouteTarget('@bob hola mundo');
  assert.strictEqual(r.to, 'bob');
  assert.strictEqual(r.text, 'hola mundo');
});
t('"text without @ is not a route"', () => {
  assert.strictEqual(parseRouteTarget('hola mundo'), null);
});
t('"@dave" without text is not a route', () => {
  assert.strictEqual(parseRouteTarget('@dave'), null);
});
t('"@DAVE text" case-insensitive', () => {
  const r = parseRouteTarget('@DAVE texto');
  assert.strictEqual(r.to, 'dave');
});
t('"@dave text with [room] inside"', () => {
  const r = parseRouteTarget('@dave [sala] hola');
  assert.strictEqual(r.to, 'dave');
  assert.strictEqual(r.text, '[sala] hola');
});

console.log('routingAllowed:');
t('carol -> dave allowed', () => {
  assert.strictEqual(routingAllowed('carol', 'dave', PERMS, 'deny'), true);
});
t('carol -> erin NOT allowed (no rule, default deny)', () => {
  assert.strictEqual(routingAllowed('carol', 'erin', PERMS, 'deny'), false);
});
t('dave -> carol allowed', () => {
  assert.strictEqual(routingAllowed('dave', 'carol', PERMS, 'deny'), true);
});
t('dave -> alice NOT allowed', () => {
  assert.strictEqual(routingAllowed('dave', 'alice', PERMS, 'deny'), false);
});
t('alice -> anyone allowed (wildcard *)', () => {
  assert.strictEqual(routingAllowed('alice', 'erin', PERMS, 'deny'), true);
});
t('self -> self always denied', () => {
  assert.strictEqual(routingAllowed('carol', 'carol', PERMS, 'allow'), false);
});
t('no rule + default allow -> allowed', () => {
  assert.strictEqual(routingAllowed('erin', 'dave', PERMS, 'allow'), true);
});
t('empty permissions + default deny -> denied', () => {
  assert.strictEqual(routingAllowed('carol', 'dave', {}, 'deny'), false);
});
t('string rule (not array) fails closed — no substring bypass (#80)', () => {
  assert.strictEqual(routingAllowed('carol', 'dave', {carol: 'dave'}, 'deny'), false);
  assert.strictEqual(routingAllowed('carol', 'davey', {carol: 'dave'}, 'deny'), false);
  assert.strictEqual(routingAllowed('carol', 'alice', {carol: 'dave,alice'}, 'deny'), false);
});
t('numeric rule fails closed without throwing (#80)', () => {
  assert.strictEqual(routingAllowed('carol', 'dave', {carol: 42}, 'deny'), false);
});

console.log(`\n${passed} passed, ${failed} failed`);
fs.rmSync(TEST_DIR, {recursive: true, force: true});
process.exit(failed ? 1 : 0);
