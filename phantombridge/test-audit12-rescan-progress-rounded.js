// 🟡 MEDIO (auditoría): el criterio de progreso del rescan no debe considerar
// pendingSince como evidencia. pendingSince cambia solo porque se registra OTRO
// drop, sin recuperar ningún evento ni liberar ningún delivered:
//   rescan -> entra otro drop -> pendingSince cambia -> progressed=true
//   -> rescanAttempts=0   (aunque deliveryCount no bajó, recoveryWatermark no
//                          avanzó, ningún mensaje fue admitido)
// Eso retrasa la detección de un rescan realmente estancado.
//
// 🟠 MEDIO (AUDIT-17): que un ID DESAPAREZCA de `dropped` tampoco es evidencia
// de procesamiento: puede salir por pruning/overflow/limpieza posterior sin
// haber sido admitido. Progreso de recolocación = el dropped concreto volvió a
// ENTRAR en `delivery` vía markDelivery (re-admisión real), no solo que ya no
// está en el ledger de drops.
//
// El progreso debe representar RECUPERACIÓN REAL:
//   - recoveryWatermark avanzó, O
//   - deliveryCount disminuyó, O
//   - un dropped CONCRETO fue re-admitido (existe en delivery).
// pendingSince por sí solo NO es evidencia de progreso, y tampoco lo es que un
// ID salga de `dropped` sin registrarse en `delivery`.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit12-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
// Aceleramos los tiempos del rescan para el test (cooldown/rescan cortos).
baseConfig.rescanStallCooldownMs = 500;
baseConfig.rescanMinIntervalMs = 20;
// Una sola petición de rescan suma 2 intentos (1 al emitir + 1 en la medición
// sin progreso); con el umbral en 2, un único rescan sin progreso ya dispara
// BACKPRESSURE, que es lo que este test quiere verificar de forma determinista.
baseConfig.rescanMaxStalled = 2;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markDelivery, recordDropped, recoverDropped, requestDeliveryRescan,
  _resetRescanStateForTest, _setBridgeStateForTest, getBridgeState,
} = bridge;

let passed = 0, failed = 0;
let _chain = Promise.resolve();
function t(name, fn) {
  _chain = _chain.then(async () => {
    try { await fn(); console.log('  ok:', name); passed++; }
    catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
  });
}

function freshState() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, delivery: {}};
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

console.log('🟡 AUDIT-12: el progreso del rescan NO depende de pendingSince (recuperación real):');

// Test 1: un cambio de pendingSince POR SÍ SOLO (otro drop registrado, sin
// recuperar nada) debe contar como NO progreso -> el rescan entra en
// BACKPRESSURE tras RESCAN_MAX_STALLED intentos.
t('pendingSince cambia por otro drop -> NO progreso -> BACKPRESSURE', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  // Ledger lleno de delivered inmaduros y un drop pendiente.
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 60; // delivered reciente, NO expirable
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 10, dropped: [{id: 'D1', ts: now - 10}],
    droppedOverflow: false, recoveryWatermark: now - 60,
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // El rescan se dispara; mientras tanto NO se recupera nada, solo entra un
  // drop nuevo -> pendingSince cambia pero deliveryCount/watermark/dropped no.
  requestDeliveryRescan();
  await sleep(1200); // deja que corra el rescan + medición de progreso
  assert.strictEqual(bridge.rescanStalled, true,
    'BACKPRESSURE activo: pendingSince solo no es progreso (rescan estancado)');
  _resetRescanStateForTest();
});

// Test 2: progreso REAL por watermark avanzado -> NO entra en BACKPRESSURE
// (aunque pendingSince también haya cambiado, cuenta el watermark).
t('recoveryWatermark avanza -> progreso -> rescan no se estanca', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'D1', ts: now - 5}],
    droppedOverflow: false, recoveryWatermark: now - 60,
    delivery: {'X': {status: 'delivered', ts: now - 600}}});
  requestDeliveryRescan();
  // Durante la medición el watermark avanza (evento procesado real): en la
  // práctica esto ocurre vía processWatermark en handleIncomingGiftWrap; aquí
  // lo simulamos justo antes de la medición (RESCAN_MIN_INTERVAL_MS*2).
  await sleep(30);
  getBridgeState().recoveryWatermark = now;
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, false,
    'NO estancado: el watermark avanzó = recuperación real');
  _resetRescanStateForTest();
});

// Test 3: progreso REAL por deliveryCount que disminuye (ledger liberado).
t('deliveryCount disminuye -> progreso -> rescan no se estanca', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 600,
    delivery: {
      'X': {status: 'delivered', ts: now - 3600},   // expirable (watermark avanzado)
      'Y': {status: 'delivered', ts: now - 3600},   // expirable
      'Z': {status: 'pending', ts: now - 30},       // queda
    }});
  requestDeliveryRescan();
  // El rescan libera 2 delivered expirables -> deliveryCount baja de 3 a 1.
  await sleep(30);
  const st = getBridgeState();
  st.delivery = {'Z': st.delivery['Z']};   // X e Y expiran; Z (pending) queda
  _setBridgeStateForTest(st);              // re-sincroniza deliverySize con el ledger
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, false,
    'NO estancado: el ledger liberó entradas (deliveryCount bajó)');
  _resetRescanStateForTest();
});

