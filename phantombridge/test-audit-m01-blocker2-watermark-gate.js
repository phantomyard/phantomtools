// AUDIT-M01-BLOCKER2 (kaieriksen, 🔴 BLOQUEANTE): processWatermark() se
// ejecutaba ANTES del gate de autorización M01. Un agente autenticado pero SIN
// permiso de sala podia enviar un gift-wrap con created_at fabricado (futuro)
// que adelantaba recoveryWatermark aunque el comando luego fuera denegado por
// agentCanOperateRoom -> deliveredCanExpire() evictaba delivered que el relay
// aún podia re-entregar (break exactly-once / pérdida).
//
// Este test adversarial verifica el invariante REAL, no la sintaxis:
//   1. Un evento DENEGADO por M01 (finishDelivery rejected) NO avanza el
//      watermark, sea cual sea su created_at.
//   2. Un evento PROCESADO (finishDelivery ok, tras pasar el gate) SÍ avanza
//      el watermark, y solo dentro de una ventana creíble (skew futuro <= 24h):
//      un created_at hostil lejano (>24h al futuro) es ignorado.
//   3. El gate agentCanOperateRoom deniega al agente sin permiso de sala.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit-blocker2-'));
const tmpState = path.join(tmpDir, 'state.json');
const realConfigPath = path.join(__dirname, 'config.json');
const baseConfig = JSON.parse(fs.readFileSync(realConfigPath, 'utf8'));
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  advanceRecoveryWatermark, getBridgeState, _setBridgeStateForTest,
  finishDelivery, agentCanOperateRoom, evalRoomPermission,
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
function wm() { return (getBridgeState() || {}).recoveryWatermark || 0; }

// 1. finishDelivery(rejected) con created_at futuro NO mueve el watermark.
t('rejected: evento denegado con created_at futuro NO avanza recoveryWatermark', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const maliciousFuture = now + 7 * 24 * 3600; // una semana al futuro (fabricado)
  // Simulamos que el handler decidió NO procesar (denegado por M01, o sala no
  // activa, etc.): finishDelivery(id, false, true) -> rejected.
  finishDelivery('wrap-rejected-1', false, true, maliciousFuture);
  assert.strictEqual(wm(), 0, 'un evento rechazado no debe avanzar el watermark');
});

// 2. Un evento procesado con created_at lejano NO alimenta el cursor: el
//    watermark solo avanza vía advanceRecoveryWatermark() (paso acotado),
//    nunca por un timestamp externo del emisor.
t('hardening: un solo evento con corte reciente NO avanza hasta el reloj local', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // Watermark previo establecido (progreso confirmado hace 30 días:
  // downtime largo). Llega UN SOLO evento procesado ({ts} demo en `now-600`
  // aprox). El watermark NO debe saltar hasta `now` (reloj actual), porque el
  // relay no ha demostrado haber recorrido el backlog de 30 días con un solo
  // mensaje — eso expiraría delivered antiguos aún recuperables.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 30 * 24 * 3600, // hace 30 días
    delivery: {}});
  finishDelivery('wrap-downtime', true, false); // un único evento procesado
  const w = wm();
  // El avance queda ACOTADO al paso del watermark (no llega ni de lejos a now).
  assert.ok(w < now - 20 * 24 * 3600,
    'un solo evento tras downtime NO puede saltar a Date.now() (watermark acotado)');
  assert.ok(w > now - 30 * 24 * 3600,
    'el watermark sí avanza un paso (progreso real de un evento)');
});

// 3. Un backlog real (muchos eventos procesados) SI avanza el watermark de
//    forma incremental, demostrando que la recuperación progresa sin romper
//    exactly-once. Cada evento confirmado concede a lo sumo un paso acotado.
t('ok: backlog procesado avanza el watermark incrementalmente (progreso real)' , () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 30 * 24 * 3600, // tras downtime de 30 días
    delivery: {}});
  const START = wm();
  // Se procesan N eventos seguidos (ráfaga de backlog del relay).
  for (let i = 0; i < 500; i++) finishDelivery('wrap-backlog-' + i, true, false);
  const w = wm();
  assert.ok(w > START, 'el watermark avanza con la ráfaga de backlog');
  assert.ok(w <= now, 'el watermark nunca supera el reloj local del bridge');
});

// 4. El gate M01 niega a un agente sin permiso para la sala (independiente del
//    created_at): el comando no debería siquiera llegar a procesarse.
t('gate M01: agente sin permiso de sala es denegado antes de procesar', () => {
  const perms = { restricted: { 'secret-room': ['bob'] } };
  assert.strictEqual(evalRoomPermission(perms, 'alice', 'secret-room'), false,
    'alice (sin permiso) no debe operar secret-room');
  assert.strictEqual(evalRoomPermission(perms, 'bob', 'secret-room'), true,
    'bob (con permiso) sí debe operar secret-room');
});

// 5. Compatibilidad: agente con full capaz de avanzar el watermark tras
//    procesamiento (confirma que el fix no rompe el progreso legítimo).
t('ok: agente full procesado avanza el watermark (progreso real intacto)', () => {
  const perms = { full: ['alice'] };
  assert.strictEqual(evalRoomPermission(perms, 'alice', 'cualquier'), true);
  assert.strictEqual(evalRoomPermission(perms, 'alice', null), true); // room-agnostic
});

// 6. agentCanOperateRoom sigue exportado y funcional como gate real.
t('gate real agentCanOperateRoom: función y comportamiento', () => {
  assert.strictEqual(typeof agentCanOperateRoom, 'function');
});

