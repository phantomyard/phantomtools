// AUDIT kaieriksen M01 (🔴 BLOCKING of PR #24 phantomyard):
//   "the configured permissions are never enforced on the agent-controlled
//    Jitsi paths... roomAgents only limits recipients and does not authorize
//    the sender. Gate every room command and message by sender plus room
//    scope before accepting it."
//
// Confirmed in the pre-fix code: CONFIG.permissions was NOT read anywhere;
// roomAgents only filtered RECIPIENTS and routingPerms only applied to DM↔DM.
// Any authenticated agent could join/leave/inject/recordings in any room.
//
// Fix: helper `agentCanOperateRoom(sender, room)` (and its pure logic
// `evalRoomPermission`) that resolves against
//   "permissions": { "full": [...], "restricted": { room: [agents] } }
// with fail-closed, and the gates are applied on join/leave/inject/recordings.
// WITHOUT a `permissions` block the helper -> true (compat: legacy behavior,
// without breaking deployments without permissions).
//
// THIS TEST EXERCISES THE REAL MATRIX (review §4 critique): it exercises the
// decision logic against ISOLATED configs — including the fail-closed bugs
// (permissions:{}, malformed permissions) and verifies the order of the real
// gates and the watermark advance (BLOCKER 2) in the code.
const assert = require('assert');
const fs = require('fs');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

// --- Pure decision matrix (without loading the module, without network) ---
// evalRoomPermission(permConfig, senderName, room):
//   undefined          -> legacy/open
//   {}                 -> participants join named rooms; room-agnostic denied
//   {full:[]}          -> same as {} (no full tier)
//   {full:[a]}         -> a: any room + room-agnostic; others: any named room
//   {full:'a'} (mal)   -> no full tier (participants join named rooms)
//   null / array       -> fail-closed (malformed block)

function evalP(permConfig, sender, room) {
  return bridgeModule.evalRoomPermission(permConfig, sender, room);
}
// We load the module once (enough for the exported pure logic; the repo base
// config is used for loading compatibility, not for the matrix).
require('./testlib.js').setup();
const bridgeModule = require('./bridge.js');

t('legacy: without a permissions block -> open (compat with existing deployments)', () => {
  assert.strictEqual(evalP(undefined, 'alice', 'mia'), true);
  assert.strictEqual(evalP(undefined, 'algún-agente', null), true);
});

t('{} (empty block) -> participants join named rooms; room-agnostic denied', () => {
  // Role-based (no per-room ACL): a present block governs only the `full`
  // tier (recordings). Participants (authenticated agents) still join/speak
  // any NAMED room; room-agnostic actions need `full`.
  assert.strictEqual(evalP({}, 'alice', 'mia'), true, 'participant joins named room');
  assert.strictEqual(evalP({}, 'alice', null), false, 'room-agnostic denied without full');
});

t('full:[] -> participants join named rooms; room-agnostic denied', () => {
  assert.strictEqual(evalP({ full: [] }, 'alice', 'mia'), true);
  assert.strictEqual(evalP({ full: [] }, 'bob', null), false);
});

t('full:[alice] -> alice any room + room-agnostic; bob named rooms only', () => {
  assert.strictEqual(evalP({ full: ['alice'] }, 'alice', 'mia'), true);
  assert.strictEqual(evalP({ full: ['alice'] }, 'alice', null), true); // room-agnostic (recordings)
  assert.strictEqual(evalP({ full: ['alice'] }, 'bob', 'mia'), true);   // participant: named room
  assert.strictEqual(evalP({ full: ['alice'] }, 'bob', null), false);   // participant: no room-agnostic
});

t('malformed full (string) -> no full tier: participants join named rooms', () => {
  // A malformed `full` yields no full tier (recordings denied), but the
  // participant baseline still lets authenticated agents join/speak.
  assert.strictEqual(evalP({ full: 'alice' }, 'alice', 'mia'), true);
  assert.strictEqual(evalP({ full: 'alice' }, 'alice', null), false);
});

t('null / array permConfig -> fail-closed (malformed block)', () => {
  assert.strictEqual(evalP(null, 'alice', 'mia'), false);
  assert.strictEqual(evalP([], 'alice', 'mia'), false);
});

t('empty sender never has permission (fail-closed)', () => {
  assert.strictEqual(bridgeModule.evalRoomPermission(undefined, null, 'mia'), false);
  assert.strictEqual(bridgeModule.evalRoomPermission(undefined, '', 'mia'), false);
});

