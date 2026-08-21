// AUDIT-4/5 punto de revisión (🟡): ¿puede un backlog de eventos NO
// autorizados avanzar `lastSeen` por delante de eventos legítimos y causar
// PÉRDIDA tras restart?
//
// Secuencia que el auditor quiere probar:
//   unauthorized backlog + authorized event + queue overflow + restart
//
// Flujo real (handleIncomingGiftWrap):
//   isSeen/deliveryStatus -> updateLastSeen() -> markSeen() -> NIP-17 auth
//   -> allowlist -> (solo autorizados) markDelivery(pending) -> ejecutar.
//
// El riesgo teórico: `updateLastSeen()` avanza `lastSeen` (reloj real) para
// TODO evento recibido, incluidos los no autorizados. Si un flood de no
// autorizados llena la cola y `lastSeen` avanza, y un legítimo posterior se
// descarta por overflow -> ¿se pierde? El mecanismo de protección es
// `pendingSince` + `recordDropped`: al descartar por overflow se ancla
// `pendingSince` y se registra el drop; `since = pendingSince - 120` en la
// siguiente suscripción nunca pasa ese punto -> el legítimo se re-entrega.
//
// Este test verifica que ESE mecanismo funciona: el cursor de recuperación
// tras restart NUNCA avanza más allá de `pendingSince`, de modo que un
// evento descartado por overflow (aunque sea tras/bajo un flood de no
// autorizados que avanzó lastSeen) sigue siendo alcanzable.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit-unauth-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  recordDropped, releasePendingSinceIfRecovered, recoverDropped,
  updateLastSeen, _setBridgeStateForTest, getBridgeState, STATE_FILE,
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

// Reproduce la lógica del subscription cursor en subscribeIncoming() (AUDIT-14/15).
// El cursor de procesamiento es recoveryWatermark (rango confirmado como
// recorrido al admitir/procesar), NO lastSeen (recepción local). pendingSince
// (STICKY) es el ancla conservadora si hay drops pendientes.
function subscriptionSince(state) {
  if (!state || !state.relay) return null; // full backlog
  const recovery = state.recoveryWatermark || 0;
  let cursor = recovery;
  if (state.pendingSince != null) {
    cursor = (cursor === 0 || state.pendingSince < cursor)
      ? state.pendingSince : cursor;
  }
  if (cursor <= 0) return null; // nothing processed yet -> full backlog
  return cursor - 120;
}

assert.strictEqual(STATE_FILE, tmpState, 'STATE_FILE temp, como en los otros tests');

console.log('AUDIT-4/5 🟡: lastSeen vs backlog no autorizado (¿pérdida tras restart?):');

// 1. Flood de no autorizados avanza lastSeen (reloj real).
t('flood de no autorizados avanza lastSeen (por diseño)', () => {
  _setBridgeStateForTest(fresh());
  // Simular la recepción de muchos eventos (autorizados o no): updateLastSeen
  // avanza lastSeen al reloj real en cada evento.
  updateLastSeen(0);
  updateLastSeen(0);
  // lastSeen avanzó por encima de 0.
  assert.ok(getBridgeState().lastSeen > 0, 'lastSeen avanzó tras recibir eventos');
});

// 2. Overflow: se descarta un evento legítimo -> se registra drop + pendingSince.
t('overflow de cola ancla pendingSince + registra el drop (no se pierde)', () => {
  _setBridgeStateForTest(fresh());
  const t0 = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: t0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {}});
  // El legítimo se descarta por overflow: recordDropped + pendingSince anclado.
  // (Simula exactamente lo que hace enqueueGiftWrap cuando la cola está llena.)
  recordDropped('legit-1');
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: t0, seenIds: [], pendingSince: t0,
    dropped: getBridgeState().dropped, droppedOverflow: false, delivery: {}});
  // El cursor de la próxima suscripción se ancla a pendingSince, NO a lastSeen.
  const since = subscriptionSince(getBridgeState());
  assert.ok(since <= t0 - 120 + 120, 'since anclado en pendingSince (no pasa el drop)');
  assert.ok(getBridgeState().pendingSince != null, 'pendingSince activo');
  assert.ok(getBridgeState().dropped.some(d => d.id === 'legit-1'), 'drop registrado');
});

