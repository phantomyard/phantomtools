// Regression test for #82: an unauthenticated envelope's rid must not steer
// the request_id short-circuit. Only an AUTHENTICATED envelope's rid is
// authoritative.
'use strict';
process.umask(0o077);
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');

const TEST_NSEC = nip19.nsecEncode(generateSecretKey());
process.env.PHANTOMBRIDGE_TEST_NSEC = TEST_NSEC;
const TEST_DIR = path.join(__dirname, '.test-tmp-rid');
fs.mkdirSync(TEST_DIR, {recursive: true});
fs.writeFileSync(path.join(TEST_DIR, 'config.json'), JSON.stringify({
  mode: 'nostr', nick: 'bridge-test', httpPort: 0,
  nostr: {relay: 'ws://127.0.0.1:19999', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'},
  agents: {alice: getPublicKey(generateSecretKey()), bob: getPublicKey(generateSecretKey())},
  routing: {permissions: {alice: ['bob'], bob: ['alice']}, default: 'deny'},
}, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = path.join(TEST_DIR, 'config.json');

const {resolveRid} = require('./bridge.js');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.error('  FAIL:', name, '-', e.message); }
}

console.log('resolveRid (#82):');
t('unauthenticated envelope rid is ignored (returns null)', () => {
  assert.strictEqual(resolveRid({env: {rid: 'forged-rid'}, authenticated: false}, 'text'), null);
});
t('authenticated envelope rid is authoritative', () => {
  assert.strictEqual(resolveRid({env: {rid: 'real-rid'}, authenticated: true}, 'text'), 'real-rid');
});
t('unauthenticated envelope without rid returns null', () => {
  assert.strictEqual(resolveRid({env: {}, authenticated: false}, 'text'), null);
});
t('no-envelope text fallback does not throw', () => {
  const rid = resolveRid(null, 'REQUEST example-org-20260101-0001 hello');
  assert.ok(rid === null || typeof rid === 'string');
});

console.log(`\nrid auth regression: ${passed} passed, ${failed} failed`);
fs.rmSync(TEST_DIR, {recursive: true, force: true});
process.exit(failed ? 1 : 0);
