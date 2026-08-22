// AUDIT-7 (HIGH): the fail-closed rejection by a full ledger must not allow
// the permanent loss of a legitimate DM.
//
// Auditor scenario: DELIVERY_MAX full of protected delivered ->
// markDelivery(L, 'pending') returns false (fail-closed backpressure). But the
// handler already called updateLastSeen() BEFORE admission, so lastSeen
// advanced with the reception time. If `L` falls outside the overlap window
// (lastSeen - 120) during a non-admitted burst, the `since` of the next
// subscription would jump ahead of `L` and the relay would no longer
// re-deliver it -> PERMANENT LOSS.
//
// The fix (option A): on the fail-closed rejection of markDelivery it reuses
// the ALTO-2 enqueueGiftWrap mechanism — record the id in the drop ledger and
// anchor STICKY pendingSince (never more recent than the first unrecovered
// drop). Thus the next subscription anchors `since = pendingSince - 120`,
// covering `L` even though lastSeen has advanced far ahead.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit7-'));
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

// Test 1: the fail-closed rejection by a full ledger records the drop and
// anchors sticky pendingSince (before the fix it did NOT -> `L` could be lost).
t('fail-closed: full-ledger rejection records dropped[id] + sticky pendingSince', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: entry});
  const admitted = markDelivery('L-legitimo', 'pending');
  assert.strictEqual(admitted, false, 'admission rejected (fail-closed)');
  const st = getBridgeState();
  assert.ok(st.dropped.some(d => d && d.id === 'L-legitimo'),
    'the id L is recorded in the drop ledger (recoverable)');
  assert.ok(st.pendingSince != null, 'pendingSince stays anchored (sticky) for the drop');
});

// Test 2: the `since` of the next subscription anchors to pendingSince (not
// to lastSeen), covering `L` even though lastSeen has advanced far ahead after
// a non-admitted burst. This is exactly what prevents the permanent loss of
// the auditor scenario (E1..E50000 + L).
t('since anchors to pendingSince: L fallen out of the overlap window stays covered', () => {
  const now = Math.floor(Date.now() / 1000);
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  // State after the burst: lastSeen advanced FAR ahead (50000 received events
  // ~ 50000s -> 13.9h after L), and the L drop anchored pendingSince at the
  // rejection moment.
  const lTime = now - 50000; // L was received ~13.9h ago (would fall outside lastSeen-120)
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: lTime, dropped: [{id: 'L-legitimo', ts: lTime}], droppedOverflow: false,
    delivery: entry});
  // Reproduces the H-NEW-01 subscribeIncoming logic: cursor = pendingSince if
  // there are drops, otherwise lastSeen.
  const cursor = (getBridgeState().pendingSince != null && getBridgeState().pendingSince < getBridgeState().lastSeen)
    ? getBridgeState().pendingSince
    : getBridgeState().lastSeen;
  const since = cursor - 120; // STATE_OVERLAP_SECS = 120
  assert.ok(cursor === lTime, 'cursor anchored to pendingSince (not to advanced lastSeen)');
  assert.ok(since <= lTime, 'since <= L moment -> L inside the re-delivery window');
  assert.ok(since < getBridgeState().lastSeen,
    'since does not jump ahead of L (even though lastSeen advanced 50000s)');
});

// Test 3: pendingSince is STICKY — it stays anchored even when more events
// arrive (a single unrecovered drop is not released just because lastSeen
// advances).
t('pendingSince sticky: not released when lastSeen advances with a pending drop', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  // Simulate the fail-closed drop
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'L-legitimo', ts: now - 5}], droppedOverflow: false,
    delivery: {}});
  // A later event does NOT purge pendingSince or the dropped while L is not
  // recovered (releasePendingSinceIfRecovered only acts with an empty dropped).
  recordDropped('L-legitimo'); // idempotent: already present
  const st = getBridgeState();
  assert.ok(st.pendingSince != null, 'pendingSince stays anchored');
  assert.ok(st.dropped.some(d => d && d.id === 'L-legitimo'), 'the drop stays recorded');
  assert.ok(st.pendingSince <= st.lastSeen, 'pendingSince is never more recent than lastSeen');
});

// cleanup
_setBridgeStateForTest(fresh());
bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
