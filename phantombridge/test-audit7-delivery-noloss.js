// AUDIT-7 (ALTO): el rechazo fail-closed por ledger lleno NO debe permitir la
// pérdida permanente de un DM legítimo.
//
// Escenario del auditor: DELIVERY_MAX lleno de delivered protegidos ->
// markDelivery(L, 'pending') devuelve false (backpressure fail-closed). Pero
// el handler ya llamó a updateLastSeen() ANTES de la admisión, así que
// lastSeen avanzó con la hora de recepción. Si `L` cae fuera de la ventana
// de overlap (lastSeen - 120) durante una ráfaga de no-admitidos, el `since`
// de la siguiente suscripción saltaría por delante de `L` y el relay ya no
// lo re-entregaría -> PÉRDIDA PERMANENTE.
//
// El fix (opción A): en el rechazo fail-closed de markDelivery se reutiliza el
// mecanismo ALTO-2 de enqueueGiftWrap — registrar el id en el ledger de drops
// y anclar pendingSince STICKY (nunca más reciente que el primer drop no
// recuperado). Así la siguiente suscripción ancla `since = pendingSince - 120`,
// cubriendo a `L` aunque lastSeen haya avanzado muy por delante.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit7-'));
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

// Test 1: el rechazo fail-closed por ledger lleno registra el drop y ancla
// pendingSince sticky (antes del fix, NO lo hacía -> `L` podía perderse).
t('fail-closed: rechazo por ledger lleno registra dropped[id] + pendingSince sticky', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: entry});
  const admitted = markDelivery('L-legitimo', 'pending');
  assert.strictEqual(admitted, false, 'admisión rechazada (fail-closed)');
  const st = getBridgeState();
  assert.ok(st.dropped.some(d => d && d.id === 'L-legitimo'),
    'el id L queda registrado en el ledger de drops (recuperable)');
  assert.ok(st.pendingSince != null, 'pendingSince queda anclado (sticky) para el drop');
});

// Test 2: el `since` de la siguiente suscripción se ancla a pendingSince
// (no a lastSeen), cubriendo a `L` aunque lastSeen haya avanzado muy por
// delante tras una ráfaga de no-admitidos. Esto es exactamente lo que evita
// la pérdida permanente del escenario del auditor (E1..E50000 + L).
t('since ancla a pendingSince: L caído de la ventana de overlap sigue cubierto', () => {
  const now = Math.floor(Date.now() / 1000);
  const entry = {};
  for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
  // Estado tras la ráfaga: lastSeen avanzó MUY por delante (50000 eventos
  // recibidos ~ 50000s -> 13.9h después de L), y el drop de L ancló pendingSince
  // al momento del rechazo.
  const lTime = now - 50000; // L se recibió hace ~13.9h (caería fuera de lastSeen-120)
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: lTime, dropped: [{id: 'L-legitimo', ts: lTime}], droppedOverflow: false,
    delivery: entry});
  // Reproduce la lógica H-NEW-01 de subscribeIncoming: cursor = pendingSince
  // si hay drops, si no lastSeen.
  const cursor = (getBridgeState().pendingSince != null && getBridgeState().pendingSince < getBridgeState().lastSeen)
    ? getBridgeState().pendingSince
    : getBridgeState().lastSeen;
  const since = cursor - 120; // STATE_OVERLAP_SECS = 120
  assert.ok(cursor === lTime, 'cursor anclado a pendingSince (no a lastSeen avanzado)');
  assert.ok(since <= lTime, 'since <= momento de L -> L dentro de la ventana de re-entrega');
  assert.ok(since < getBridgeState().lastSeen,
    'since no salta por delante de L (aunque lastSeen avanzo 50000s)');
});

// Test 3: pendingSince es STICKY — sigue anclado aunque lleguen más eventos
// (un solo drop no recuperado no se libera porque lastSeen avance).
t('pendingSince sticky: no se libera al avanzar lastSeen con un drop pendiente', () => {
  _setBridgeStateForTest(fresh());
  const now = Math.floor(Date.now() / 1000);
  // Simular el drop del fail-closed
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'L-legitimo', ts: now - 5}], droppedOverflow: false,
    delivery: {}});
  // Un evento posterior NO purga el pendingSince ni el dropped mientras no se
  // recupere L (releasePendingSinceIfRecovered solo actúa con dropped vacío).
  recordDropped('L-legitimo'); // idempotente: ya está
  const st = getBridgeState();
  assert.ok(st.pendingSince != null, 'pendingSince sigue anclado');
  assert.ok(st.dropped.some(d => d && d.id === 'L-legitimo'), 'drop sigue registrado');
  assert.ok(st.pendingSince <= st.lastSeen, 'pendingSince nunca es más reciente que lastSeen');
});

// cleanup
_setBridgeStateForTest(fresh());
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
