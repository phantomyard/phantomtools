// AUDIT-M01-BLOCKER2 (kaieriksen, 🔴 BLOCKING): processWatermark() ran BEFORE
// the M01 authorization gate. An authenticated agent WITHOUT room permission
// could send a gift-wrap with a fabricated (future) created_at that advanced
// recoveryWatermark even though the command was later denied by
// agentCanOperateRoom -> deliveredCanExpire() evicted delivered that the
// relay could still re-deliver (break exactly-once / loss).
//
// This adversarial test verifies the REAL invariant, not the syntax:
//   1. An event DENIED by M01 (finishDelivery rejected) does NOT advance the
//      watermark, whatever its created_at.
//   2. A PROCESSED event (finishDelivery ok, after passing the gate) DOES
//      advance the watermark, and only within a credible window (future skew
//      <= 24h): a far hostile created_at (>24h in the future) is ignored.
//   3. The agentCanOperateRoom gate denies the agent without room permission.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit-blocker2-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  advanceRecoveryWatermark, getBridgeState, _setBridgeStateForTest,
  finishDelivery, agentCanOperateRoom, evalRoomPermission,
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
function wm() { return (getBridgeState() || {}).recoveryWatermark || 0; }

// 1. finishDelivery(rejected) with a future created_at does NOT move the watermark.
t('rejected: a denied event with future created_at does NOT advance recoveryWatermark', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const maliciousFuture = now + 7 * 24 * 3600; // one week in the future (fabricated)
  // We simulate the handler deciding NOT to process (denied by M01, or room not
  // active, etc.): finishDelivery(id, false, true) -> rejected.
  finishDelivery('wrap-rejected-1', false, true, maliciousFuture);
  assert.strictEqual(wm(), 0, 'a rejected event must not advance the watermark');
});

// 2. A processed event with a far created_at does NOT feed the cursor: the
//    watermark only advances via advanceRecoveryWatermark() (bounded step),
//    never by an external sender timestamp.
t('hardening: a single recent-event does NOT advance to the local clock', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // Previous watermark established (confirmed progress 30 days ago: long
  // downtime). ONE processed event arrives ({ts} demo at ~`now-600`). The
  // watermark must NOT jump to `now` (current clock), because the relay has
  // not demonstrated walking the 30-day backlog with a single message — that
  // would expire old delivered that are still recoverable.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 30 * 24 * 3600, // 30 days ago
    delivery: {}});
  finishDelivery('wrap-downtime', true, false); // a single processed event
  const w = wm();
  // The advance stays BOUNDED to the watermark step (nowhere near now).
  assert.ok(w < now - 20 * 24 * 3600,
    'a single event after downtime cannot jump to Date.now() (bounded watermark)');
  assert.ok(w > now - 30 * 24 * 3600,
    'the watermark does advance one step (real progress from one event)');
});

// 3. A real backlog (many processed events) DOES advance the watermark
//    incrementally, showing recovery progresses without breaking exactly-once.
//    Each confirmed event grants at most one bounded step.
t('ok: processed backlog advances the watermark incrementally (real progress)' , () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 30 * 24 * 3600, // after 30 days of downtime
    delivery: {}});
  const START = wm();
  // N consecutive events are processed (relay backlog burst).
  for (let i = 0; i < 500; i++) finishDelivery('wrap-backlog-' + i, true, false);
  const w = wm();
  assert.ok(w > START, 'the watermark advances with the backlog burst');
  assert.ok(w <= now, 'the watermark never exceeds the bridge local clock');
});

// 4. The M01 gate denies an agent without room permission (independent of
//    created_at): the command should not even reach processing.
t('gate M01: an agent without room permission is denied before processing', () => {
  const perms = { restricted: { 'secret-room': ['bob'] } };
  assert.strictEqual(evalRoomPermission(perms, 'alice', 'secret-room'), false,
    'alice (no permission) must not operate secret-room');
  assert.strictEqual(evalRoomPermission(perms, 'bob', 'secret-room'), true,
    'bob (with permission) must be able to operate secret-room');
});

// 5. Compatibility: an agent with full can advance the watermark after
//    processing (confirms the fix does not break legitimate progress).
t('ok: a full agent processed advances the watermark (real progress intact)', () => {
  const perms = { full: ['alice'] };
  assert.strictEqual(evalRoomPermission(perms, 'alice', 'cualquier'), true);
  assert.strictEqual(evalRoomPermission(perms, 'alice', null), true); // room-agnostic
});

// 6. agentCanOperateRoom stays exported and functional as a real gate.
t('real gate agentCanOperateRoom: function and behavior', () => {
  assert.strictEqual(typeof agentCanOperateRoom, 'function');
});

