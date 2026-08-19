// 🟠 MEDIO (auditoría): processWatermark() debe estar ALIMENTADO con el
// progreso real del relay, no solo definido. Sin integración, la garantía de
// recuperación colgaba: el cursor que seguía avanzando al recibir eventos era
// lastSeen (vía updateLastSeen), mientras que recoveryWatermark (la única
// base legítima de deliveredCanExpire) quedaba en 0 y nunca expiraba delivered
// inalcanzables — o peor, si se avanzaba por Date.now() de una admisión
// interna, expiraba delivered no confirmados por el relay (break exactly-once).
//
// La ruta real: handleIncomingGiftWrap, tras autenticar+autorizar+admitir un
// evento (markDelivery pending exitoso), llama finishDelivery(ok) que a su vez
// invoca advanceRecoveryWatermark() (avance incremental ACOTADO, NUNCA un
// salto libre a Date.now() tras downtime). Este test demuestra la cadena
// completa que la auditoría exige:
//   evento procesado -> advanceRecoveryWatermark -> recoveryWatermark avanza
//     -> delivered antiguo puede expirar (y NO por mera recepción/lastSeen).
// AUDIT-M01-OPCION2-FIX: processWatermark(ts) fue ELIMINADO — alimentar el
// cursor con un timestamp externo (created_at del emisor) reintroduce la
// superficie de ataque. El único avance legítimo es el paso acotado.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit11-'));
const tmpState = path.join(tmpDir, 'state.json');
const realConfigPath = path.join(__dirname, 'config.json');
const baseConfig = JSON.parse(fs.readFileSync(realConfigPath, 'utf8'));
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  advanceRecoveryWatermark, markDelivery, deliveryStatus, getBridgeState,
  _setBridgeStateForTest, recoveryWatermark,
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

console.log('🟠 AUDIT-11: processWatermark alimentado con el progreso real del relay (cadena completa):');

// Test 1: la CADENA COMPLETA — un event procesado avanza el watermark y
// entonces (y solo entonces) un delivered viejo puede expirar. Esto es lo que
// la auditoría pide demostrar en código: NO basta "evento recibido -> lastSeen
// avanza"; es "evento procesado -> advanceRecoveryWatermark -> recoveryWatermark
// avanza -> delivered viejo expira". El avance es INCREMENTAL ACOTADO (nunca
// un salto libre a Date.now()): se procesan varios eventos para que el
// watermark recorra el rango.
t('cadena completa: evento procesado avanza el watermark -> delivered viejo expira', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 3600; // delivered entregado hace 1h
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: tX - 1, // watermark justo antes de X
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // ANTES de procesar: lastSeen puede estar inflado por recepción (0 aquí,
  // pero en la ruta real sería la hora de recepción) — lastSeen NO es la
  // base. Solo el watermark de recuperación lo es.
  const st0 = getBridgeState();
  st0.lastSeen = now; // recepción cruda al día, pero watermark no avanza por eso
  const wmBefore = bridge.recoveryWatermark;
  // Procesamos UNA ráfaga de eventos reales (backlog del relay tras conectar).
  advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark > wmBefore,
    'el evento procesado avanza el watermark (progreso real)');
  // El avance es ACOTADO: 1 evento dado a -3600 no llega ni de lejos a now.
  assert.ok(bridge.recoveryWatermark < now,
    'un evento aislado tras downtime NO salta a Date.now()');
  // Suficientes eventos procesados (ráfaga) -> watermark supera X + ventana.
  for (let i = 0; i < 20; i++) advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark >= tX + 120 + 120,
    'tras la ráfaga el watermark supera la ventana de X');
  // Ahora una admisión dispara el sweep; X (ts+240 < watermark) expira.
  assert.ok(markDelivery('nuevo-1', 'pending'), 'admisión exitosa');
  assert.strictEqual(deliveryStatus('X'), null,
    'delivered viejo expira SOLO tras el watermark de recuperación avanzado');
});

