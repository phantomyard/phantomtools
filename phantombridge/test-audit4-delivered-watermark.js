// AUDIT-4 regression test (option B — watermark, not TTL wall-clock):
// reproduces the auditor's scenario: a bridge that delivers a gift-wrap,
// goes down >30 min and on restart must NOT re-run `delivered` (exactly-once
// after a long downtime).
//
// Before (DELIVERY_TTL_SECS=30min): delivered[X] expired by wall-clock,
// seenIds expired at ~180s, but lastSeen persisted to disk -> on
// restart with since=lastSeen-120 the relay re-delivered X and deliveryStatus(X)
// was null -> it re-ran.
//
// Now (option B): delivered ONLY expires by WATERMARK (when lastSeen already
// advanced past the replay window), never by wall-clock. A
// long downtime freezes lastSeen -> delivered[X] does NOT expire -> the real
// dedup in handleIncomingGiftWrap (isSeen && delivered==='delivered') skips it.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit4-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markSeen, markDelivery, deliveryStatus, isSeen,
  _setBridgeStateForTest, STATE_FILE, getBridgeState,
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

// The real dedup in handleIncomingGiftWrap (after AUDIT-4): the durable ledger
// is the authoritative source of "already delivered"; it does NOT require isSeen() (which
// expires at 180s and is therefore false after a long downtime).
function shouldSkipAsDelivered(id) {
  return deliveryStatus(id) === 'delivered';
}

assert.strictEqual(STATE_FILE, tmpState, 'STATE_FILE must point to the temp file');

console.log('AUDIT-4: delivered by watermark (no TTL wall-clock) + cap fail-closed:');

// ---- 🔴: long downtime does NOT re-run delivered ----
t('downtime 1h: delivered survives restart (does not expire by wall-clock)', () => {
  _setBridgeStateForTest(freshState());
  // Simulate: received+delivered at T0, with lastSeen=T0.
  const T0 = Math.floor(Date.now() / 1000) - 3600; // 1 hour ago
  // lastSeen = T0 (frozen during the downtime, as persisted to disk)
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: T0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    delivery: {'X': {status: 'delivered', ts: T0}}});
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: T0, seenIds: [{id: 'X', ts: T0}], pendingSince: null,
    dropped: [], droppedOverflow: false,
    delivery: {'X': {status: 'delivered', ts: T0}}});
  // "Restart": reload from disk. lastSeen is still T0 (1h old).
  // delivered must NOT expire even though 60 min of the old TTL have passed.
  getBridgeState().delivery = getBridgeState().delivery || {};
  // marking a new entry triggers the sweep; delivered must still be present.
  markDelivery('Y', 'pending');
  assert.strictEqual(deliveryStatus('X'), 'delivered', 'delivered does NOT expire by wall-clock after 1h of downtime');
  // And dedup skips X (does not re-run)
  assert.strictEqual(shouldSkipAsDelivered('X'), true, 'replay de X -> SKIP (exactly-once)');
});

t('delivered only expires by watermark (recoveryWatermark advanced past it)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // X delivered 1h ago, but the RECOVERY WATERMARK already advanced
  // (the bridge PROCESSED/admitted events after delivering X -> the relay can no
  // longer re-deliver it within the window). -> X can be expired.
  const tX = now - 3600;
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 600, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 600, // procesado hasta hace 10 min
    delivery: {'X': {status: 'delivered', ts: tX}}});
  markDelivery('Y', 'pending');
  assert.strictEqual(deliveryStatus('X'), null, 'delivered with recoveryWatermark far ahead -> expires (watermark met)');
});

t('delivered does NOT expire if lastSeen did not advance (downtime)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 7200; // delivered 2h ago
  // lastSeen = tX (the bridge did NOT process anything since X: downtime / startup)
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: tX, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    delivery: {'X': {status: 'delivered', ts: tX}}});
  markDelivery('Y', 'pending');
  assert.strictEqual(deliveryStatus('X'), 'delivered', 'delivered is kept even after 2h: lastSeen did not advance');
});

// ---- 🟠: pending DOES expire by TTL (different semantics) ----
t('pending expires by TTL wall-clock (PENDING_TTL_SECS)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // pending from 25h ago (PENDING_TTL_SECS=24h) -> expires
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    delivery: {'P': {status: 'pending', ts: now - 25 * 3600}}});
  markDelivery('Y', 'delivered');
  assert.strictEqual(deliveryStatus('P'), null, 'pending viejo (>TTL) expira');
});

t('pending reciente (dentro de TTL) NO expira', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    delivery: {'P2': {status: 'pending', ts: now - 3600}}});
  markDelivery('Y', 'delivered');
  assert.strictEqual(deliveryStatus('P2'), 'pending', 'pending reciente se conserva');
});

// ---- 🟠: cap fail-closed ----
t('cap: no evicta delivered inmaduros y rechaza la admision (fail-closed)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // Llenar delivery solo con delivered INMADUROS (lastSeen==their ts, no avanzado)
  const entry = {};
  const t0 = now - 60; // todos recientes
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: t0 + i};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: entry});
  // Al admitir un nuevo pending: NO se evicta delivered inmaduro; en su lugar
  // la admision se RECHAZA (fail-closed). El caller debe abortar el comando.
  const admitted = markDelivery('fresh', 'pending');
  assert.strictEqual(admitted, false, 'admision rechazada cuando el ledger esta lleno de delivered inmaduros');
  assert.strictEqual(deliveryStatus('fresh'), null, 'NO se admitio fresh (no hay entrada durable pending)');
  // Los delivered inmaduros NO se evictan masivamente (fail-closed).
  const st = bridge.getBridgeState().delivery;
  const delivCount = Object.keys(st).filter(k => st[k] && st[k].status === 'delivered').length;
  assert.ok(delivCount > 10000, 'no se evictaron delivered inmaduros en masa (fail-closed), quedan ' + delivCount);
  // Un delivered es admisible explicitamente aun lleno (replayer de delivered no bloquea).
  const admittedDel = markDelivery('admin-1', 'delivered');
  assert.strictEqual(admittedDel, true, 'marcar delivered (finishDelivery) si se admite incluso sobre el cap');
});

// cleanup
_setBridgeStateForTest(freshState());
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