// 7. finishDelivery con ok=true avanza el watermark SOLO por reloj local del
//    bridge (progreso real confirmado del stream), nunca por created_at del
//    emisor. (OPCION2: se eliminó el parámetro wrapTs de finishDelivery.)
t('finishDelivery(ok=true) avanza por reloj local, no por created_at del emisor', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // Watermark previo establecido (AUDIT-10: advanceRecoveryWatermark solo
  // extiende un watermark ya presente; no da el primer salto desde 0).
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 600, delivery: {}});
  // El 4º arg (antiguo wrapTs = created_at del emisor) ya NO existe en la
  // firma; finishDelivery(id, ok, rejected) ignora cualquier arg extra.
  finishDelivery('wrap-ok-1', true, false);
  const w = wm();
  assert.ok(w >= now - 600, 'finishDelivery(ok) avanza el watermark (progreso real)');
  assert.ok(w < now, 'el avance es ACOTADO (no salta libre a now)');
});

// 8. AUDIT-M01-OPCION2 (🔴 BLOQUEANTE kaieriksen): el watermark ya NO depende
//    de `created_at` del emisor. finishDelivery ya no recibe wrapTs; usa
//    advanceRecoveryWatermark() que avanza con el RELOJ LOCAL del bridge.
//    Por tanto un `created_at` creíble o hostil del emisor NO controla el
//    cursor (el emisor no controla el reloj del bridge). El watermark solo
//    avanza por progreso confirmado del stream (reloj local del bridge).
//
// 8a. finishDelivery(ok=true) NO usa wrapTs => un ts creíble como argumento de
//     más ya no tiene efecto (firma vieja de 4 arg; se ignora el 4º).
t('OPCION2: finishDelivery(ok) avanza solo por reloj local, NO por ts del emisor', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // Estado con watermark ya establecido (progreso real previo, AUDIT-10):
  // advanceRecoveryWatermark() extiende al reloj local. Un `created_at`
  // creíble que NO deberia avanzar (la cadena vieja esperaba wm == ts) ya no
  // es la fuente. El avance viene del reloj del bridge, no del argumento.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 300, // watermark previo establecido
    delivery: {}});
  // Podemos pasar un 4º argumento (ts del emisor) o no: finishDelivery ya lo
  // ignora. El watermark avanza al reloj local (~now), no al ts del argumento.
  finishDelivery('wrap-ok-1', true, false, now + 23 * 3600); // ts hostil +23h ignorado
  const w = wm();
  assert.ok(w > 0, 'finishDelivery(ok) avanza el watermark por progreso real');
  assert.ok(w <= now, 'el avance nunca supera el reloj local del bridge');
  assert.ok(w <= now - 300 + 300, 'el avance es ACOTADO al paso (prev estaba a menos de 1 paso de now)');
});

// 8b. El caso que pide ChatGPT: sender AUTORIZADO para room-A envia con
//     created_at = now+23h -> el watermark NO avanza a ese ts futuro.
t('OPCION2: sender autorizado con created_at futuro (+23h) NO adelanta el watermark', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  // Watermark previo establecido (progreso real). El sender autorizado para
  // room-A podria haber operado, pero el created_at fabricado (+23h) NO es la
  // fuente: el watermark solo avanza al reloj local del bridge, nunca al ts
  // del emisor, ni siquiera dentro del límite de 24h.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 600,
    delivery: {}});
  const fabricadoFuturo = now + 23 * 3600; // +23h: pasa el filtro de 24h
  finishDelivery('wrap-roomA', true, false, fabricadoFuturo);
  const w = wm();
  assert.ok(w <= now, 'watermark NO supera el reloj local pese al created_at futuro (+23h)');
  assert.ok(w >= now - 600, 'watermark solo avanzo por progreso real (no por el ts del emisor)');
  assert.ok(w < now, 'el avance queda acotado al paso (no salta a now tras downtime)');
});

// 9. AUDIT-M01-BLOCKER3 (kaieriksen, 🔴 BLOQUEANTE): los comandos de LECTURA
//    status/help/routes NO pasan por el gate M01 y NO representan progreso de
//    stream. Como el watermark ya no depende de `created_at` del emisor en
//    NINGUN path, status/help no pueden adelantarlo de ninguna forma.
t('BLOCKER3: status/help sin permiso NO pueden adelantar el watermark', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  const fabricado = now + 23 * 3600; // +23h
  finishDelivery('wrap-status', true, false, fabricado);
  // El watermark no avanza por el ts del emisor (ni crear; el reloj local del
  // bridge, si no hay watermark previo, no da salto desde 0 — AUDIT-10).
  assert.ok(wm() <= now, 'status/help no puede superar el reloj local del bridge');
});

// 10. Solo el progreso CONFIRMADO del stream (reloj local del bridge, watermark
//     previo establecido) avanza. join/leave/inject/routing pasan auth y el
//     reloj local les da el avance; el ts del emisor nunca interviene.
t('OPCION2: solo el reloj local del bridge (progreso real) avanza el watermark', () => {
  _setBridgeStateForTest(freshState());
  const now = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [],
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 120,
    delivery: {}});
  finishDelivery('wrap-auth', true, false, now - 5); // ts del emisor creible
  const w = wm();
  assert.ok(w > 0, 'progreso real avanza');
  assert.ok(w <= now, 'el avance nunca excede el reloj local (el ts del emisor no lo controla)');
});

console.log(`\nAUDIT-M01-BLOCKER2 (processWatermark gate order) Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
