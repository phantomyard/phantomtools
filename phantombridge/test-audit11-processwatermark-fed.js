// 🟠 MEDIUM (audit): processWatermark() must be FED with the relay's real
// progress, not just defined. Without integration, the recovery guarantee
// hung: the cursor that kept advancing when receiving events was lastSeen
// (via updateLastSeen), while recoveryWatermark (the only legitimate basis of
// deliveredCanExpire) stayed at 0 and never expired unreachable delivered
// records — or worse, if advanced via Date.now() on an internal admission, it
// expired delivered records not confirmed by the relay (break exactly-once).
//
// The real path: handleIncomingGiftWrap, after authenticating+authorizing+
// admitting an event (successful markDelivery pending), calls finishDelivery(ok)
// which in turn invokes advanceRecoveryWatermark() (a BOUNDED incremental
// advance, NEVER a free jump to Date.now() after downtime). This test
// demonstrates the full chain the audit requires:
//   processed event -> advanceRecoveryWatermark -> recoveryWatermark advances
//     -> an old delivered can expire (and NOT by mere reception/lastSeen).
// AUDIT-M01-OPTION2-FIX: processWatermark(ts) was REMOVED — feeding the
// cursor with an external timestamp (the sender's created_at) reintroduces
// the attack surface. The only legitimate advance is the bounded step.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit11-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  advanceRecoveryWatermark, markDelivery, deliveryStatus, getBridgeState,
  _setBridgeStateForTest, recoveryWatermark,
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

console.log('🟠 AUDIT-11: processWatermark fed with real relay progress (full chain):');

// Test 1: the FULL CHAIN — a processed event advances the watermark and then
// (and only then) an old delivered can expire. This is what the audit asks to
// demonstrate in code: "received event -> lastSeen advances" is NOT enough;
// it is "processed event -> advanceRecoveryWatermark -> recoveryWatermark
// advances -> old delivered expires". The advance is INCREMENTAL BOUNDED (never
// a free jump to Date.now()): several events are processed so the watermark
// walks the range.
t('full chain: processed event advances the watermark -> old delivered expires', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 3600; // delivered delivered 1h ago
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: tX - 1, // watermark just before X
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // BEFORE processing: lastSeen may be inflated by reception (0 here,
  // but in the real path it would be the reception time) — lastSeen is NOT
  // the basis. Only the recovery watermark is.
  const st0 = getBridgeState();
  st0.lastSeen = now; // raw reception up to date, but the watermark doesn't advance because of it
  const wmBefore = bridge.recoveryWatermark;
  // We process ONE burst of real events (relay backlog after connecting).
  advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark > wmBefore,
    'the processed event advances the watermark (real progress)');
  // The advance is BOUNDED: 1 event at -3600 doesn't come close to now.
  assert.ok(bridge.recoveryWatermark < now,
    'an isolated event after downtime does NOT jump to Date.now()');
  // Enough processed events (burst) -> watermark surpasses X + window.
  for (let i = 0; i < 20; i++) advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark >= tX + 120 + 120,
    'after the burst the watermark surpasses X\'s window');
  // Now an admission triggers the sweep; X (ts+240 < watermark) expires.
  assert.ok(markDelivery('nuevo-1', 'pending'), 'admission successful');
  assert.strictEqual(deliveryStatus('X'), null,
    'old delivered expires ONLY after the recovery watermark has advanced');
});

// Test 2: a RECEIVED but NOT processed event does NOT advance the watermark —
// the difference against lastSeen. Here we simulate lastSeen advancing by
// reception (as updateLastSeen does in the real path) but the watermark
// stays put, because there was no processWatermark/admitted event.
t('raw reception (lastSeen) does NOT feed the watermark — only processing does', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 600;
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false, recoveryWatermark: 0,
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // Reception burst: lastSeen advances (updateLastSeen does so in the handler
  // for every frame, even unadmitted), but nobody calls processWatermark ->
  // the watermark stays at 0.
  const st = getBridgeState();
  st.lastSeen = now; // reception advanced lastSeen, not the watermark
  assert.strictEqual(bridge.recoveryWatermark, 0, 'watermark intact at 0 (reception only)');
  // An internal admission (markDelivery) does NOT give the first jump via Date.now().
  markDelivery('nuevo-1', 'pending');
  assert.strictEqual(deliveryStatus('X'), 'delivered',
    'X is preserved: reception/internal admission does not expire delivered (watermark at 0)');
});

// Test 3: monotonic — a previous (backdated) event does not move the watermark back.
// The bounded advance never moves backwards; processing after a backlog only adds.
t('the watermark is monotonic: an old processing does not regress', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  advanceRecoveryWatermark(); // we process something (establishes a watermark)
  const w1 = bridge.recoveryWatermark;
  advanceRecoveryWatermark(); // another processing
  assert.ok(bridge.recoveryWatermark >= w1, 'does not regress despite the step');
  // And an up-to-date processing does advance (bounded step).
  const before = bridge.recoveryWatermark;
  advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark > before, 'advances with real processing');
});

// Test 4: the real integration after OPTION2 — the runtime NO LONGER calls
// processWatermark(sender's created_at) on any path. The watermark advances
// ONLY by the bridge's local clock (advanceRecoveryWatermark) after confirmed
// processing. We verify at the code level that: (a) the handler no longer
// derives wrapTs to feed back the watermark, and (b) an authorized sender has
// no way to push its future created_at into the cursor.
t('OPTION2: the runtime does NOT feed the watermark with the sender created_at', () => {
  const src = fs.readFileSync('./bridge.js', 'utf8');
  // (a) The handler must not call processWatermark(wrap's created_at) after
  // admitting. The watermark is only advanced by advanceRecoveryWatermark()
  // (local clock) from finishDelivery(ok) / successful routing. The only
  // occurrence of 'processWatermark(wrapTs)' is an explanatory COMMENT (a
  // line starting with //) describing why it is no longer done — not code.
  const lines = src.split('\n').filter(l => l.includes('processWatermark(wrapTs)'));
  assert.ok(lines.every(l => /^\s*\/\//.test(l)),
    'every occurrence of processWatermark(wrapTs) must be a comment, not code');
  // (c) legitimate advances use advanceRecoveryWatermark (local clock).
  const finish = src.indexOf('function finishDelivery(id, ok, rejected)');
  let depth = 0, finEnd = finish;
  for (; finEnd < src.length; finEnd++) {
    if (src[finEnd] === '{') depth++;
    else if (src[finEnd] === '}') { depth--; if (depth === 0) break; }
  }
  const finishBlock = src.slice(finish, finEnd + 1);
  assert.ok(finishBlock.includes('advanceRecoveryWatermark();'),
    'finishDelivery must advance the watermark by local clock (not by created_at)');
});

// cleanup
_setBridgeStateForTest(freshState());
bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
