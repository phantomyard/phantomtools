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
// backpressureRejected y deliveryRescanNeeded son getters vivos: se leen
// via bridge.X en cada acceso (desestructurarlos los congelaría en el import).

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function fresh() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}};
}

// NO exportamos DELIVERY_MAX/SOFT_LIMIT (internos); medimos via comportamiento.
t('soft-limit: limpieza agresiva NO evicta delivered protegido ni pending vigente', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  // Llenar delivery justo bajo el cap con delivered inmaduros (protegidos) +
  // 1 pending vigente.
  const entry = {};
  for (let i = 0; i < 9995; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  entry['p-active'] = {status: 'pending', ts: now - 60}; // vigente (<24h)
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: entry});
  const admitted = markDelivery('new-1', 'pending');
  // Puede admitir si hay sitio; no debe expulsar delivered protegido ni el pending vigente.
  const st = getBridgeState().delivery;
  assert.strictEqual(st['p-active'].status, 'pending', 'pending vigente NO expulsado');
  assert.ok(st['d-0'] && st['d-0'].status === 'delivered', 'delivered protegido NO expulsado');
  void admitted;
});

t('re-scan: markDelivery rechazada por lleno programa re-scan (flag se activa)', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  // Llenar del todo con delivered inmaduros -> pending nuevo NO cabe.
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: entry});
  const beforeCount = bridge.backpressureRejected;
  const admitted = markDelivery('stuck-1', 'pending');
  assert.strictEqual(admitted, false, 'admisión rechazada (fail-closed)');
  assert.ok(bridge.deliveryRescanNeeded, 'se solicita re-scan para desatascar el cursor');
  const afterCount = getBridgeState(); void afterCount;
  assert.ok(bridge.backpressureRejected > beforeCount, 'contador de backpressure incrementado');
});

t('re-scan: requestDeliveryRescan es invocable y no lanza si no hay conexion', () => {
  // En tests no hay subscribeIncoming corriendo -> reconnectIncoming sigue null;
  // requestDeliveryRescan debe completar sin lanzar (warning, no crash).
  requestDeliveryRescan();
  // Solo pedimos que no haya excepción; el flag marca que se solicitó.
  assert.ok(true, 'requestDeliveryRescan no lanza sin conexion');
});

// cleanup
_setBridgeStateForTest(fresh());
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
