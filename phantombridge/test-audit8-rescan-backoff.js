// AUDIT-8 (MEDIO): the recovery rescan must NOT become a loop
// of reconnections against the relay/process.
//
// Auditor scenario: if the ledger stays full (lastSeen does not advance) and
// every non-admitted event re-invokes requestDeliveryRescan(), we would get
// connect/close/connect/close... indefinitely — a reconnection DoS against
// the relay and against the process itself.
//
// The fix: exponential backoff (RESCAN_MIN_INTERVAL_MS * 2^attempts, capped at
// RESCAN_MAX_BACKOFF_MS) + a hard limit of rescans per 60s window
// (RESCAN_MAX_PER_MINUTE). When the window cap is reached, the rescan is
// suppressed (deliveryRescanNeeded stays true but NO more reconnection is
// scheduled); the next natural cycle or an event that frees space re-arms it.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit8-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
// Reduce RESCAN_MAX_PER_MINUTE and speed up the backoff for a fast test.
baseConfig.rescanMaxPerMinute = 3;
baseConfig.rescanMinIntervalMs = 100;   // sped-up minimum wait
baseConfig.rescanMaxBackoffMs = 400;    // low cap
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  requestDeliveryRescan, _setBridgeStateForTest, _resetRescanStateForTest,
  getBridgeState, markDelivery,
} = bridge;

function fresh() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}};
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

(async () => {
  let passed = 0, failed = 0;
  // Async test runner: awaits each test's promise and counts.
  function run(name, promise) {
    return promise.then(() => { console.log('  ok:', name); passed++; })
      .catch(e => { console.error('  FAIL:', name, '-', e.message); failed++; });
  }

  // Test 1: the rescans/minute cap suppresses additional rescans — NO
  // loop. After RESCAN_MAX_PER_MINUTE no more reconnection is scheduled in the
  // window (even though deliveryRescanNeeded may stay set).
  await run('per-minute rescan cap: no reconnection loop is scheduled', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const t0 = Date.now();
    // Burst: 100 rescan requests in the same tick (full ledger ->
    // each failed markDelivery asks for a rescan). Only the first schedules one.
    for (let i = 0; i < 100; i++) requestDeliveryRescan();
    await sleep(700); // let the 100ms timers run
    const windowCount = bridge.rescanWindowCount;
    assert.ok(windowCount <= baseConfig.rescanMaxPerMinute,
      'no more than RESCAN_MAX_PER_MINUTE rescans are scheduled in the window (got ' + windowCount + ')');
    console.log('    window: ' + windowCount + ' rescans emitted out of cap ' + baseConfig.rescanMaxPerMinute);
    assert.ok(Date.now() - t0 < 5000, 'the burst resolves fast (no spurious long waits)');
  })());

  // Test 2: backoff — after a burst, the spaced retries show
  // the counter progressing (2 rescans) without exceeding the cap.
  await run('backoff: spaced retries, never exceed the window cap', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    // 1st rescan: start -> min interval (100ms).
    requestDeliveryRescan();
    await sleep(150);
    // 2nd request: enters backoff (approximately 200ms = 100*2).
    requestDeliveryRescan();
    await sleep(500);
    const windowCount = bridge.rescanWindowCount;
    assert.ok(windowCount >= 2, 'at least a second rescan progresses (got ' + windowCount + ')');
    assert.ok(windowCount <= baseConfig.rescanMaxPerMinute, 'never exceeds the cap');
    console.log('    backoff: ' + windowCount + ' rescans in window after burst');
  })());

  // Test 3: the auditor scenario does NOT reconnect without limit — with the
  // ledger full and many failed markDelivery calls, the number of emitted
  // rescans stays bounded by the window cap.
  await run('auditor scenario: 200 failures generate <=MAX rescans (no loop)', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const now = Math.floor(Date.now() / 1000);
    const entry = {};
    for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
    _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
      dropped: [], droppedOverflow: false, delivery: entry});
    // 200 events that are not admitted (fail-closed). Each one attempts a rescan.
    for (let i = 0; i < 200; i++) markDelivery('E' + i, 'pending');
    await sleep(600);
    assert.ok(bridge.rescanWindowCount <= baseConfig.rescanMaxPerMinute,
      '200 failures generate at most ' + baseConfig.rescanMaxPerMinute + ' rescans (got ' + bridge.rescanWindowCount + ')');
    console.log('    200 failures -> ' + bridge.rescanWindowCount + ' rescans scheduled');
  })());

  // cleanup
  _setBridgeStateForTest(fresh());
  bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
  try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
  delete process.env.PHANTOMBRIDGE_CONFIG;

  console.log('');
  console.log(`Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('FATAL:', e && e.message); process.exit(1); });