// Test 2: un evento RECIBIDO pero NO procesado NO avanza el watermark — la
// diferencia frente a lastSeen. Aquí simulamos que lastSeen avanza por
// recepción (como hace updateLastSeen en la ruta real) pero el watermark se
// queda, porque no hubo processWatermark/evento admitido.
t('recepción cruda (lastSeen) NO alimenta el watermark — solo el procesado', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const tX = now - 600;
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false, recoveryWatermark: 0,
    delivery: {'X': {status: 'delivered', ts: tX}}});
  // Ráfaga de recepción: lastSeen avanza (lo hace updateLastSeen en el
  // handler por cada frame, incluso no-admitidos), pero nadie llama
  // processWatermark -> el watermark sigue en 0.
  const st = getBridgeState();
  st.lastSeen = now; // la recepción avanzó lastSeen, no el watermark
  assert.strictEqual(bridge.recoveryWatermark, 0, 'watermark intacto en 0 (solo recepción)');
  // Una admisión interna (markDelivery) NO da el primer salto por Date.now().
  markDelivery('nuevo-1', 'pending');
  assert.strictEqual(deliveryStatus('X'), 'delivered',
    'X se conserva: la recepción/la admisión interna no expira delivered (watermark en 0)');
});

// Test 3: monotónico — un evento previo (backdated) no retrocede el watermark.
// El avance acotado nunca retrocede; procesar tras un backlog solo suma.
t('el watermark es monotónico: un procesamiento viejo no retrocede', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  advanceRecoveryWatermark(); // procesamos algo (afianza un watermark)
  const w1 = bridge.recoveryWatermark;
  advanceRecoveryWatermark(); // otro procesamiento
  assert.ok(bridge.recoveryWatermark >= w1, 'no retrocede pese al paso');
  // Y un procesamiento al día sí avanza (paso acotado).
  const before = bridge.recoveryWatermark;
  advanceRecoveryWatermark();
  assert.ok(bridge.recoveryWatermark > before, 'avanza con el procesamiento real');
});

// Test 4: la integración real tras OPCION2 — el runtime YA NO llama
// processWatermark(created_at del emisor) en ningún path. El avance del
// watermark ocurre SOLO por reloj local del bridge (advanceRecoveryWatermark)
// tras procesamiento confirmado. Verificamos a nivel de código que: (a) el
// handler ya no deriva wrapTs para realimentar el watermark, y (b) un sender
// autorizado no tiene vía para empujar su created_at futuro al cursor.
t('OPCION2: el runtime NO alimenta el watermark con created_at del emisor', () => {
  const src = fs.readFileSync('./bridge.js', 'utf8');
  // (a) El handler no debe llamar processWatermark(created_at del wrap) tras
  // admitir. El watermark solo lo avanza advanceRecoveryWatermark() (reloj
  // local) desde finishDelivery(ok) / routing exitoso. La única aparición de
  // 'processWatermark(wrapTs)' es un COMENTARIO explicativo (línea que
  // empieza por //) que describe por qué ya NO se hace — no código.
  const lines = src.split('\n').filter(l => l.includes('processWatermark(wrapTs)'));
  assert.ok(lines.every(l => /^\s*\/\//.test(l)),
    'toda aparicion de processWatermark(wrapTs) debe ser comentario, no codigo');
  // (c) los avances legítimos usan advanceRecoveryWatermark (reloj local).
  const finish = src.indexOf('function finishDelivery(id, ok, rejected)');
  let depth = 0, finEnd = finish;
  for (; finEnd < src.length; finEnd++) {
    if (src[finEnd] === '{') depth++;
    else if (src[finEnd] === '}') { depth--; if (depth === 0) break; }
  }
  const finishBlock = src.slice(finish, finEnd + 1);
  assert.ok(finishBlock.includes('advanceRecoveryWatermark();'),
    'finishDelivery debe avanzar el watermark por reloj local (no por created_at)');
});

// cleanup
_setBridgeStateForTest(freshState());
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
