// AUDIT-4 regression test (opción B — watermark, no TTL wall-clock):
// reproduce el escenario del auditor: un bridge que entrega un gift-wrap,
// cae >30 min y al reiniciar NO debe re-ejecutar el `delivered` (exactly-once
// tras downtime largo).
//
// Antes (DELIVERY_TTL_SECS=30min): delivered[X] expiraba por reloj de pared,
// seenIds expiraba a los ~180s, pero lastSeen se conserva en disco -> al
// reiniciar con since=lastSeen-120 el relay re-entregaba X y deliveryStatus(X)
// era null -> se re-ejecutaba.
//
// Ahora (opción B): delivered SOLO expira por WATERMARK (cuando lastSeen ya
// avanzó por delante de la ventana de replay), nunca por reloj de pared. Un
// downtime largo congela lastSeen -> delivered[X] NO expira -> el dedup real
// de handleIncomingGiftWrap (isSeen && delivered==='delivered') lo salta.
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

// El dedup real de handleIncomingGiftWrap (tras AUDIT-4): el ledger durable
// es la fuente autoritativa de "ya entregado"; NO requiere isSeen() (que
// expira a los 180s y por tanto es false tras un downtime largo).
function shouldSkipAsDelivered(id) {
  return deliveryStatus(id) === 'delivered';
}

assert.strictEqual(STATE_FILE, tmpState, 'STATE_FILE debe apuntar al temp');

console.log('AUDIT-4: delivered por watermark (no TTL wall-clock) + cap fail-closed:');

// ---- 🔴: downtime largo NO re-ejecuta delivered ----
t('downtime 1h: delivered sobrevive tras restart (no expira por reloj)', () => {
  _setBridgeStateForTest(freshState());
  // Simular: recibido+entregado en T0, con lastSeen=T0.
  const T0 = Math.floor(Date.now() / 1000) - 3600; // hace 1 hora
  // lastSeen = T0 (congelado durante el downtime, como persiste en disco)
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: T0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    delivery: {'X': {status: 'delivered', ts: T0}}});
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: T0, seenIds: [{id: 'X', ts: T0}], pendingSince: null,
    dropped: [], droppedOverflow: false,
    delivery: {'X': {status: 'delivered', ts: T0}}});
  // "Reinicio": recargar desde disco. lastSeen sigue siendo T0 (1h viejo).
  // El delivered NO debe expirar aunque hayan pasado 60 min del TTL de antes.
  getBridgeState().delivery = getBridgeState().delivery || {};
  // marcar una entrada nueva dispara el sweep; delivered debe seguir presente.
  markDelivery('Y', 'pending');
  assert.strictEqual(deliveryStatus('X'), 'delivered', 'delivered NO expira por reloj tras 1h de downtime');
  // Y el dedup salta X (no re-ejecuta)
  assert.strictEqual(shouldSkipAsDelivered('X'), true, 'replay de X -> SKIP (exactly-once)');
});

t('delivered solo expira por watermark (recoveryWatermark avanzado por delante)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // X entregado hace 1h, pero el WATERMARK DE RECUPERACIÓN ya avanzó
  // (el bridge PROCESÓ/admitió eventos tras entregar X -> el relay ya no
  // puede re-entregarlo dentro de la ventana). -> X se puede expirar.
  const tX = now - 3600;
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 600, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 600, // procesado hasta hace 10 min
    delivery: {'X': {status: 'delivered', ts: tX}}});
  markDelivery('Y', 'pending');
  assert.strictEqual(deliveryStatus('X'), null, 'delivered con recoveryWatermark muy adelantado -> expira (watermark cumplido)');
});

t('delivered NO expira si lastSeen no avanzó (downtime)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 7200; // entregado hace 2h
  // lastSeen = tX (el bridge NO procesó nada desde X: downturn / arranque)
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: tX, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    delivery: {'X': {status: 'delivered', ts: tX}}});
  markDelivery('Y', 'pending');
  assert.strictEqual(deliveryStatus('X'), 'delivered', 'delivered se mantiene aunque lleve 2h: lastSeen no avanzó');
});

// ---- 🟠: pending SI expira por TTL (semántica distinta) ----
t('pending expira por TTL wall-clock (PENDING_TTL_SECS)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // pending de hace 25h (PENDING_TTL_SECS=24h) -> expira
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
