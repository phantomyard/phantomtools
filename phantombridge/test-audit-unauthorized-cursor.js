// AUDIT-4/5 review point (🟡): can a backlog of UNAUTHORIZED events advance
// `lastSeen` ahead of legitimate events and cause LOSS after restart?
//
// Sequence the auditor wants to test:
//   unauthorized backlog + authorized event + queue overflow + restart
//
// Real flow (handleIncomingGiftWrap):
//   isSeen/deliveryStatus -> updateLastSeen() -> markSeen() -> NIP-17 auth
//   -> allowlist -> (only authorized) markDelivery(pending) -> execute.
//
// The theoretical risk: `updateLastSeen()` advances `lastSeen` (real clock)
// for EVERY received event, including unauthorized ones. If a flood of
// unauthorized events fills the queue and `lastSeen` advances, and a later
// legitimate one is dropped by overflow -> is it lost? The protection
// mechanism is `pendingSince` + `recordDropped`: when dropping by overflow it
// anchors `pendingSince` and records the drop; `since = pendingSince - 120`
// in the next subscription never passes that point -> the legitimate event is
// re-delivered.
//
// This test verifies that THAT mechanism works: the recovery cursor after
// restart NEVER advances past `pendingSince`, so an event dropped by overflow
// (even after/under a flood of unauthorized events that advanced lastSeen)
// remains reachable.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit-unauth-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  recordDropped, releasePendingSinceIfRecovered, recoverDropped,
  updateLastSeen, _setBridgeStateForTest, getBridgeState, STATE_FILE,
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

// Reproduces the subscription cursor logic in subscribeIncoming() (AUDIT-14/15).
// The processing cursor is recoveryWatermark (the range confirmed as walked
// when admitting/processing), NOT lastSeen (local reception). pendingSince
// (STICKY) is the conservative anchor if there are pending drops.
function subscriptionSince(state) {
  if (!state || !state.relay) return null; // full backlog
  const recovery = state.recoveryWatermark || 0;
  let cursor = recovery;
  if (state.pendingSince != null) {
    cursor = (cursor === 0 || state.pendingSince < cursor)
      ? state.pendingSince : cursor;
  }
  if (cursor <= 0) return null; // nothing processed yet -> full backlog
  return cursor - 120;
}

assert.strictEqual(STATE_FILE, tmpState, 'STATE_FILE temp, as in the other tests');

console.log('AUDIT-4/5 🟡: lastSeen vs unauthorized backlog (loss after restart?):');

// 1. Flood of unauthorized events advances lastSeen (real clock).
t('flood of unauthorized events advances lastSeen (by design)', () => {
  _setBridgeStateForTest(fresh());
  // Simulate receiving many events (authorized or not): updateLastSeen
  // advances lastSeen to the real clock on each event.
  updateLastSeen(0);
  updateLastSeen(0);
  // lastSeen advanced above 0.
  assert.ok(getBridgeState().lastSeen > 0, 'lastSeen advanced after receiving events');
});

// 2. Overflow: a legitimate event is dropped -> drop + pendingSince are recorded.
t('queue overflow anchors pendingSince + records the drop (not lost)', () => {
  _setBridgeStateForTest(fresh());
  const t0 = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: t0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}});
  // The legitimate one is dropped by overflow: recordDropped + pendingSince anchored.
  // (Simulates exactly what enqueueGiftWrap does when the queue is full.)
  recordDropped('legit-1');
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: t0, seenIds: [], pendingSince: t0,
    dropped: getBridgeState().dropped, droppedOverflow: false, delivery: {}});
  // The next subscription cursor anchors to pendingSince, NOT to lastSeen.
  const since = subscriptionSince(getBridgeState());
  assert.ok(since <= t0 - 120 + 120, 'since anchored to pendingSince (does not pass the drop)');
  assert.ok(getBridgeState().pendingSince != null, 'pendingSince active');
  assert.ok(getBridgeState().dropped.some(d => d.id === 'legit-1'), 'drop recorded');
});

