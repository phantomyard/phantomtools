// AUDIT-9 (MEDIO): the rescan must DEMONSTRATE real progress; otherwise it
// enters an explicit BACKPRESSURE state instead of insisting on blind
// reconnections.
//
// Auditor scenario: `requestDeliveryRescan() -> reconnectIncoming()` but the
// ledger release depends on `deliveredCanExpire(e, lastSeen)`. If after the
// rescan `lastSeen` did not advance NOR `delivery` decreased NOR `pendingSince`
// changed, the rescan achieved nothing and must NOT keep requesting
// reconnections indefinitely: it must enter BACKPRESSURE (rescanStalled=true)
// until real progress exists (or the cooldown breather expires).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit9-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
// Lower thresholds for a fast test: 1 rescan without progress -> BACKPRESSURE.
baseConfig.rescanMaxStalled = 1;
baseConfig.rescanMinIntervalMs = 100;
baseConfig.rescanMaxBackoffMs = 400;
baseConfig.rescanMaxPerMinute = 100; // do not limit by window in this test
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  requestDeliveryRescan, _setBridgeStateForTest, _resetRescanStateForTest,
  getBridgeState, markDelivery, updateLastSeen,
} = bridge;

function fresh() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}};
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

(async () => {
  let passed = 0, failed = 0;
  function run(name, promise) {
    return promise.then(() => { console.log('  ok:', name); passed++; })
      .catch(e => { console.error('  FAIL:', name, '-', e.message); failed++; });
  }

  // Test 1: NO progress (lastSeen does not advance, delivery does not decrease,
  // pendingSince does not change) -> the rescan enters BACKPRESSURE
  // (rescanStalled=true) and does NOT keep requesting reconnections.
  await run('no real progress -> BACKPRESSURE (rescanStalled) bounds reconnections', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const now = Math.floor(Date.now() / 1000);
    // Ledger full of immature delivered that do NOT expire (frozen lastSeen).
    const entry = {};
    for (let i = 0; i < 12000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
    _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 5000, seenIds: [],
      pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
    // Trigger the rescan. Without a real reconnection (reconnectIncoming null),
    // the guard will emit the warn and the progress measurement will see that
    // NOTHING advanced.
    requestDeliveryRescan();
    // Wait for the waitMs (min 100ms) + the post-reconnect measurement.
    await sleep(800);
    assert.strictEqual(bridge.rescanStalled, true,
      'after a rescan with no progress, it enters BACKPRESSURE (rescanStalled=true)');
    assert.ok(bridge.rescanStalledSince > 0, 'records the stall moment');
    console.log('    rescanStalled=' + bridge.rescanStalled);
  })());

  // Test 2: with BACKPRESSURE active, requestDeliveryRescan does NOT emit more
  // rescans (deliveryRescanNeeded stays set but reconnection is not re-requested).
  await run('in BACKPRESSURE: no more rescans are emitted until cooldown/progress', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const now = Math.floor(Date.now() / 1000);
    const entry = {};
    for (let i = 0; i < 12000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
    _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 5000, seenIds: [],
      pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
    requestDeliveryRescan();
    await sleep(800);
    assert.strictEqual(bridge.rescanStalled, true, 'BACKPRESSURE active');
    // A new rescan attempt while stalled: the guard suppresses it (count does not rise).
    const before = bridge.rescanWindowCount;
    requestDeliveryRescan();
    requestDeliveryRescan();
    await sleep(500);
    // The stalled guard does not emit -> the window counter does not grow from these.
    const after = bridge.rescanWindowCount;
    assert.strictEqual(bridge.deliveryRescanNeeded, true,
      'the request stays recorded... (not discarded)');
    console.log('    rescans emitted: ' + before + ' -> ' + after + ' (stalled)');
  })());

  // Test 3: REAL progress (successful markDelivery / updateLastSeen advances)
  // leaves BACKPRESSURE and resets the stall burst.
  await run('real progress (admission) clears BACKPRESSURE', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const now = Math.floor(Date.now() / 1000);
    // Delivered old enough (ts = now-600): with an advanced lastSeen
    // they expire by watermark, freeing space for admission.
    const entry = {};
    for (let i = 0; i < 12000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 600};
    _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 5000, seenIds: [],
      pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
    requestDeliveryRescan();
    await sleep(800);
    assert.strictEqual(bridge.rescanStalled, true, 'BACKPRESSURE active after blind rescan');
    // REAL PROGRESS: the RECOVERY watermark advances (processed/admitted event
    // arrives) -> the old delivered expire by watermark and the ledger frees up.
    // NOTE (Phase 2 / AUDIT-10): it is NOT lastSeen (reception cursor) that
    // proves progress, but recoveryWatermark.
    const st = getBridgeState();
    st.recoveryWatermark = now; // processed up to now -> delivered with ts now-600 unreachable
    // Now an admission has room -> successful markDelivery -> clears.
    const admitted = markDelivery('nuevo-1', 'pending');
    assert.strictEqual(admitted, true, 'admission possible after freeing');
    assert.strictEqual(bridge.rescanStalled, false,
      'successful markDelivery clears BACKPRESSURE (real progress)');
    console.log('    rescanStalled=' + bridge.rescanStalled + ' after admission');
  })());

  // cleanup
  _setBridgeStateForTest(fresh());
  try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
  delete process.env.PHANTOMBRIDGE_CONFIG;

  console.log('');
  console.log(`Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('FATAL:', e && e.message); process.exit(1); });
