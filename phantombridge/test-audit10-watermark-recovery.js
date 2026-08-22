// AUDIT-10 (HIGH, root): cursor separation — raw reception vs recovery
// watermark.
//
// AUDIT-7 came from mixing two incompatible semantics in lastSeen:
//   - "last RECEIVED event" (advances with every frame, even rejected/
//     non-admitted/discarded) — used by the subscription `since`.
//   - "watermark proving earlier events can no longer reappear" — used by
//     deliveredCanExpire() to delete delivered.
//
// With Phase 2, deliveredCanExpire() must NOT depend on lastSeen (raw
// reception) but on recoveryWatermark, which ONLY advances when an event is
// really PROCESSED/ADMITTED (successful markDelivery). A received but
// rejected/non-admitted frame NEVER moves recoveryWatermark, no matter how
// much lastSeen advances.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit10-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  updateLastSeen, markDelivery, deliveryStatus, advanceRecoveryWatermark,
  getBridgeState, _setBridgeStateForTest,
} = bridge;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function freshState() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, delivery: {}};
}

console.log('AUDIT-10: receiveCursor (lastSeen) vs recoveryWatermark separation:');

// Test 1: raw reception (updateLastSeen) advances lastSeen BUT NOT recoveryWatermark.
t('updateLastSeen (reception) does NOT advance the recovery watermark', () => {
  _setBridgeStateForTest(freshState());
  const before = bridge.recoveryWatermark;
  // Simulates a burst of received frames (authorized or not): each frame
  // calls updateLastSeen(0) before authenticating/admitting (exactly the
  // handler pattern). lastSeen advances, recoveryWatermark must not.
  updateLastSeen(0);
  updateLastSeen(0);
  updateLastSeen(0);
  const st = getBridgeState();
  assert.ok(st.lastSeen > 0, 'lastSeen (receiveCursor) advanced with reception');
  assert.strictEqual(st.recoveryWatermark, before,
    'recoveryWatermark does NOT advance via raw reception (only via real processing)');
});

// Test 2: the delivered is NEVER deleted due to lastSeen inflated by non-admitted.
// AUDIT-7 scenario: a burst of non-admitted advances lastSeen far ahead of a
// delivered X; with the old semantics, deliveredCanExpire(X, lastSeen) deleted
// it (break exactly-once). With Phase 2, deliveredCanExpire uses
// recoveryWatermark which did NOT advance -> X is preserved.
t('delivered survives lastSeen inflated by non-admitted (watermark intact)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 60; // delivered delivered 1 min ago
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0,
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // Burst of non-admitted: lastSeen advances MORE than 240s (+120+120 margin)
  // ahead of X (the relay returns traffic the bridge does not admit), but
  // recoveryWatermark stays at 0 (nothing processed yet).
  const st = getBridgeState();
  st.lastSeen = tX + 600; // raw reception far ahead of X
  // Any later markDelivery triggers evictDeliveryLedger.
  markDelivery('Y', 'pending');
  // X must NOT expire: the recovery watermark did not advance.
  assert.strictEqual(deliveryStatus('X'), 'delivered',
    'delivered X is preserved even though lastSeen advanced (only the REAL watermark expires it)');
});

// Test 3: ONLY real confirmed processing (event/range the relay has walked,
// via advanceRecoveryWatermark) advances the watermark, and it is then (and
// only then) that an old delivered can expire. The advance is INCREMENTAL and
// BOUNDED (never a free jump to Date.now()): a real event confirms at most one
// backlog step (RECOVERY_WATERMARK_STEP_SECS). After downtime, the watermark
// progresses little by little with the backlog burst.
t('advanceRecoveryWatermark (confirmed event) advances the watermark -> old delivered expires', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 600; // delivered delivered 10 min ago
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 600, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    recoveryWatermark: tX - 1, // watermark just before X (X still unreachable after 1 step)
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // One REAL processed event advances the watermark one step. With a 300s step
  // and tX 599s behind the previous watermark+step, X stays inside the overlap
  // (does not expire yet with just 1 event — correct: the relay has not walked
  // further).
  const wmBefore = bridge.recoveryWatermark;
  advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark > wmBefore, 'watermark advances with the confirmed event');
  // We process enough events (burst) for the watermark to surpass X+overlap.
  for (let i = 0; i < 5; i++) advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark >= tX + 120 + 120,
    'after the burst the watermark surpasses X\'s window');
  assert.ok(markDelivery('nuevo-1', 'pending'), 'admission successful');
  assert.strictEqual(deliveryStatus('X'), null,
    'old delivered expires ONLY when the recovery watermark surpasses it');
});

// Test 4: markDelivery by itself (internal admission) does NOT advance the
// watermark if there was no previous watermark — after downtime the relay has
// not confirmed walking the range, and an internal admission must not expire
// legitimate delivered (break exactly-once).
t('markDelivery without a prior watermark does NOT expire delivered (downtime)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 3600; // delivered delivered 1h ago
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: tX, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, // never confirmed processed
    delivery: {'X': {status: 'delivered', ts: tX}}});
  markDelivery('Y', 'pending');
  assert.strictEqual(deliveryStatus('X'), 'delivered',
    'delivered survives: the watermark stays at 0 (no confirmed event) after downtime');
});

// Test 5: advanceRecoveryWatermark is a helper that advances the watermark in
// an INCREMENTAL BOUNDED way. It does not give a free jump to Date.now() after
// downtime (that would break exactly-once), but it can establish the first
// watermark from 0 with a small step (conservative cold start).
t('advanceRecoveryWatermark: bounded incremental advance, never a free jump to now', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  assert.strictEqual(bridge.recoveryWatermark, 0, 'starts at 0');
  // From 0, a single event establishes a BOUNDED watermark (step), not now.
  advanceRecoveryWatermark();
  const w0 = bridge.recoveryWatermark;
  assert.ok(w0 > 0, 'establishes a watermark from 0 (bounded startup step)');
  assert.ok(w0 < now, 'the first establishment does NOT jump to now (bounded to the step)');
  // Each later event adds at most one step; never exceeds the local clock.
  const step = 300; // RECOVERY_WATERMARK_STEP_SECS default
  for (let i = 0; i < 200; i++) advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark <= now, 'monotonic and never exceeds now');
  // An isolated event after downtime (prev far behind now) advances ONLY one step.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 30 * 24 * 3600, // 30 days ago
    delivery: {}});
  const prev = bridge.recoveryWatermark;
  advanceRecoveryWatermark(); // a single processed event
  assert.strictEqual(bridge.recoveryWatermark, prev + step,
    'an event after 30 days of downtime advances EXACTLY one step, not to now');
});

// cleanup
_setBridgeStateForTest(freshState());
bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
