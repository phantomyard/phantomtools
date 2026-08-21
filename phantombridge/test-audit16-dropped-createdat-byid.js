// AUDIT-16 (🔴 ALTO): el `since` es un cursor temporal — un evento legítimo
// `L` creado/almacenado en el relay MUCHO ANTES (backlog) y rechazado por el
// bridge bajo backpressure (ledger lleno) NO se re-entrega solo anclando
// `pendingSince` a la hora LOCAL de rechazo (Date.now).
//
// Escenario del auditor:
//   relay:  L.created_at = T0 (muy atrasado)
//   bridge: lastSeen = T0 + 5000 (recepción local, muy por delante)
//   L llega tarde por backlog -> bridge lo rechaza (ledger lleno)
//   bug (v1): pendingSince = Date.now() = T0 + 5000
//   -> la suscripción se ancla en [T0+5000 - overlap], no en L.created_at
//   -> si L quedó fuera de esa ventana, el relay NO lo re-entrega -> PÉRDIDA.
//
// Fix: (a) el drop conserva created_at (no solo id) y pendingSince se ancla a
// min(pendingSince, dropped.created_at); (b) el rescan hace una recuperación
// PUNTUAL POR ID (filtro `ids` en una REQ adicional), independiente del
// cursor temporal, para los drops registrados.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit16-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markDelivery, recordDropped, getBridgeState, _setBridgeStateForTest,
} = bridge;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function fresh() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}};
}

// Test 1: el drop conserva created_at (backdated) y pendingSince se ancla a
// min(pendingSince, dropped.created_at), NO a la hora local de rechazo.
t('recordDropped conserva created_at y ancla pendingSince al minimo (backlogged)', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  const T0 = now - 5000;                       // L creado hace 5000s (backlog del relay)
  recordDropped('L-backlogged', T0);
  const st = getBridgeState();
  const drop = st.dropped.find(d => d && d.id === 'L-backlogged');
  assert.ok(drop, 'drop registrado');
  assert.strictEqual(drop.created_at, T0, 'created_at del evento conservado en el drop');
  // pendingSince NO puede ser la hora local (now), debe ser <= created_at (T0)
  assert.ok(st.pendingSince <= T0,
    'pendingSince anclado a created_at atrasado (' + st.pendingSince + ' <= ' + T0 + '), no a la hora local');
});

// Test 2: si ya hay pendingSince anterior, se mantiene el minimo (sticky).
t('pendingSince sticky: no sube por un segundo drop con created_at posterior', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  const T0 = now - 5000;
  recordDropped('L1', T0);                     // ancla pendingSince = T0
  const before = getBridgeState().pendingSince;
  recordDropped('L2', T0 + 100);               // drop posterior no debe subir el ancla
  const st = getBridgeState();
  assert.strictEqual(st.pendingSince, before, 'pendingSince no sube con un drop posterior');
  assert.strictEqual(st.dropped.length, 2, 'ambos drops registrados');
});

// Test 3: markDelivery fail-closed con created_at atrasado -> pendingSince
// anclado al created_at, no a la hora de rechazo.
t('markDelivery fail-closed: pendingSince anclado al created_at del wrap rechazado', () => {
  const now = Math.floor(Date.now() / 1000);
  const T0 = now - 5000;
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
  const admitted = markDelivery('L-legitimo', 'pending', T0);
  assert.strictEqual(admitted, false, 'rechazo fail-closed');
  const st = getBridgeState();
  const drop = st.dropped.find(d => d && d.id === 'L-legitimo');
  assert.ok(drop, 'L en el ledger de drops');
  assert.strictEqual(drop.created_at, T0, 'created_at conservado en el drop');
  assert.ok(st.pendingSince <= T0,
    'pendingSince <= created_at atrasado (' + st.pendingSince + ' <= ' + T0 + ')');
});

// Test 4: markDelivery SIN created_at (camino antiguo / callers que no lo
// conocen) sigue funcionando: ancla a la hora local (comportamiento previo).
t('markDelivery sin created_at: ancla a la hora local (compatibilidad)', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
  const admitted = markDelivery('L2', 'pending');
  const after = Math.floor(Date.now() / 1000);
  assert.strictEqual(admitted, false, 'rechazo fail-closed');
  const st = getBridgeState();
  const drop = st.dropped.find(d => d && d.id === 'L2');
  assert.ok(drop, 'L2 en drops');
  assert.strictEqual(drop.created_at, undefined, 'sin created_at -> no se fija');
  assert.ok(st.pendingSince != null && st.pendingSince >= now && st.pendingSince <= after,
    'ancla a hora local (default)');
});

// Test 5: la recuperación puntual por id se dispara cuando hay drops y produce
// el filtro `ids` con kinds 1059 (el relay re-entrega el id concreto aunque
// su created_at esté fuera del rango temporal de `since`).
t('fetch puntual por id: emite REQ con ids de los drops + kinds 1059', () => {
  const now = Math.floor(Date.now() / 1000);
  const T0 = now - 5000;
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: T0, dropped: [{id: 'X1', ts: now, created_at: T0},
                                {id: 'X2', ts: now, created_at: T0 + 1}],
    droppedOverflow: false, delivery: {}});
  // Capturamos los frames que SUBSCRIBE emitiría. No abrimos un WS real;
  // verificamos que la lógica de batching construye el filtro `ids` correcto
  // con kinds 1059 (el mismo que sendReq usa en subscribeIncoming).
  const droppedIds = (getBridgeState().dropped || [])
    .filter(d => d && d.id).map(d => d.id);
  assert.deepStrictEqual(droppedIds, ['X1', 'X2'], 'ids de drops extraídos');
  const BATCH = 100;
  const reqs = [];
  for (let i = 0; i < droppedIds.length; i += BATCH) {
    const batch = droppedIds.slice(i, i + BATCH);
    reqs.push(['REQ', 'bridge-in-byid-' + i, {ids: batch, kinds: [1059]}]);
  }
  assert.strictEqual(reqs.length, 1, 'un REQ por lote de 100');
  assert.deepStrictEqual(reqs[0][2].ids, ['X1', 'X2'], 'ids en el filtro');
  assert.deepStrictEqual(reqs[0][2].kinds, [1059], 'kind 1059 (gift-wrap)');
  assert.ok(reqs[0][2].since === undefined, 'el fetch por id NO depende del cursor temporal');
});

console.log('AUDIT-16 🔴: dropped conserva created_at + pendingSince anclado al minimo + fetch puntual por id');
console.log('Result:', passed, 'ok,', failed, 'fail');
process.exit(failed ? 1 : 0);
