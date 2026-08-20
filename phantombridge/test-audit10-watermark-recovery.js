// AUDIT-10 (ALTO, raíz): separación de cursores — recepción cruda vs watermark
// de recuperación.
//
// El ALTO-7 vino de mezclar dos semánticas incompatibles en lastSeen:
//   - "último evento RECIBIDO" (avanza con cada frame, incluso rechazados/
//     no-admitidos/descartados) — lo usa el `since` de la suscripción.
//   - "watermark que demuestra que los eventos anteriores ya no pueden
//     reaparecer" — lo usaba deliveredCanExpire() para borrar delivered.
//
// Con Fase 2, deliveredCanExpire() NO debe depender de lastSeen (recepción
// cruda) sino de recoveryWatermark, que SOLO avanza cuando un evento se
// PROCESA/ADMITE de verdad (markDelivery exitoso). Un frame recibido pero
// rechazado/no-admitido NUNCA mueve recoveryWatermark, por mucho que avance
// lastSeen.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit10-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  updateLastSeen, markDelivery, deliveryStatus, advanceRecoveryWatermark,
  getBridgeState, _setBridgeStateForTest,
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

console.log('AUDIT-10: separación receiveCursor (lastSeen) vs recoveryWatermark:');

// Test 1: recepción cruda (updateLastSeen) avanza lastSeen PERO NO recoveryWatermark.
t('updateLastSeen (recepción) NO avanza el watermark de recuperación', () => {
  _setBridgeStateForTest(freshState());
  const before = bridge.recoveryWatermark;
  // Simula una ráfaga de frames recibidos (autorizados o no): cada frame
  // llama a updateLastSeen(0) antes de autenticar/admitir (exactamente el
  // patrón del handler). lastSeen avanza, recoveryWatermark NO debe.
  updateLastSeen(0);
  updateLastSeen(0);
  updateLastSeen(0);
  const st = getBridgeState();
  assert.ok(st.lastSeen > 0, 'lastSeen (receiveCursor) avanzó con la recepción');
  assert.strictEqual(st.recoveryWatermark, before,
    'recoveryWatermark NO avanza por recepción cruda (solo por procesamiento real)');
});

// Test 2: el delivered NUNCA se borra por lastSeen inflado por no-admitidos.
// Escenario ALTO-7: ráfaga de no-admitidos avanza lastSeen muy por delante de
// un delivered X; con la semántica antigua, deliveredCanExpire(X, lastSeen)
// lo borraba (break exactly-once). Con Fase 2, deliveredCanExpire usa
// recoveryWatermark que NO avanzó -> X se conserva.
t('delivered sobrevive a lastSeen inflado por no-admitidos (watermark intacto)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 60; // delivered entregado hace 1 min
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0,
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // Ráfaga de no-admitidos: lastSeen avanza MÁS de 240s (+120+120 margen)
  // por delante de X (el relay devuelve tráfico que el bridge no admite), pero
  // recoveryWatermark sigue en 0 (nada procesado todavía).
  const st = getBridgeState();
  st.lastSeen = tX + 600; // recepción cruda muy por delante de X
  // Cualquier markDelivery posterior dispara evictDeliveryLedger.
  markDelivery('Y', 'pending');
  // X NO debe expirar: el watermark de recuperación no avanzó.
  assert.strictEqual(deliveryStatus('X'), 'delivered',
    'delivered X se conserva aunque lastSeen avanzo (solo el watermark REAL lo expira)');
});

// Test 3: SOLO el procesamiento real confirmado (evento/rango que el relay ha
// recorrido, vía advanceRecoveryWatermark) avanza el watermark y es entonces
// (y solo entonces) cuando un delivered viejo puede expirar. El avance es
// INCREMENTAL y ACOTADO (nunca un salto libre a Date.now()): un evento real
// confirma a lo sumo un paso de backlog (RECOVERY_WATERMARK_STEP_SECS). Tras
// un downtime el watermark progresa poco a poco con la ráfaga de backlog.
t('advanceRecoveryWatermark (evento confirmado) avanza el watermark -> delivered viejo expira', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 600; // delivered entregado hace 10 min
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 600, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    recoveryWatermark: tX - 1, // watermark justo antes de X (X aún inalcanzable tras 1 paso)
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // Un evento REAL procesado avanza el watermark un paso. Con un paso de 300s
  // y tX a 599s detrás del watermark previo+paso, X queda dentro del solapado
  // (no expira aún con 1 solo evento — correcto: el relay no ha recorrido más).
  const wmBefore = bridge.recoveryWatermark;
  advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark > wmBefore, 'watermark avanza con el evento confirmado');
  // Procesamos suficientes eventos (ráfaga) para que el watermark supere X+overlap.
  for (let i = 0; i < 5; i++) advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark >= tX + 120 + 120,
    'tras la ráfaga el watermark supera la ventana de X');
  assert.ok(markDelivery('nuevo-1', 'pending'), 'admision exitosa');
  assert.strictEqual(deliveryStatus('X'), null,
    'delivered viejo expira SOLO cuando el watermark de recuperacion lo supera');
});

// Test 4: markDelivery por sí solo (admisión interna) NO avanza el watermark
// si no hubo watermark previo — tras un downtime el relay no ha confirmado
// haber recorrido el rango, y una admisión interna no debe expirar delivered
// legítimos (break exactly-once).
t('markDelivery sin watermark previo NO expira delivered (downtime)', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 3600; // delivered entregado hace 1h
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: tX, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, // nunca procesado confirmado
    delivery: {'X': {status: 'delivered', ts: tX}}});
  markDelivery('Y', 'pending');
  assert.strictEqual(deliveryStatus('X'), 'delivered',
    'delivered sobrevive: el watermark sigue en 0 (sin evento confirmado) tras el downtime');
});

// Test 5: advanceRecoveryWatermark es un helper que avanza el watermark de
// forma INCREMENTAL ACOTADA. No da un salto libre a Date.now() tras downtime
// (rompería exactly-once), pero sí puede establecer el primer watermark desde
// 0 con un paso pequeño (arranque frío conservador).
t('advanceRecoveryWatermark: avance incremental acotado, nunca salto libre a now', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  assert.strictEqual(bridge.recoveryWatermark, 0, 'empieza en 0');
  // Desde 0, un solo evento establece un watermark ACOTADO (paso), no now.
  advanceRecoveryWatermark();
  const w0 = bridge.recoveryWatermark;
  assert.ok(w0 > 0, 'establece un watermark desde 0 (paso acotado de arranque)');
  assert.ok(w0 < now, 'el primer establecimiento NO salta a now (acotado al paso)');
  // Cada evento posterior suma como mucho un paso; nunca supera el reloj local.
  const step = 300; // RECOVERY_WATERMARK_STEP_SECS default
  for (let i = 0; i < 200; i++) advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark <= now, 'monotónico y nunca supera now');
  // Un evento aislado tras downtime (prev far detrás de now) avanza SOLO un paso.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 30 * 24 * 3600, // hace 30 días
    delivery: {}});
  const prev = bridge.recoveryWatermark;
  advanceRecoveryWatermark(); // un solo evento procesado
  assert.strictEqual(bridge.recoveryWatermark, prev + step,
    'un evento tras 30 días de downtime avanza EXACTAMENTE un paso, no hasta now');
});

// cleanup
_setBridgeStateForTest(freshState());
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
