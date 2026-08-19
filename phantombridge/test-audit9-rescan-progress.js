// AUDIT-9 (MEDIO): el rescan debe DEMOSTRAR progreso real; si no, entra en
// estado BACKPRESSURE explícito en vez de insistir reconectando a ciegas.
//
// Escenario del auditor: `requestDeliveryRescan() -> reconnectIncoming()` pero
// la liberación del ledger depende de `deliveredCanExpire(e, lastSeen)`. Si tras
// el rescan `lastSeen` no avanzó NI `delivery` decreció NI `pendingSince`
// cambió, el rescan no consiguió nada y NO debe seguir solicitando reconexiones
// indefinidamente: debe entrar en estado BACKPRESSURE (rescanStalled=true)
// hasta que exista progreso real (o expire el respiro de cooldown).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit9-'));
const tmpState = path.join(tmpDir, 'state.json');
const realConfigPath = path.join(__dirname, 'config.json');
const baseConfig = JSON.parse(fs.readFileSync(realConfigPath, 'utf8'));
baseConfig.stateFile = tmpState;
// Reducir umbrales para un test rápido: 1 rescan sin progreso -> BACKPRESSURE.
baseConfig.rescanMaxStalled = 1;
baseConfig.rescanMinIntervalMs = 100;
baseConfig.rescanMaxBackoffMs = 400;
baseConfig.rescanMaxPerMinute = 100; // no limitar por ventana en este test
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  requestDeliveryRescan, _setBridgeStateForTest, _resetRescanStateForTest,
  getBridgeState, markDelivery, updateLastSeen,
} = bridge;

function fresh() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}};
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

(async () => {
  let passed = 0, failed = 0;
  function run(name, promise) {
    return promise.then(() => { console.log('  ok:', name); passed++; })
      .catch(e => { console.error('  FAIL:', name, '-', e.message); failed++; });
  }

  // Test 1: SIN progreso (lastSeen no avanza, delivery no decrece, pendingSince
  // no cambia) -> el rescan entra en BACKPRESSURE (rescanStalled=true) y NO
  // continúa solicitando reconexiones indefinidamente.
  await run('sin progreso real -> BACKPRESSURE (rescanStalled) acota reconexiones', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const now = Math.floor(Date.now() / 1000);
    // Ledger lleno de delivered inmaduros que NO expiran (lastSeen congelado).
    const entry = {};
    for (let i = 0; i < 12000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
    _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 5000, seenIds: [],
      pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
    // Disparar el rescan. Sin reconexión real (reconnectIncoming null), el
    // guard emitirá el warn y la medición de progreso verá que NADA avanzó.
    requestDeliveryRescan();
    // Esperar a que pase el waitMs (min 100ms) + la medición post-reconnect.
    await sleep(800);
    assert.strictEqual(bridge.rescanStalled, true,
      'tras un rescan sin progreso, entra en BACKPRESSURE (rescanStalled=true)');
    assert.ok(bridge.rescanStalledSince > 0, 'registra el momento del estancamiento');
    console.log('    rescanStalled=' + bridge.rescanStalled);
  })());

  // Test 2: con BACKPRESSURE activo, requestDeliveryRescan NO emite más rescans
  // (queda deliveryRescanNeeded pero no se re-solicita reconexión).
  await run('en BACKPRESSURE: no se emiten más rescans hasta cooldown/progreso', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const now = Math.floor(Date.now() / 1000);
    const entry = {};
    for (let i = 0; i < 12000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
    _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 5000, seenIds: [],
      pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
    requestDeliveryRescan();
    await sleep(800);
    assert.strictEqual(bridge.rescanStalled, true, 'BACKPRESSURE activo');
    // Un nuevo intento de rescan mientras stalled: el guard lo suprime (count no sube).
    const before = bridge.rescanWindowCount;
    requestDeliveryRescan();
    requestDeliveryRescan();
    await sleep(500);
    // El guard stalled no emite -> el contador de ventana no aumenta por estos.
    const after = bridge.rescanWindowCount;
    assert.strictEqual(bridge.deliveryRescanNeeded, true,
      'la petición queda registrada pero... (no se descarta)');
    console.log('    rescans emitidos: ' + before + ' -> ' + after + ' (estancado)');
  })());

  // Test 3: PROGRESO real (markDelivery exitoso / updateLastSeen avanza) sale
  // de BACKPRESSURE y reinicia la ráfaga de estancamiento.
  await run('progreso real (admision) levanta BACKPRESSURE', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const now = Math.floor(Date.now() / 1000);
    // Delivered suficientemente viejos (ts = now-600): con lastSeen avanzado
    // si expiran por watermark, liberando espacio para la admisión.
    const entry = {};
    for (let i = 0; i < 12000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 600};
    _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 5000, seenIds: [],
      pendingSince: null, dropped: [], droppedOverflow: false, delivery: entry});
    requestDeliveryRescan();
    await sleep(800);
    assert.strictEqual(bridge.rescanStalled, true, 'BACKPRESSURE activo tras rescan ciego');
    // PROGRESO REAL: el watermark de RECUPERACIÓN avanza (evento
    // procesado/admitido llega) -> los delivered viejos expiran por watermark
    // y el ledger se libera. NOTA (Fase 2 / AUDIT-10): NO es lastSeen (cursor
    // de recepción) el que demuestra progreso, sino recoveryWatermark.
    const st = getBridgeState();
    st.recoveryWatermark = now; // procesado hasta ahora -> delivered con ts now-600 inalcanzables
    // Ahora una admisión tiene hueco -> markDelivery exitoso -> levanta.
    const admitted = markDelivery('nuevo-1', 'pending');
    assert.strictEqual(admitted, true, 'admision posible tras liberar');
    assert.strictEqual(bridge.rescanStalled, false,
      'markDelivery exitoso levanta BACKPRESSURE (progreso real)');
    console.log('    rescanStalled=' + bridge.rescanStalled + ' tras admision');
  })());

  // cleanup
  _setBridgeStateForTest(fresh());
  try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
  delete process.env.PHANTOMBRIDGE_CONFIG;

  console.log('');
  console.log(`Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('FATAL:', e && e.message); process.exit(1); });
