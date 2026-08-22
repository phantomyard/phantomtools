// AUDIT-6 (MEDIO): el delivery ledger NO debe bloquear la admision para
// siempre. Escenario del auditor: DELIVERY_MAX lleno de delivered protegidos
// por watermark (lastSeen congelado por condicion de recuperacion) -> un
// pending nuevo no puede entrar -> fail-closed correcto PERO si lastSeen no
// avanza, es un DoS permanente de admision (aunque no haya perdida ni dup).
//
// El fix: DELIVERY_SOFT_LIMIT + requestDeliveryRescan() — al llegar al
// soft-limit se fuerza limpieza agresiva y, si aun rechaza, se programa un
// re-scan de la suscripcion para que el cursor avance y libere delivered ya
// inalcanzables. Nunca se evicta delivered protegido ni pending vigente.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit6-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markDelivery, deliveryStatus, _setBridgeStateForTest, STATE_FILE,
  getBridgeState, requestDeliveryRescan,
} = bridge;
// backpressureRejected and deliveryRescanNeeded are live getters: they are read
// via bridge.X on each access (destructuring them would freeze them at import).

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function fresh() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}};
}

// We do not export DELIVERY_MAX/SOFT_LIMIT (internal); we measure via behavior.
t('soft-limit: aggressive cleanup does NOT evict protected delivered nor active pending', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  // Fill delivery just under the cap with immature delivered (protected) +
  // 1 active pending.
  const entry = {};
  for (let i = 0; i < 9995; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  entry['p-active'] = {status: 'pending', ts: now - 60}; // active (<24h)
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: entry});
  const admitted = markDelivery('new-1', 'pending');
  // It may admit if there is room; it must not evict protected delivered nor the active pending.
  const st = getBridgeState().delivery;
  assert.strictEqual(st['p-active'].status, 'pending', 'active pending NOT evicted');
  assert.ok(st['d-0'] && st['d-0'].status === 'delivered', 'protected delivered NOT evicted');
  void admitted;
});

t('re-scan: markDelivery rejected when full schedules re-scan (flag activates)', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  // Fill entirely with immature delivered -> new pending does NOT fit.
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: entry});
  const beforeCount = bridge.backpressureRejected;
  const admitted = markDelivery('stuck-1', 'pending');
  assert.strictEqual(admitted, false, 'admission rejected (fail-closed)');
  assert.ok(bridge.deliveryRescanNeeded, 're-scan requested to unblock the cursor');
  const afterCount = getBridgeState(); void afterCount;
  assert.ok(bridge.backpressureRejected > beforeCount, 'contador de backpressure incrementado');
});

t('re-scan: requestDeliveryRescan is invocable and does not throw if no connection', () => {
  // En tests no hay subscribeIncoming corriendo -> reconnectIncoming sigue null;
  // requestDeliveryRescan debe completar sin lanzar (warning, no crash).
  requestDeliveryRescan();
  // We only require no exception; the flag marks that it was requested.
  assert.ok(true, 'requestDeliveryRescan does not throw without connection');
});

// cleanup
_setBridgeStateForTest(fresh());
bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
