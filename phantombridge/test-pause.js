process.umask(0o077);
// Unit tests of the per-side pause (kill-switch) — no relay, no XMPP.
// Usage: node test-pause.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');
const TEST_NSEC = nip19.nsecEncode(generateSecretKey());
process.env.PHANTOMBRIDGE_TEST_NSEC = TEST_NSEC;
const TEST_DIR = path.join(__dirname, '.test-tmp-pause');
fs.mkdirSync(TEST_DIR, {recursive: true});
fs.writeFileSync(path.join(TEST_DIR, 'config.json'), JSON.stringify({
  mode: 'nostr',
  nick: 'secretario',
  httpPort: 18091,
  nostr: {relay: 'ws://127.0.0.1:19999', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'},
  agents: {roberto: getPublicKey(generateSecretKey()), alma: getPublicKey(generateSecretKey())},
  routing: {permissions: {roberto: ['alma']}, default: 'deny'},
}, null, 2));

process.env.PHANTOMBRIDGE_CONFIG = path.join(TEST_DIR, 'config.json');
const {isPaused, setPaused, PAUSED} = require('./bridge.js');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.error('  FAIL:', name, '—', e.message); }
}

// --- reset between tests ---
function reset() { PAUSED.jitsi = false; PAUSED.nostr = false; }

console.log('test-pause: default state');
t('both sides start NOT paused (config without paused)', () => {
  reset();
  assert.strictEqual(isPaused('jitsi'), false);
  assert.strictEqual(isPaused('nostr'), false);
  assert.strictEqual(isPaused('both'), false);
});

t('PAUSED reflects the state', () => {
  reset();
  assert.deepStrictEqual(PAUSED, {jitsi: false, nostr: false});
});

console.log('test-pause: setPaused independent per side');
t('pausing jitsi does not touch nostr', () => {
  reset();
  const state = setPaused('jitsi', true);
  assert.deepStrictEqual(state, {jitsi: true, nostr: false});
  assert.strictEqual(isPaused('jitsi'), true);
  assert.strictEqual(isPaused('nostr'), false);
  assert.strictEqual(isPaused('both'), true); // both = OR
});

t('pausing nostr does not touch jitsi', () => {
  reset();
  const state = setPaused('nostr', true);
  assert.deepStrictEqual(state, {jitsi: false, nostr: true});
  assert.strictEqual(isPaused('jitsi'), false);
  assert.strictEqual(isPaused('nostr'), true);
});

t('resuming one side does not touch the other', () => {
  reset();
  setPaused('jitsi', true);
  setPaused('nostr', true);
  const state = setPaused('jitsi', false);
  assert.deepStrictEqual(state, {jitsi: false, nostr: true});
  assert.strictEqual(isPaused('jitsi'), false);
  assert.strictEqual(isPaused('nostr'), true);
});

console.log('test-pause: setPaused both');
t('both pauses both sides', () => {
  reset();
  const state = setPaused('both', true);
  assert.deepStrictEqual(state, {jitsi: true, nostr: true});
  assert.strictEqual(isPaused('both'), true);
});

t('both resumes both sides', () => {
  reset();
  setPaused('both', true);
  const state = setPaused('both', false);
  assert.deepStrictEqual(state, {jitsi: false, nostr: false});
  assert.strictEqual(isPaused('both'), false);
});

console.log('test-pause: validation');
t('invalid side throws error', () => {
  reset();
  assert.throws(() => setPaused('xmpp', true), /invalid side/);
  assert.throws(() => setPaused('', true), /invalid side/);
});

t('non-boolean values are coerced', () => {
  reset();
  setPaused('nostr', 1);
  assert.strictEqual(isPaused('nostr'), true);
  setPaused('nostr', 0);
  assert.strictEqual(isPaused('nostr'), false);
});

console.log('\nResult: ' + passed + ' ok, ' + failed + ' fail');
fs.rmSync(TEST_DIR, {recursive: true, force: true});
process.exit(failed ? 1 : 0);