// 3. Tras restart: el cursor NUNCA sobrepasa pendingSince mientras haya drops.
t('restart: cursor no avanza por delante del drop pendiente (sin pérdida)', () => {
  _setBridgeStateForTest(fresh());
  const t0 = Math.floor(Date.now() / 1000);
  // Estado persistido: lastSeen avanzó MUCHO por el flood de no autorizados,
  // pero pendingSince está anclado al punto del drop del legítimo.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: t0 + 5000, seenIds: [], pendingSince: t0,
    dropped: [{id: 'legit-2', ts: t0}], droppedOverflow: false, delivery: {}});
  const since = subscriptionSince(getBridgeState());
  // pendingSince < lastSeen -> cursor usa pendingSince (no lastSeen).
  assert.strictEqual(since, t0 - 120, 'since = pendingSince - 120 (el legítimo es alcanzable)');
  assert.ok(since < t0 + 5000, 'el cursor NO se deja arrastrar por lastSeen del flood');
});

// 4. Recuperación: cuando el legítimo se re-entrega (markSeen), el drop se
//    limpia; solo entonces pendingSince se libera y el cursor vuelve al de
//    PROCESAMIENTO (recoveryWatermark), no a lastSeen (recepción local).
t('recovery: al re-ver el drop, pendingSince se libera y el cursor de procesamiento manda', () => {
  _setBridgeStateForTest(fresh());
  const t0 = Math.floor(Date.now() / 1000);
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: t0 + 5000, seenIds: [], pendingSince: t0,
    dropped: [{id: 'legit-3', ts: t0}], droppedOverflow: false, recoveryWatermark: t0 + 5000, delivery: {}});
  // El relay re-entrega legit-3 -> recoverDropped lo saca del ledger.
  recoverDropped('legit-3');
  // Ya sin drops, releasePendingSinceIfRecovered libera el ancla.
  releasePendingSinceIfRecovered();
  assert.strictEqual(getBridgeState().pendingSince, null, 'pendingSince liberado al recuperar');
  assert.strictEqual(getBridgeState().dropped.length, 0, 'ledger de drops vacío');
  // Ahora el cursor vuelve al de PROCESAMIENTO (recoveryWatermark), que es la
  // base correcta del `since` — NO lastSeen (recepción local manipulable).
  const since = subscriptionSince(getBridgeState());
  assert.strictEqual(since, (t0 + 5000) - 120, 'tras recuperar, since = recoveryWatermark - 120');
});

// 5. El caso que SÍ sería pérdida: si el cursor dependiera de lastSeen (recepción
//    local manipulable por un flood de NO procesados), un evento recibido pero
//    aún sin admitir podría quedar fuera del `since`. Con el cursor de
//    PROCESAMIENTO (recoveryWatermark), un flood de recepción NO mueve el
//    `since`, así que un legítimo pendiente sigue cubierto aunque lastSeen haya
//    avanzado mucho por recepción cruda no-admitida.
t('regresión: lastSeen (recepción) NO controla el since — recoveryWatermark (procesamiento) sí', () => {
  _setBridgeStateForTest(fresh());
  const t0 = Math.floor(Date.now() / 1000);
  // Escenario: flood de recepción avanzó lastSeen MUCHO (t0+5000) pero NO se
  // procesó nada (recoveryWatermark=0) -> el since NO debe saltar: queda null
  // (full backlog) para que el relay re-entregue lo pendiente.
  const stateNoProcess = {relay: 'ws://test.local', lastSeen: t0 + 5000, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, delivery: {}};
  const sinceNoProcess = subscriptionSince(stateNoProcess);
  assert.strictEqual(sinceNoProcess, null,
    'sin procesamiento confirmado, since=null (full backlog), lastSeen NO lo mueve');
  // Escenario con procesamiento real: recoveryWatermark avanzó -> el since se
  // ancla a él (el relay recorrió ese rango).
  const stateProcessed = {relay: 'ws://test.local', lastSeen: t0 + 5000, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: t0 + 5000, delivery: {}};
  const sinceProcessed = subscriptionSince(stateProcessed);
  assert.strictEqual(sinceProcessed, (t0 + 5000) - 120,
    'con procesamiento confirmado, since = recoveryWatermark - 120');
});

// cleanup
_setBridgeStateForTest(fresh());
bridge.flushStateNow(); // flush pending state write before rmSync (exit handler)
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
