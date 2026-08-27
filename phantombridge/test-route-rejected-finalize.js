// 🟡 BAJO (audit): handleRoute() must finalize DETERMINISTIC rejections.
//
// The event was already admitted as `pending` by handleIncomingGiftWrap()
// before calling handleRoute(). If handleRoute() does a bare `return` on a
// rejection that the retry will NEVER change (non-existent agent, no
// permission, blocked anti-loop), the `pending` entry keeps consuming the
// ledger until PENDING_TTL_SECS — useless retries that will never change the
// result.
//
// Fix: in those three paths finishDelivery(giftWrapId, false, true) is called
// (rejected=true) -> the `pending` entry is removed from the ledger.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'route-rej-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  handleRoute, markDelivery, deliveryStatus, _setBridgeStateForTest,
} = bridge;

let passed = 0, failed = 0;
let _chain = Promise.resolve();
function t(name, fn) {
  _chain = _chain.then(async () => {
    try { await fn(); console.log('  ok:', name); passed++; }
    catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
  });
}

// Base ledger state; we admit a `pending` like
// handleIncomingGiftWrap() would before delegating to handleRoute().
function freshLedger() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, delivery: {}};
}

// handleRoute fires publishDM to agents; we do not want real network. We are
// going to test the THREE deterministic rejection paths that `return` before
// reaching publishDM, so no publishDM mock is needed unless the path reaches
// the publish — which is not the case. We still trap any network failure with
// try/catch in the harness (publishDM is caught with .catch).
const UNKNOWN_FROM = 'remitente desconocido';

console.log('🟡 handleRoute: deterministic rejections finalize the pending (rejected):');

// 1) Unknown agent: route.to does not exist in CONFIG.agents.
t('non-existent agent -> finishDelivery rejected -> pending removed', async () => {
  _setBridgeStateForTest(freshLedger());
  // Admit the pending as the real handler would.
  assert.ok(markDelivery('gw-1', 'pending'), 'admitted pending');
  const fromPk = 'abc'.padEnd(64, '0');
  // @non-existent-agent: not in CONFIG.agents -> toPk undefined.
  const route = {to: 'agente-inexistente', text: 'hola'};
  // We wrap so a possible failed publishDM does not break the assert.
  await handleRoute(UNKNOWN_FROM, fromPk, route, 'gw-1').catch(() => {});
  assert.strictEqual(deliveryStatus('gw-1'), null,
    'pending removed after non-existent agent (rejected)');
});

// 2) No permission: routingAllowed() == false (default deny and no rule).
t('no permission (routingAllowed false) -> finishDelivery rejected -> pending removed', async () => {
  _setBridgeStateForTest(freshLedger());
  // We pick a pair with no permission rule and default deny: from -> to denied.
  // The real CONFIG.agents has the sender and the @to; the from is not in
  // routing.permissions and default deny -> routingAllowed false.
  assert.ok(markDelivery('gw-2', 'pending'), 'admitted pending');
  const fromPk = 'def'.padEnd(64, '0');
  // We take a @to that DOES exist but whose from has no permission.
  const toName = Object.keys(bridge.CONFIG.agents || {})[0] || 'dave';
  const route = {to: toName, text: 'hola'};
  await handleRoute(UNKNOWN_FROM, fromPk, route, 'gw-2').catch(() => {});
  assert.strictEqual(deliveryStatus('gw-2'), null,
    'pending removed after no permission (rejected)');
});

// 3) Blocked anti-loop: antiLoopCheck() == false (same duplicated content).
t('blocked anti-loop -> finishDelivery rejected -> pending removed', async () => {
  _setBridgeStateForTest(freshLedger());
  assert.ok(markDelivery('gw-3', 'pending'), 'admitted pending');
  const fromPk = '123'.padEnd(64, '0');
  const toName = Object.keys(bridge.CONFIG.agents || {})[0] || 'dave';
  const route = {to: toName, text: '@' + toName + ' duplicado-para-antiloop'};
  // Run the same text twice: the second time antiLoopCheck must block (content
  // dedup) within the window -> rejected.
  await handleRoute(UNKNOWN_FROM, fromPk, route, 'gw-3').catch(() => {});
  // Admit another pending for the second attempt with the SAME content.
  _setBridgeStateForTest(freshLedger());
  assert.ok(markDelivery('gw-4', 'pending'), 'admitted pending 2');
  await handleRoute(UNKNOWN_FROM, fromPk, route, 'gw-4').catch(() => {});
  assert.strictEqual(deliveryStatus('gw-4'), null,
    'pending removed after anti-loop (rejected)');
});

// 4) CONTROL: a legitimate route (with permission) must NOT leave a pending nor
//    delete anything incorrectly — when publishDM fails by network the
//    pending is kept (legitimate retry) or marked. Here we only verify that
//    rejected is NOT triggered on a NON-deterministic denial path (the real
//    publish is not relevant). We ensure the fix does not break the normal
//    flow: with a granted permission, handleRoute does NOT call
//    finishDelivery(rejected).
t('control: routing with permission does NOT trigger rejected', async () => {
  _setBridgeStateForTest(freshLedger());
  const toName = Object.keys(bridge.CONFIG.agents || {})[0] || 'dave';
  // We assume the first permissive agent can write to itself no —
  // better: we use a from that DOES have permission (if it exists in
  // routing.perms).
  const perms = (bridge.CONFIG.routing && bridge.CONFIG.routing.permissions) || {};
  const grantedFrom = Object.keys(perms).find(f =>
    (perms[f] || []).some(t => t === toName || t === '*'));
  if (!grantedFrom) {
    console.log('  (skip) no from with permission in config for the control');
    return;
  }
  const fromPk = bridge.CONFIG.agents[grantedFrom];
  assert.ok(markDelivery('gw-5', 'pending'), 'admitted pending');
  const route = {to: toName, text: 'control legitimo'};
  // publishDM will fail by network (no relay) -> it is caught; the important
  // thing: the pending must NOT be removed by a deterministic rejection (stays
  // pending).
  await handleRoute(grantedFrom, fromPk, route, 'gw-5').catch(() => {});
  // With permission, the flow reaches publishDM (fails by network) -> pending
  // is kept (legitimate retry) or becomes rejected only if the handler decided
  // so.
  const st = deliveryStatus('gw-5');
  // We accept 'pending' (legitimate retry, failed publish) — never a
  // deterministic permission rejection.
  assert.ok(st === 'pending', 'with permission the pending is kept (legitimate retry)');
});

_chain.then(() => {
  // cleanup — must run AFTER the cases, not before
  _setBridgeStateForTest(freshLedger());
  bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
  try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
  delete process.env.PHANTOMBRIDGE_CONFIG;
  console.log('');
  console.log(`Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
}).catch((e) => { console.error('FATAL:', e && e.message); process.exit(1); });