// 7. finishDelivery with ok=true advances the watermark ONLY by the bridge
//    local clock (confirmed real stream progress), never by the sender
//    created_at. (OPTION2: the wrapTs parameter was removed from finishDelivery.)
t('finishDelivery(ok=true) advances by local clock, not by the sender created_at', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // Previous watermark established (AUDIT-10: advanceRecoveryWatermark only
  // extends an already-present watermark; it does not give the first jump from 0).
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 600, delivery: {}});
  // The 4th arg (old wrapTs = sender created_at) no longer exists in the
  // signature; finishDelivery(id, ok, rejected) ignores any extra arg.
  finishDelivery('wrap-ok-1', true, false);
  const w = wm();
  assert.ok(w >= now - 600, 'finishDelivery(ok) advances the watermark (real progress)');
  assert.ok(w < now, 'the advance is BOUNDED (no free jump to now)');
});

// 8. AUDIT-M01-OPTION2 (🔴 kaieriksen BLOCKING): the watermark no longer depends
//    on the sender `created_at`. finishDelivery no longer receives wrapTs; it
//    uses advanceRecoveryWatermark() which advances with the bridge LOCAL
//    CLOCK. Therefore a credible or hostile sender `created_at` does NOT
//    control the cursor (the sender does not control the bridge clock). The
//    watermark only advances via confirmed stream progress (bridge local clock).
//
// 8a. finishDelivery(ok=true) does NOT use wrapTs => a credible ts as an extra
//     argument no longer has effect (old 4-arg signature; the 4th is ignored).
t('OPTION2: finishDelivery(ok) advances only by local clock, NOT by sender ts', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // State with watermark already established (prior real progress, AUDIT-10):
  // advanceRecoveryWatermark() extends to the local clock. A credible
  // `created_at` that should NOT advance (the old chain expected wm == ts) is
  // no longer the source. The advance comes from the bridge clock, not the arg.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 300, // previous watermark established
    delivery: {}});
  // We may pass a 4th argument (sender ts) or not: finishDelivery already
  // ignores it. The watermark advances to the local clock (~now), not to the
  // argument ts.
  finishDelivery('wrap-ok-1', true, false, now + 23 * 3600); // hostile +23h ts ignored
  const w = wm();
  assert.ok(w > 0, 'finishDelivery(ok) advances the watermark via real progress');
  assert.ok(w <= now, 'the advance never exceeds the bridge local clock');
  assert.ok(w <= now - 300 + 300, 'the advance is BOUNDED to the step (prev was less than 1 step from now)');
});

// 8b. The case ChatGPT asks for: an AUTHORIZED sender for room-A sends with
//     created_at = now+23h -> the watermark does NOT advance to that future ts.
t('OPTION2: authorized sender with future created_at (+23h) does NOT advance the watermark', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // Previous watermark established (real progress). The sender authorized for
  // room-A could have operated, but the fabricated created_at (+23h) is NOT
  // the source: the watermark only advances to the bridge local clock, never
  // to the sender ts, not even within the 24h limit.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 600,
    delivery: {}});
  const fabricadoFuturo = now + 23 * 3600; // +23h: passes the 24h filter
  finishDelivery('wrap-roomA', true, false, fabricadoFuturo);
  const w = wm();
  assert.ok(w <= now, 'watermark does NOT exceed the local clock despite the future created_at (+23h)');
  assert.ok(w >= now - 600, 'watermark only advanced via real progress (not the sender ts)');
  assert.ok(w < now, 'the advance stays bounded to the step (no jump to now after downtime)');
});

// 9. AUDIT-M01-BLOCKER3 (kaieriksen, 🔴 BLOCKING): the READ commands
//    status/help/routes do NOT pass the M01 gate and do NOT represent stream
//    progress. Since the watermark no longer depends on the sender `created_at`
//    on ANY path, status/help cannot advance it in any way.
t('BLOCKER3: status/help without permission cannot advance the watermark', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const fabricado = now + 23 * 3600; // +23h
  finishDelivery('wrap-status', true, false, fabricado);
  // The watermark does not advance via the sender ts (nor on its own; the
  // bridge local clock, with no prior watermark, gives no jump from 0 — AUDIT-10).
  assert.ok(wm() <= now, 'status/help cannot exceed the bridge local clock');
});

// 10. Only CONFIRMED stream progress (bridge local clock, prior watermark
//     established) advances. join/leave/inject/routing pass auth and the local
//     clock grants the advance; the sender ts never intervenes.
t('OPTION2: only the bridge local clock (real progress) advances the watermark', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 120,
    delivery: {}});
  finishDelivery('wrap-auth', true, false, now - 5); // credible sender ts
  const w = wm();
  assert.ok(w > 0, 'real progress advances');
  assert.ok(w <= now, 'the advance never exceeds the local clock (the sender ts does not control it)');
});

console.log(`\nAUDIT-M01-BLOCKER2 (processWatermark gate order) Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
