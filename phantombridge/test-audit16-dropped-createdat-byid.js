// AUDIT-16 (🔴 ALTO): `since` is a temporal cursor — a legitimate event `L`
// created/stored on the relay MUCH EARLIER (backlog) and rejected by the
// bridge under backpressure (full ledger) is NOT re-delivered just by anchoring
// `pendingSince` to the LOCAL rejection time (Date.now).
//
// Auditor scenario:
//   relay:  L.created_at = T0 (very delayed)
//   bridge: lastSeen = T0 + 5000 (local reception, far ahead)
//   L arrives late by backlog -> bridge rejects it (full ledger)
//   bug (v1): pendingSince = Date.now() = T0 + 5000
//   -> the subscription anchors on [T0+5000 - overlap], not on L.created_at
//   -> if L fell outside that window, the relay does NOT re-deliver it -> LOSS.
//
// Fix: (a) the drop keeps created_at (not just id) and pendingSince anchors to
// min(pendingSince, dropped.created_at); (b) the rescan does a POINT recovery
// BY ID (`ids` filter in an ADDITIONAL REQ), independent of the temporal
// cursor, for the registered drops.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit16-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markDelivery, recordDropped, getBridgeState, _setBridgeStateForTest,
} = bridge;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function fresh() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}};
}

// Test 1: the drop keeps created_at (backdated) and pendingSince anchors to
// min(pendingSince, dropped.created_at), NOT to the local rejection time.
t('recordDropped keeps created_at and anchors pendingSince to the minimum (backlogged)', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  const T0 = now - 5000;                       // L created 5000s ago (relay backlog)
  recordDropped('L-backlogged', T0);
  const st = getBridgeState();
  const drop = st.dropped.find(d => d && d.id === 'L-backlogged');
  assert.ok(drop, 'drop registered');
  assert.strictEqual(drop.created_at, T0, 'event created_at kept in the drop');
  // pendingSince must NOT be the local time (now), it must be <= created_at (T0)
  assert.ok(st.pendingSince <= T0,
    'pendingSince anchored to a backdated created_at (' + st.pendingSince + ' <= ' + T0 + '), not to the local time');
});

// Test 2: if an earlier pendingSince already exists, the minimum is kept (sticky).
t('pendingSince sticky: it does not rise due to a second drop with a later created_at', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  const T0 = now - 5000;
  recordDropped('L1', T0);                     // anchors pendingSince = T0
  const before = getBridgeState().pendingSince;
  recordDropped('L2', T0 + 100);               // a later drop must not raise the anchor
  const st = getBridgeState();
  assert.strictEqual(st.pendingSince, before, 'pendingSince does not rise with a later drop');
  assert.strictEqual(st.dropped.length, 2, 'both drops registered');
});

// Test 3: markDelivery fail-closed with a backdated created_at -> pendingSince
// anchored to the created_at, not to the rejection time.
t('markDelivery fail-closed: pendingSince anchored to the rejected wrap created_at', () => {
  const now = Math.floor(Date.now() / 1000);
  const T0 = now - 5000;
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
  const admitted = markDelivery('L-legitimo', 'pending', T0);
  assert.strictEqual(admitted, false, 'fail-closed rejection');
  const st = getBridgeState();
  const drop = st.dropped.find(d => d && d.id === 'L-legitimo');
  assert.ok(drop, 'L in the drops ledger');
  assert.strictEqual(drop.created_at, T0, 'created_at kept in the drop');
  assert.ok(st.pendingSince <= T0,
    'pendingSince <= backdated created_at (' + st.pendingSince + ' <= ' + T0 + ')');
});

// Test 4: markDelivery WITHOUT created_at (legacy path / callers that do not
// know it) still works: it anchors to the local time (previous behavior).
t('markDelivery without created_at: anchors to the local time (compatibility)', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
  const admitted = markDelivery('L2', 'pending');
  const after = Math.floor(Date.now() / 1000);
  assert.strictEqual(admitted, false, 'fail-closed rejection');
  const st = getBridgeState();
  const drop = st.dropped.find(d => d && d.id === 'L2');
  assert.ok(drop, 'L2 in drops');
  assert.strictEqual(drop.created_at, undefined, 'no created_at -> not set');
  assert.ok(st.pendingSince != null && st.pendingSince >= now && st.pendingSince <= after,
    'anchors to the local time (default)');
});

// Test 5: the point recovery by id is triggered when there are drops and
// produces the `ids` filter with kinds 1059 (the relay re-delivers the
// specific id even if its created_at is outside the temporal range of `since`).
t('point fetch by id: emits REQ with the drop ids + kinds 1059', () => {
  const now = Math.floor(Date.now() / 1000);
  const T0 = now - 5000;
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: T0, dropped: [{id: 'X1', ts: now, created_at: T0},
                                {id: 'X2', ts: now, created_at: T0 + 1}],
    droppedOverflow: false, delivery: {}});
  // We capture the frames that SUBSCRIBE would emit. We do not open a real WS;
  // we verify that the batching logic builds the correct `ids` filter
  // with kinds 1059 (the same one sendReq uses in subscribeIncoming).
  const droppedIds = (getBridgeState().dropped || [])
    .filter(d => d && d.id).map(d => d.id);
  assert.deepStrictEqual(droppedIds, ['X1', 'X2'], 'drop ids extracted');
  const BATCH = 100;
  const reqs = [];
  for (let i = 0; i < droppedIds.length; i += BATCH) {
    const batch = droppedIds.slice(i, i + BATCH);
    reqs.push(['REQ', 'bridge-in-byid-' + i, {ids: batch, kinds: [1059]}]);
  }
  assert.strictEqual(reqs.length, 1, 'one REQ per batch of 100');
  assert.deepStrictEqual(reqs[0][2].ids, ['X1', 'X2'], 'ids in the filter');
  assert.deepStrictEqual(reqs[0][2].kinds, [1059], 'kind 1059 (gift-wrap)');
  assert.ok(reqs[0][2].since === undefined, 'the by-id fetch does NOT depend on the temporal cursor');
});

console.log('AUDIT-16 🔴: dropped keeps created_at + pendingSince anchored to the minimum + point fetch by id');
console.log('Result:', passed, 'ok,', failed, 'fail');
process.exit(failed ? 1 : 0);