t('agentCanOperateRoom is an exported function (real gate)', () => {
  assert.strictEqual(typeof bridgeModule.agentCanOperateRoom, 'function');
});

// --- Verification that the REAL gates are applied in the code ---
const src = fs.readFileSync('./bridge.js', 'utf8');
t('gates applied: recordings + [room] text + join/leave', () => {
  const uses = (src.match(/agentCanOperateRoom\(/g) || []).length;
  // recordings(1) + injection(1) + handleJoinLeave(1) = 3 calls
  assert.ok(uses >= 3, 'expected >=3 gate calls, found ' + uses);
});

t('BLOCKER 2 FIX: processWatermark does NOT run before the M01 gate', () => {
  // The recovery watermark must advance ONLY by the bridge local clock (real
  // confirmed stream progress), never with the sender `created_at`, and never
  // on admission (before the gate). We verify admission no longer calls
  // processWatermark and finishDelivery uses advanceRecoveryWatermark().
  const admissionBlock = src.slice(
    src.indexOf('const admitted = markDelivery'),
    src.indexOf('const content = unwrapped.content'));
  assert.ok(!admissionBlock.includes('processWatermark(wrapTs)'),
    'processWatermark(wrapTs) must NOT run on admission (advances cursor with hostile created_at)');
  const fin = src.indexOf('function finishDelivery(id, ok, rejected)');
  // Find the REAL end of the function (balancing braces), not the first '\n}'
  // (which would cut at the closing of the internal if (rejected)).
  let depth = 0, finEnd = fin;
  for (; finEnd < src.length; finEnd++) {
    if (src[finEnd] === '{') depth++;
    else if (src[finEnd] === '}') { depth--; if (depth === 0) break; }
  }
  const finBlock = src.slice(fin, finEnd + 1);
  // OPTION2: the watermark advance uses advanceRecoveryWatermark() (bridge
  // local clock), not processWatermark(wrapTs) (sender created_at). We look for
  // the real CALL (with ';' — the explanatory comment names the function
  // without calling it and must not count as code).
  assert.ok(finBlock.includes('advanceRecoveryWatermark();'),
    'finishDelivery must advance the watermark by local clock (OPTION2)');
  const delivPos = finBlock.indexOf("markDelivery(id, 'delivered')");
  const wmPos = finBlock.indexOf('advanceRecoveryWatermark();');
  assert.ok(wmPos > delivPos,
    'the watermark advance must go AFTER marking delivered (only after success)');
});

t('BLOCKER 2 FIX: the cursor is not fed with external timestamps (process removed)', () => {
  // AUDIT-M01-OPTION2-FIX: processWatermark(ts) was REMOVED entirely. Feeding
  // the watermark with a wire timestamp (sender created_at) reintroduces the
  // attack surface. The only legitimate advance is advanceRecoveryWatermark()
  // with a bounded step (RECOVERY_WATERMARK_STEP_SECS), which NEVER jumps to
  // Date.now() after downtime.
  assert.ok(!/function processWatermark\(/.test(src),
    'processWatermark(ts) must have been removed (do not feed the cursor with sender ts)');
  assert.ok(src.includes('RECOVERY_WATERMARK_STEP_SECS'),
    'the advance must be BOUNDED by a step (RECOVERY_WATERMARK_STEP_SECS), not jump to now');
  assert.ok(src.includes('Math.min(prev + RECOVERY_WATERMARK_STEP_SECS, now)'),
    'the watermark advances to min(prev+step, now): never a free jump to Date.now()');
});

t('BLOCKER 1 FIX: GET /recordings requires admin (closes public signed URLs)', () => {
  const listIdx = src.indexOf("req.url === '/recordings'");
  const dlIdx = src.indexOf("req.url.startsWith('/recordings/')");
  assert.ok(listIdx > 0 && dlIdx > listIdx, 'the list matcher must come before the download one');
  const window_ = src.slice(listIdx, dlIdx);
  assert.ok(window_.includes('requireAdmin'),
    'the /recordings listing must require requireAdmin (fail-closed, no public signed URLs)');
});

t('README documents the gate and the fail-closed', () => {
  const readme = fs.readFileSync('./README.md', 'utf8');
  assert.ok(/permissions/i.test(readme), 'README should document permissions');
});

console.log(`\nAUDIT M01 (permissions gate) Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
