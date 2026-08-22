#!/usr/bin/env node
// LOW-8 regression test (audit 462e62b): rejected frames (e.g. too large)
// must be marked in a SEPARATE cache (rejectedIds), NOT in seenIds (dedup of
// PROCESSABLE events). The previous bug: subscribeIncoming marked big[2].id
// with markSeen() before validating JSON/signature/kind, allowing an attacker
// to inject up to ~200 arbitrary IDs per giant frame and degrade the
// legitimate seenIds dedup.
//
// This test exercises the REAL functions exported from the bridge
// (markRejected / isRejected / rejectedIds), without booting a relay.
const assert = require('assert');
require('./testlib.js').setup();
const bridge = require('./bridge.js');
const {markRejected, isRejected, rejectedIds, markSeen, isSeen, getBridgeState, _setBridgeStateForTest} = bridge;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.error('  FAIL:', name, '—', e.message); }
}
function resetRejected() { rejectedIds.length = 0; }

// Seed state for markSeen (needs a bridgeState with seenIds).
_setBridgeStateForTest({
  relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: [],
});

// 1. markRejected registers and isRejected sees it.
t('markRejected -> isRejected true', () => {
  resetRejected();
  markRejected('id-rechazado-1');
  assert.strictEqual(isRejected('id-rechazado-1'), true);
});

// 2. Does not contaminate seenIds (dedup of processable events).
t('markRejected does NOT mark seenIds (dedup intact)', () => {
  resetRejected();
  markRejected('id-gigante');
  assert.strictEqual(isRejected('id-gigante'), true);
  // The id is NOT in seenIds: a distinct legitimate event is not affected.
  assert.strictEqual(getBridgeState().seenIds.some(e => e.id === 'id-gigante'), false);
  assert.strictEqual(isSeen('id-gigante'), false);
});

// 3. Own cap: >200 rejected truncates the cache to 200.
t('REJECTED_IDS_MAX (200) cap applies', () => {
  resetRejected();
  for (let i = 0; i < 250; i++) markRejected('rej-' + i);
  assert.ok(rejectedIds.length <= 200, 'rejectedIds.length=' + rejectedIds.length);
  // the most recent are kept, the oldest are evicted
  assert.strictEqual(isRejected('rej-249'), true);
});

// 4. Idempotent: marking the same id twice does not duplicate entries.
t('markRejected idempotent (no duplicates)', () => {
  resetRejected();
  markRejected('dup');
  markRejected('dup');
  markRejected('dup');
  assert.strictEqual(rejectedIds.filter(e => e.id === 'dup').length, 1);
});

// 5. TTL: an entry with a ts out of window does not count as rejected.
t('TTL: old entry is not rejected', () => {
  resetRejected();
  markRejected('viejo');
  // manually age the entry beyond the window
  const now = Math.floor(Date.now() / 1000);
  const entry = rejectedIds.find(e => e.id === 'viejo');
  entry.ts = now - (130 + 3600); // well beyond STATE_OVERLAP_SECS+60
  assert.strictEqual(isRejected('viejo'), false);
});

// 6. No id -> no-op, does not throw.
t('markRejected(undefined) / (null) no-op', () => {
  resetRejected();
  markRejected(undefined);
  markRejected(null);
  assert.strictEqual(rejectedIds.length, 0);
});

// 7. The cache accepts arbitrary ids but with a cap: an attacker cannot grow
//    rejectedIds without limit (LOW-8: bounded degradation).
t('flood of arbitrary ids stays bounded by the cap', () => {
  resetRejected();
  for (let i = 0; i < 500; i++) markRejected('evil-' + i);
  assert.ok(rejectedIds.length <= 200, 'length=' + rejectedIds.length);
});

// 8. markSeen still works for legitimate events (without breaking ALTO-3).
t('markSeen still records normal seen (ALTO-3 regression)', () => {
  markSeen('legitimo-1');
  assert.strictEqual(isSeen('legitimo-1'), true);
});

console.log('\nResult: ' + passed + ' ok, ' + failed + ' fail');
process.exit(failed ? 1 : 0);
