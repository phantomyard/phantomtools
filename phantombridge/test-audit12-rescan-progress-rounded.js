// 🟡 MEDIUM (audit): the rescan progress criterion must not consider
// pendingSince as evidence. pendingSince only changes because ANOTHER drop is
// recorded, without recovering any event or freeing any delivered:
//   rescan -> another drop comes in -> pendingSince changes -> progressed=true
//   -> rescanAttempts=0   (even though deliveryCount did not drop, recoveryWatermark
//                          did not advance, no message was admitted)
// That delays detection of a truly stalled rescan.
//
// 🟠 MEDIUM (AUDIT-17): an ID DISAPPEARING from `dropped` is also not evidence
// of processing: it can leave via pruning/overflow/later cleanup without ever
// being admitted. Relocation progress = the specific dropped was re-ENTERED
// into `delivery` via markDelivery (a real re-admission), not just that it is
// no longer in the drop ledger.
//
// Progress must represent REAL RECOVERY:
//   - recoveryWatermark advanced, OR
//   - deliveryCount decreased, OR
//   - a specific dropped was re-admitted (exists in delivery).
// pendingSince by itself is NOT evidence of progress, and neither is an ID
// leaving `dropped` without being recorded in `delivery`.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit12-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
// We speed up rescan timing for the test (short cooldown/rescan).
baseConfig.rescanStallCooldownMs = 500;
baseConfig.rescanMinIntervalMs = 20;
// A single rescan request adds 2 attempts (1 on emit + 1 on the no-progress
// measurement); with the threshold at 2, a single rescan without progress
// already triggers BACKPRESSURE, which is what this test wants to verify
// deterministically.
baseConfig.rescanMaxStalled = 2;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markDelivery, recordDropped, recoverDropped, requestDeliveryRescan,
  _resetRescanStateForTest, _setBridgeStateForTest, getBridgeState,
} = bridge;

let passed = 0, failed = 0;
let _chain = Promise.resolve();
function t(name, fn) {
  _chain = _chain.then(async () => {
    try { await fn(); console.log('  ok:', name); passed++; }
    catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
  });
}

function freshState() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, delivery: {}};
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

console.log('🟡 AUDIT-12: rescan progress does NOT depend on pendingSince (real recovery):');

// Test 1: a pendingSince change BY ITSELF (another drop recorded, without
// recovering anything) must count as NO progress -> the rescan enters
// BACKPRESSURE after RESCAN_MAX_STALLED attempts.
t('pendingSince changes due to another drop -> NO progress -> BACKPRESSURE', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  // Ledger full of immature delivered and one pending drop.
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 60; // recent delivered, NOT expirable
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 10, dropped: [{id: 'D1', ts: now - 10}],
    droppedOverflow: false, recoveryWatermark: now - 60,
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // The rescan triggers; meanwhile nothing is recovered, only a new drop comes
  // in -> pendingSince changes but deliveryCount/watermark/dropped do not.
  requestDeliveryRescan();
  await sleep(1200); // let the rescan + progress measurement run
  assert.strictEqual(bridge.rescanStalled, true,
    'BACKPRESSURE active: pendingSince alone is not progress (stalled rescan)');
  _resetRescanStateForTest();
});

// Test 2: REAL progress via advanced watermark -> does NOT enter BACKPRESSURE
// (even if pendingSince also changed, the watermark counts).
t('recoveryWatermark advances -> progress -> rescan does not stall', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'D1', ts: now - 5}],
    droppedOverflow: false, recoveryWatermark: now - 60,
    delivery: {'X': {status: 'delivered', ts: now - 600}}});
  requestDeliveryRescan();
  // During the measurement the watermark advances (real processed event): in
  // practice this happens via processWatermark in handleIncomingGiftWrap; here
  // we simulate it right before the measurement (RESCAN_MIN_INTERVAL_MS*2).
  await sleep(30);
  getBridgeState().recoveryWatermark = now;
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, false,
    'NOT stalled: the watermark advanced = real recovery');
  _resetRescanStateForTest();
});

// Test 3: REAL progress via decreasing deliveryCount (ledger freed).
t('deliveryCount decreases -> progress -> rescan does not stall', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 600,
    delivery: {
      'X': {status: 'delivered', ts: now - 3600},   // expirable (advanced watermark)
      'Y': {status: 'delivered', ts: now - 3600},   // expirable
      'Z': {status: 'pending', ts: now - 30},       // stays
    }});
  requestDeliveryRescan();
  // The rescan frees 2 expirable delivered -> deliveryCount drops from 3 to 1.
  await sleep(30);
  const st = getBridgeState();
  st.delivery = {'Z': st.delivery['Z']};   // X and Y expire; Z (pending) stays
  _setBridgeStateForTest(st);              // re-syncs deliverySize with the ledger
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, false,
    'NOT stalled: the ledger freed entries (deliveryCount dropped)');
  _resetRescanStateForTest();
});

