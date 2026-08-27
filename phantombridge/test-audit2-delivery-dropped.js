// AUDIT-2 regression test (2nd security audit): bounds the `delivery` ledger
// (MEDIO-2: unbounded growth -> I/O-amplification DoS via per-entry fsync) and
// fixes `DROPPED_MAX` overflow breaking the recovery guarantee (MEDIO-3).
//
// A) delivery ledger:
//    - `delivered` entries expire after DELIVERY_TTL_SECS (lazy eviction).
//    - `pending` entries also expire after the TTL (recoverable by range).
//    - the hard cap DELIVERY_MAX evicts oldest-first (FIFO by ts).
// B) dropped ledger overflow:
//    - dropping > DROPPED_MAX distinct ids sets persistent `droppedOverflow`.
//    - while `droppedOverflow` is set, releasePendingSinceIfRecovered() MUST
//      NOT clear the since-anchor (the evicted ids need range recovery).
//    - once the survivor ledger drains, the flag clears and the anchor MAY
//      release.
//
// We exercise the real bridge functions against an isolated durable state file
// (same pattern as test-persist-audit-alto3.js).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit2-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markDelivery, deliveryStatus, recordDropped, recoverDropped,
  releasePendingSinceIfRecovered, _setBridgeStateForTest, STATE_FILE,
} = bridge;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function resetState(overrides) {
  _setBridgeStateForTest(Object.assign({
    relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {},
  }, overrides || {}));
}

assert.strictEqual(STATE_FILE, tmpState, 'STATE_FILE must point to the temp for the test');

console.log('AUDIT-2: bounded delivery ledger + DROPPED_MAX overflow:');

// ---- A) delivery ledger TTL/cap (option B: watermark for delivered) ----
t('delivered does NOT expire by wall clock (watermark, option B)', () => {
  resetState();
  markDelivery('a-1', 'delivered');
  // Force a very old ts (2h ago) but lastSeen WITHOUT advancing (downtime).
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false,
    delivery: {'a-1': {status: 'delivered', ts: Math.floor(Date.now() / 1000) - (2 * 3600)}}});
  // Any subsequent markDelivery triggers the lazy sweep.
  markDelivery('a-2', 'pending');
  // delivered must NOT expire by clock: lastSeen did not advance beyond its window.
  assert.strictEqual(deliveryStatus('a-1'), 'delivered', 'delivered survives long downtime (watermark)');
  assert.strictEqual(deliveryStatus('a-2'), 'pending', 'a-2 present');
});

t('delivered expires ONLY if the recovery watermark advanced past its window', () => {
  resetState();
  const now = Math.floor(Date.now() / 1000);
  // a-1 delivered 10 min ago, but the RECOVERY WATERMARK (recoveryWatermark)
  // advanced to 5 min ago (the bridge PROCESSED/admitted events after a-1) ->
  // the replay (since) no longer covers it -> it expires. NOTE (Phase 2 /
  // AUDIT-10): expiration does NOT depend on lastSeen (reception cursor), but
  // on the real watermark.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 300, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 300,
    delivery: {'a-1': {status: 'delivered', ts: now - 600}}});
  markDelivery('a-2', 'pending');
  assert.strictEqual(deliveryStatus('a-1'), null, 'delivered expires when the watermark already passed its window');
  assert.strictEqual(deliveryStatus('a-2'), 'pending', 'a-2 present');
});

t('pending expires by wall clock (PENDING_TTL_SECS)', () => {
  resetState();
  // pending from 25h ago (PENDING_TTL_SECS=24h) -> expires.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false,
    delivery: {'p-1': {status: 'pending', ts: Math.floor(Date.now() / 1000) - (25 * 3600)}}});
  markDelivery('p-2', 'delivered');
  assert.strictEqual(deliveryStatus('p-1'), null, 'pending expired (>24h)');
});

t('hard DELIVERY_MAX cap: evicts old pending, NOT immature delivered (fail-closed)', () => {
  resetState();
  const now = Math.floor(Date.now() / 1000);
  // Fill with old pending (evictable) to test the FIFO pending cap.
  const entry = {};
  for (let i = 0; i < 10050; i++) entry['x-' + i] = {status: 'pending', ts: now - 25 * 3600 + i};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false,
    delivery: entry});
  markDelivery('fresh', 'pending');
  const remaining = bridge.getBridgeState().delivery;
  const keys = Object.keys(remaining);
  assert.ok(keys.length <= 10001, 'ledger bounded after cap: ' + keys.length);
  assert.ok(keys.includes('fresh'), 'the new entry survives');
});

// ---- B) DROPPED_MAX overflow ----
t('>DROPPED_MAX drops -> droppedOverflow is set', () => {
  resetState();
  for (let i = 0; i < 5010; i++) recordDropped('d-' + i);
  const st = bridge.getBridgeState();
  assert.strictEqual(st.droppedOverflow, true, 'droppedOverflow active');
  assert.ok(st.dropped.length <= 5000, 'bounded drops ledger: ' + st.dropped.length);
});

t('with droppedOverflow, pendingSince is NOT released even if dropped is empty', () => {
  resetState({pendingSince: 1000, dropped: [], droppedOverflow: true});
  releasePendingSinceIfRecovered();
  assert.strictEqual(bridge.getBridgeState().pendingSince, 1000, 'anchor held on overflow');
});

t('without overflow and empty dropped -> pendingSince is released', () => {
  resetState({pendingSince: 1000, dropped: [], droppedOverflow: false});
  releasePendingSinceIfRecovered();
  assert.strictEqual(bridge.getBridgeState().pendingSince, null, 'anchor released');
});

t('recover all drops -> droppedOverflow clears', () => {
  resetState({pendingSince: 1000, dropped: [{id: 'd-1', ts: 1}, {id: 'd-2', ts: 2}], droppedOverflow: true});
  recoverDropped('d-1');
  assert.strictEqual(bridge.getBridgeState().droppedOverflow, true, 'still in overflow (d-2 remains)');
  recoverDropped('d-2');
  assert.strictEqual(bridge.getBridgeState().droppedOverflow, false, 'overflow cleared when drained');
  releasePendingSinceIfRecovered();
  assert.strictEqual(bridge.getBridgeState().pendingSince, null, 'anchor released after draining');
});

t('droppedOverflow persists to disk (serialization/restore)', () => {
  resetState({pendingSince: 5, dropped: [{id: 'z-1', ts: 1}], droppedOverflow: true});
  if (fs.existsSync(tmpState)) fs.unlinkSync(tmpState);
  markDelivery('keep', 'delivered'); // force durable flush
  const s = JSON.parse(fs.readFileSync(tmpState, 'utf8'));
  assert.strictEqual(s.droppedOverflow, true, 'droppedOverflow in the file');
  // restore like loadState()
  _setBridgeStateForTest({relay: s.relay || 'ws://test.local', lastSeen: s.lastSeen || 0,
    seenIds: s.seenIds || [], pendingSince: s.pendingSince != null ? s.pendingSince : null,
    dropped: s.dropped || [], droppedOverflow: !!s.droppedOverflow,
    delivery: s.delivery || {}});
  assert.strictEqual(bridge.getBridgeState().droppedOverflow, true, 'overflow restored after restart');
});

// cleanup
resetState();
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
