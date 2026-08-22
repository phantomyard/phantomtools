// ALTO-3 + MEDIO-4 regression test (audit 462e62b): the bridge must persist
// delivery intent durably (fsync, not the 5s debounce timer) so that:
//   - a crash between admission and publish cannot re-deliver a DM that was
//     already committed (duplication), nor re-drop one that wasn't (loss);
//   - a wrap marked `delivered` is skipped on re-delivery;
//   - a wrap still `pending` (publish failed) is retried, not silently
//     marked seen and dropped (the MEDIO-4 gap).
//
// We exercise the real bridge functions markDelivery/deliveryStatus against a
// real durable state file (pointed at a temp path via PHANTOMBRIDGE_CONFIG
// BEFORE the module loads, so the module-scoped STATE_FILE const is correct).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

// Build a temp config (copy of the real one) with an isolated stateFile so
// the module's STATE_FILE const lands on the temp path. Must be set BEFORE
// require('./bridge.js').
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'alto3-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markDelivery, deliveryStatus, isSeen, markSeen,
  _setBridgeStateForTest, STATE_FILE,
} = bridge;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function resetState() {
  _setBridgeStateForTest({
    relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], delivery: {},
  });
}

// Guard: make sure the module actually loaded our temp path as STATE_FILE.
assert.strictEqual(STATE_FILE, tmpState, 'STATE_FILE debe apuntar al temp para el test');

console.log('ALTO-3 + MEDIO-4 (delivery ledger transaccional):');

t('markDelivery pending -> deliveryStatus(id)==pending', () => {
  resetState();
  markDelivery('w-1', 'pending');
  assert.strictEqual(deliveryStatus('w-1'), 'pending');
});

t('markDelivery delivered -> deliveryStatus(id)==delivered', () => {
  resetState();
  markDelivery('w-2', 'delivered');
  assert.strictEqual(deliveryStatus('w-2'), 'delivered');
});

t('wrap delivered -> dedup skips it (isSeen + delivered)', () => {
  resetState();
  markSeen('w-3');
  markDelivery('w-3', 'delivered');
  assert.strictEqual(isSeen('w-3') && deliveryStatus('w-3') === 'delivered', true);
});

t('wrap pending (publish failed) -> NOT treated as delivered (allows retry)', () => {
  resetState();
  markSeen('w-4');
  markDelivery('w-4', 'pending');
  assert.strictEqual(isSeen('w-4'), true);
  assert.strictEqual(deliveryStatus('w-4'), 'pending');
  assert.strictEqual(isSeen('w-4') && deliveryStatus('w-4') === 'delivered', false);
});

t('no delivery record -> not delivered -> gets processed', () => {
  resetState();
  markSeen('w-5');
  assert.strictEqual(deliveryStatus('w-5'), null);
  assert.strictEqual(isSeen('w-5') && deliveryStatus('w-5') === 'delivered', false);
});

t('markDelivery writes the DURABLE state (sync, file present immediately)', () => {
  try {
    resetState();
    if (fs.existsSync(tmpState)) fs.unlinkSync(tmpState);
    // Replicate the real handleIncomingGiftWrap flow: markSeen BEFORE
    // marking the delivery (lines 1208-1209), so seenIds + delivery both
    // become durable.
    markSeen('dur-1');
    markDelivery('dur-1', 'delivered');
    // markDelivery -> flushStateNow -> persistState() wrote + fsync + rename.
    // It must exist ALREADY (sync), without waiting for the 5s timer.
    assert.ok(fs.existsSync(tmpState), 'state file written synchronously after markDelivery');
    const parsed = JSON.parse(fs.readFileSync(tmpState, 'utf8'));
    assert.strictEqual(parsed.delivery['dur-1'].status, 'delivered', 'ledger on disk');
  } catch (e) {
    resetState();
    throw e;
  }
});

t('after "crash/restart": delivered restored from disk and dedup skips it', () => {
  // Simulate restart: the file has dur-1 in BOTH seenIds and delivery[]. A
  // fresh in-memory state rebuilt as loadState() does must restore both and
  // treat it as delivered (so the dedup skips it on re-delivery).
  const s = JSON.parse(fs.readFileSync(tmpState, 'utf8'));
  _setBridgeStateForTest({
    relay: s.relay || 'ws://test.local',
    lastSeen: s.lastSeen || 0,
    seenIds: s.seenIds || [],
    pendingSince: s.pendingSince != null ? s.pendingSince : null,
    dropped: s.dropped || [],
    delivery: (s.delivery && typeof s.delivery === 'object') ? s.delivery : {},
  });
  assert.strictEqual(deliveryStatus('dur-1'), 'delivered', 'delivered restaurado tras restart');
  assert.strictEqual(isSeen('dur-1'), true, 'seen restaurado');
  assert.strictEqual(isSeen('dur-1') && deliveryStatus('dur-1') === 'delivered', true, 'dedup salta tras restart');
});

// cleanup
bridge.getBridgeState && _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: [], delivery: {}});
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