// Test 4: REAL progress via a specific dropped re-admitted through markDelivery
// (the message re-entered delivery = real re-admission).
t('a specific dropped re-admitted into delivery -> progress -> rescan does not stall', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'D1', ts: now - 5}],
    droppedOverflow: false, recoveryWatermark: now - 600,
    delivery: {'X': {status: 'delivered', ts: now - 3600}}});
  requestDeliveryRescan();
  // During the rescan, the dropped D1 is re-admitted: the relay re-delivers it
  // and markDelivery records it in `delivery` (the real recovery path).
  await sleep(30);
  markDelivery('D1', 'delivered', now - 5);
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, false,
    'NOT stalled: the specific dropped was re-admitted into delivery = real recovery');
  _resetRescanStateForTest();
});

// Test 4b (🟠 AUDIT-17): CONTROL — an ID that DISAPPEARS from `dropped` via
// cleanup/pruning (without entering `delivery`) is NOT progress: same watermark,
// same deliveryCount, no markDelivery. The rescan must enter BACKPRESSURE
// (previously this scenario gave a false-positive progress).
t('a dropped leaving dropped without entering delivery -> NO progress -> BACKPRESSURE', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'D1', ts: now - 5}],
    droppedOverflow: false, recoveryWatermark: now - 600,
    delivery: {'X': {status: 'delivered', ts: now - 60}}});
  requestDeliveryRescan();
  // The ID D1 disappears from `dropped` via pruning/cleanup (e.g. eviction or
  // overflow) BUT never goes through markDelivery: it is not in `delivery`.
  await sleep(30);
  recoverDropped('D1'); // removes from the drop ledger, without processing
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, true,
    'BACKPRESSURE: D1 left dropped without markDelivery is not real progress');
  _resetRescanStateForTest();
});

// Test 4c (🟠 AUDIT-17): CONTROL — an ID disappearing from `dropped` via
// overflow/eviction (full ledger deletion, not entering delivery) is not
// progress; re-admitting another distinct ID is also not progress if it was
// not a dropped before.
t('drop evicted/deleted without delivery -> NO progress; static watermark+count -> BACKPRESSURE', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'D1', ts: now - 5}],
    droppedOverflow: false, recoveryWatermark: now - 600,
    delivery: {'X': {status: 'delivered', ts: now - 60}}});
  requestDeliveryRescan();
  // Eviction: D1 is removed from the drop ledger (overflow/cleanup) without
  // recovery. deliveryCount stays the same (X does not expire), watermark too.
  await sleep(30);
  getBridgeState().dropped = [];       // evicted
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, true,
    'BACKPRESSURE: D1 eviction without delivery is not real progress');
  _resetRescanStateForTest();
});

// Test 5: CONTROL — now the old criterion (with pendingSince) would have given
// progress with D1; we verify the fix does NOT count a mere change: if there is
// no real progress, it enters BACKPRESSURE even though pendingSince changed.
// (Reinforces test 1 with an explicit value change.)
t('pendingSince changes (different value) without recovery -> NO progress -> BACKPRESSURE', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 10, dropped: [{id: 'D1', ts: now - 10}],
    droppedOverflow: false, recoveryWatermark: now - 600,
    delivery: {'X': {status: 'delivered', ts: now - 60}}});
  requestDeliveryRescan();
  // pendingSince changes value (e.g. another drop is recorded) but there is no
  // recovery: deliveryCount same, watermark same, D1 still present.
  await sleep(30);
  getBridgeState().pendingSince = now - 3; // value changed
  recordDropped('D2');                      // another drop comes in (still unrecovered)
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, true,
    'BACKPRESSURE: pendingSince changed but there was no real recovery');
  _resetRescanStateForTest();
});

_chain.then(() => {
  // cleanup — must run AFTER the cases, not before
  _setBridgeStateForTest(freshState());
  bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
  try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
  delete process.env.PHANTOMBRIDGE_CONFIG;
  console.log('');
  console.log(`Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
}).catch((e) => { console.error('FATAL:', e && e.message); process.exit(1); });