// 3. After restart: the cursor NEVER passes pendingSince while there are drops.
t('restart: cursor does not advance past the pending drop (no loss)', () => {
  _setBridgeStateForTest(fresh());
  const t0 = Math.floor(Date.now() / 1000);
  // Persisted state: lastSeen advanced a LOT due to the unauthorized flood,
  // but pendingSince is anchored to the drop point of the legitimate one.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: t0 + 5000, seenIds: [], pendingSince: t0,
    dropped: [{id: 'legit-2', ts: t0}], droppedOverflow: false, delivery: {}});
  const since = subscriptionSince(getBridgeState());
  // pendingSince < lastSeen -> cursor uses pendingSince (not lastSeen).
  assert.strictEqual(since, t0 - 120, 'since = pendingSince - 120 (the legitimate one is reachable)');
  assert.ok(since < t0 + 5000, 'the cursor is NOT dragged by flood lastSeen');
});

// 4. Recovery: when the legitimate one is re-delivered (markSeen), the drop is
//    cleared; only then is pendingSince released and the cursor returns to the
//    PROCESSING one (recoveryWatermark), not to lastSeen (local reception).
t('recovery: when the drop is re-seen, pendingSince is released and the processing cursor wins', () => {
  _setBridgeStateForTest(fresh());
  const t0 = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: t0 + 5000, seenIds: [], pendingSince: t0,
    dropped: [{id: 'legit-3', ts: t0}], droppedOverflow: false, recoveryWatermark: t0 + 5000, delivery: {}});
  // The relay re-delivers legit-3 -> recoverDropped removes it from the ledger.
  recoverDropped('legit-3');
  // With no drops left, releasePendingSinceIfRecovered releases the anchor.
  releasePendingSinceIfRecovered();
  assert.strictEqual(getBridgeState().pendingSince, null, 'pendingSince released upon recovery');
  assert.strictEqual(getBridgeState().dropped.length, 0, 'drop ledger empty');
  // Now the cursor returns to the PROCESSING one (recoveryWatermark), which is
  // the correct basis for `since` — NOT lastSeen (manipulable local reception).
  const since = subscriptionSince(getBridgeState());
  assert.strictEqual(since, (t0 + 5000) - 120, 'after recovery, since = recoveryWatermark - 120');
});

// 5. The case that WOULD be loss: if the cursor depended on lastSeen (local
//    reception manipulable by a flood of UNPROCESSED events), an event received
//    but not yet admitted could fall outside `since`. With the PROCESSING
//    cursor (recoveryWatermark), a reception flood does NOT move `since`, so a
//    legitimate pending event stays covered even though lastSeen advanced a
//    lot via raw non-admitted reception.
t('regression: lastSeen (reception) does NOT control since — recoveryWatermark (processing) does', () => {
  _setBridgeStateForTest(fresh());
  const t0 = Math.floor(Date.now() / 1000);
  // Scenario: reception flood advanced lastSeen a LOT (t0+5000) but nothing
  // was processed (recoveryWatermark=0) -> since must NOT jump: it stays null
  // (full backlog) so the relay re-delivers what is pending.
  const stateNoProcess = {relay: 'ws://test.local', lastSeen: t0 + 5000, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, delivery: {}};
  const sinceNoProcess = subscriptionSince(stateNoProcess);
  assert.strictEqual(sinceNoProcess, null,
    'without confirmed processing, since=null (full backlog), lastSeen does NOT move it');
  // Scenario with real processing: recoveryWatermark advanced -> since anchors
  // to it (the relay walked that range).
  const stateProcessed = {relay: 'ws://test.local', lastSeen: t0 + 5000, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: t0 + 5000, delivery: {}};
  const sinceProcessed = subscriptionSince(stateProcessed);
  assert.strictEqual(sinceProcessed, (t0 + 5000) - 120,
    'with confirmed processing, since = recoveryWatermark - 120');
});

// cleanup
_setBridgeStateForTest(fresh());
bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
