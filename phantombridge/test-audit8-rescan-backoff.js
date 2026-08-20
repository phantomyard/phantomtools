// AUDIT-8 (MEDIO): el rescan de recuperación NO debe convertirse en un bucle
// de reconexiones contra el relay/el proceso.
//
// Escenario del auditor: si el ledger permanece lleno (lastSeen no avanza) y
// cada evento no admitido reinvoca requestDeliveryRescan(), tendríamos
// connect/close/connect/close... indefinidamente — un DoS de reconexión
// contra el relay y contra el propio proceso.
//
// El fix: backoff exponencial (RESCAN_MIN_INTERVAL_MS * 2^attempts, capado a
// RESCAN_MAX_BACKOFF_MS) + límite duro de rescans por ventana de 60s
// (RESCAN_MAX_PER_MINUTE). Cuando se alcanza el techo de la ventana, se
// suprime el rescan (queda deliveryRescanNeeded=true pero NO se programa más
// reconexión); el siguiente ciclo natural o un evento que libere espacio lo
// rearma.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit8-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
// Reducir RESCAN_MAX_PER_MINUTE y acelerar el backoff para un test rápido.
baseConfig.rescanMaxPerMinute = 3;
baseConfig.rescanMinIntervalMs = 100;   // espera mínima acelerada
baseConfig.rescanMaxBackoffMs = 400;    // techo bajo
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  requestDeliveryRescan, _setBridgeStateForTest, _resetRescanStateForTest,
  getBridgeState, markDelivery,
} = bridge;

function fresh() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}};
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

(async () => {
  let passed = 0, failed = 0;
  // Test runner async: espera la promesa de cada test y cuenta.
  function run(name, promise) {
    return promise.then(() => { console.log('  ok:', name); passed++; })
      .catch(e => { console.error('  FAIL:', name, '-', e.message); failed++; });
  }

  // Test 1: el techo de rescans/minuto suprime rescans adicionales — NO hay
  // bucle. Tras RESCAN_MAX_PER_MINUTE no se programa más reconexión en la
  // ventana (aunque deliveryRescanNeeded pueda quedar marcado).
  await run('techo de rescans/minuto: no se programa bucle de reconexión', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const t0 = Date.now();
    // Ráfaga: 100 peticiones de rescan en el mismo tick (ledger lleno ->
    // cada markDelivery fallido pide rescan). Solo la primera programa.
    for (let i = 0; i < 100; i++) requestDeliveryRescan();
    await sleep(700); // dejar que se ejecuten los timers de 100ms
    const windowCount = bridge.rescanWindowCount;
    assert.ok(windowCount <= baseConfig.rescanMaxPerMinute,
      'no se programan más de RESCAN_MAX_PER_MINUTE rescans en la ventana (got ' + windowCount + ')');
    console.log('    ventana: ' + windowCount + ' rescans emitidos de techo ' + baseConfig.rescanMaxPerMinute);
    assert.ok(Date.now() - t0 < 5000, 'la ráfaga se resuelve rápido (sin esperas largas espúreas)');
  })());

  // Test 2: backoff — tras una ráfaga, los reintentos espaciados muestran
  // el contador progresando (2 rescans) sin superar el techo.
  await run('backoff: reintentos espaciados, nunca superan el techo de ventana', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    // 1er rescan: arranque -> min interval (100ms).
    requestDeliveryRescan();
    await sleep(150);
    // 2ª petición: entra en backoff (aproximadamente 200ms = 100*2).
    requestDeliveryRescan();
    await sleep(500);
    const windowCount = bridge.rescanWindowCount;
    assert.ok(windowCount >= 2, 'progresa al menos un segundo rescan (got ' + windowCount + ')');
    assert.ok(windowCount <= baseConfig.rescanMaxPerMinute, 'nunca supera el techo');
    console.log('    backoff: ' + windowCount + ' rescans en ventana tras ráfaga');
  })());

  // Test 3: el escenario del auditor NO reconecta sin límite — con el ledger
  // lleno y muchos markDelivery fallidos, el número de rescans emitidos queda
  // acotado por el techo de ventana.
  await run('escenario auditor: 200 fallidos generan <=MAX rescans (no bucle)', (async () => {
    _setBridgeStateForTest(fresh());
    _resetRescanStateForTest();
    const now = Math.floor(Date.now() / 1000);
    const entry = {};
    for (let i = 0; i < 20000; i++) entry['d-' + i] = {status: 'delivered', ts: now - 60};
    _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null,
      dropped: [], droppedOverflow: false, delivery: entry});
    // 200 eventos que no se admiten (fail-closed). Cada uno intenta rescan.
    for (let i = 0; i < 200; i++) markDelivery('E' + i, 'pending');
    await sleep(600);
    assert.ok(bridge.rescanWindowCount <= baseConfig.rescanMaxPerMinute,
      '200 fallidos generan como mucho ' + baseConfig.rescanMaxPerMinute + ' rescans (got ' + bridge.rescanWindowCount + ')');
    console.log('    200 fallidos -> ' + bridge.rescanWindowCount + ' rescans programados');
  })());

  // cleanup
  _setBridgeStateForTest(fresh());
  try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
  delete process.env.PHANTOMBRIDGE_CONFIG;

  console.log('');
  console.log(`Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('FATAL:', e && e.message); process.exit(1); });