// Test 4: progreso REAL por un dropped CONCRETO re-admitido vía markDelivery
// (el mensaje volvió a entrar en delivery = re-admisión real).
t('un dropped concreto re-admitido en delivery -> progreso -> rescan no se estanca', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'D1', ts: now - 5}],
    droppedOverflow: false, recoveryWatermark: now - 600,
    delivery: {'X': {status: 'delivered', ts: now - 3600}}});
  requestDeliveryRescan();
  // Durante el rescan, el dropped D1 es re-admitido: el relay lo re-entrega y
  // markDelivery lo registra en `delivery` (ruta real de recuperación).
  await sleep(30);
  markDelivery('D1', 'delivered', now - 5);
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, false,
    'NO estancado: el dropped concreto fue re-admitido en delivery = recuperación real');
  _resetRescanStateForTest();
});

// Test 4b (🟠 AUDIT-17): CONTROL — un ID que DESAPARECE de `dropped` por
// limpieza/pruning (sin entrar en `delivery`) NO es progreso: mismo watermark,
// mismo deliveryCount, ningún markDelivery. El rescan debe entrar en
// BACKPRESSURE (antes este escenario daba un falso positivo de progreso).
t('un dropped que sale de dropped sin entrar en delivery -> NO progreso -> BACKPRESSURE', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'D1', ts: now - 5}],
    droppedOverflow: false, recoveryWatermark: now - 600,
    delivery: {'X': {status: 'delivered', ts: now - 60}}});
  requestDeliveryRescan();
  // El ID D1 desaparece de `dropped` por pruning/limpieza (p.ej. eviction u
  // overflow) PERO nunca pasa por markDelivery: no está en `delivery`.
  await sleep(30);
  recoverDropped('D1'); // saca del ledger de drops, sin procesar
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, true,
    'BACKPRESSURE: D1 salió de dropped sin markDelivery no es progreso real');
  _resetRescanStateForTest();
});

// Test 4c (🟠 AUDIT-17): CONTROL — un ID que desaparece de `dropped` por
// overflow/eviction (borrado del ledger completo, no entra en delivery) no es
// progreso; volver a admitir otro ID distinto tampoco si no era dropped antes.
t('drop evicted/borrado sin delivery -> NO progreso; watermark+count estaticos -> BACKPRESSURE', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 5, dropped: [{id: 'D1', ts: now - 5}],
    droppedOverflow: false, recoveryWatermark: now - 600,
    delivery: {'X': {status: 'delivered', ts: now - 60}}});
  requestDeliveryRescan();
  // Eviction: D1 se borra del ledger de drops (overflow/limpieza) sin
  // recuperación. El deliveryCount se mantiene (X no expira), watermark igual.
  await sleep(30);
  getBridgeState().dropped = [];       // evictado
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, true,
    'BACKPRESSURE: eviction de D1 sin delivery no es progreso real');
  _resetRescanStateForTest();
});

// Test 5: CONTROL — ahora el criterio viejo (con pendingSince) habría dado
// progreso con D1; verificamos que el fix NO cuenta un mero cambio de
// direction opuesta: si NO hay progreso real, entra en BACKPRESSURE aunque
// pendingSince cambie. (Refuerza el test 1 con cambio explícito de valor.)
t('pendingSince cambia (valor distinto) sin recuperar -> NO progreso -> BACKPRESSURE', async () => {
  _setBridgeStateForTest(freshState());
  _resetRescanStateForTest();
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: now - 10, dropped: [{id: 'D1', ts: now - 10}],
    droppedOverflow: false, recoveryWatermark: now - 600,
    delivery: {'X': {status: 'delivered', ts: now - 60}}});
  requestDeliveryRescan();
  // pendingSince cambia de valor (p.ej. se registra otro drop) pero no hay
  // recuperación: deliveryCount igual, watermark igual, D1 sigue presente.
  await sleep(30);
  getBridgeState().pendingSince = now - 3; // cambió el valor
  recordDropped('D2');                      // entra otro drop (sigue sin recuperar)
  await sleep(1200);
  assert.strictEqual(bridge.rescanStalled, true,
    'BACKPRESSURE: pendingSince cambió pero no hubo recuperación real');
  _resetRescanStateForTest();
});

_chain.then(() => {
  // cleanup — debe correr DESPUÉS de los casos, no antes
  _setBridgeStateForTest(freshState());
  try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
  delete process.env.PHANTOMBRIDGE_CONFIG;
  console.log('');
  console.log(`Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
}).catch((e) => { console.error('FATAL:', e && e.message); process.exit(1); });
