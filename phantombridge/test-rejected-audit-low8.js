#!/usr/bin/env node
// LOW-8 regression test (audit 462e62b): frames rechazados (p.ej. demasiado
// grandes) deben marcarse en un caché SEPARADO (rejectedIds), NO en seenIds
// (dedup de eventos PROCESABLES). El bug previo: subscribeIncoming marcaba
// big[2].id con markSeen() antes de validar JSON/firma/kind, permitiendo que
// un atacante inyectara hasta ~200 IDs arbitrarios por frame gigante y
// degradara la dedup legítima de seenIds.
//
// Este test ejercita las funciones REALES exportadas del bridge
// (markRejected / isRejected / rejectedIds), sin bootear relay.
const assert = require('assert');
require('./testlib.js').setup();
const bridge = require('./bridge.js');
const {markRejected, isRejected, rejectedIds, markSeen, isSeen, getBridgeState, _setBridgeStateForTest} = bridge;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.error('  FAIL:', name, '—', e.message); }
}
function resetRejected() { rejectedIds.length = 0; }

// Seed estado para markSeen (necesita bridgeState con seenIds).
_setBridgeStateForTest({
  relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: [],
});

// 1. markRejected registra y isRejected lo ve.
t('markRejected -> isRejected true', () => {
  resetRejected();
  markRejected('id-rechazado-1');
  assert.strictEqual(isRejected('id-rechazado-1'), true);
});

// 2. No contamina seenIds (dedup de eventos procesables).
t('markRejected NO marca seenIds (dedup intacta)', () => {
  resetRejected();
  markRejected('id-gigante');
  assert.strictEqual(isRejected('id-gigante'), true);
  // El id NO está en seenIds: un evento legítimo distinto no se ve afectado.
  assert.strictEqual(getBridgeState().seenIds.some(e => e.id === 'id-gigante'), false);
  assert.strictEqual(isSeen('id-gigante'), false);
});

// 3. Cap propio: >200 rechazados trunca el caché a 200.
t('cap REJECTED_IDS_MAX (200) aplica', () => {
  resetRejected();
  for (let i = 0; i < 250; i++) markRejected('rej-' + i);
  assert.ok(rejectedIds.length <= 200, 'rejectedIds.length=' + rejectedIds.length);
  // los más recientes se conservan, los más viejos se evictan
  assert.strictEqual(isRejected('rej-249'), true);
});

// 4. Idempotente: marcar dos veces el mismo id no duplica entradas.
t('markRejected idempotente (no duplica)', () => {
  resetRejected();
  markRejected('dup');
  markRejected('dup');
  markRejected('dup');
  assert.strictEqual(rejectedIds.filter(e => e.id === 'dup').length, 1);
});

// 5. TTL: una entrada con ts fuera de ventana no cuenta como rechazada.
t('TTL: entrada vieja no es rechazada', () => {
  resetRejected();
  markRejected('viejo');
  // envejecer manualmente la entrada más allá de la ventana
  const now = Math.floor(Date.now() / 1000);
  const entry = rejectedIds.find(e => e.id === 'viejo');
  entry.ts = now - (130 + 3600); // mucho más allá de STATE_OVERLAP_SECS+60
  assert.strictEqual(isRejected('viejo'), false);
});

// 6. Sin id -> no-op, no lanza.
t('markRejected(undefined) / (null) no-op', () => {
  resetRejected();
  markRejected(undefined);
  markRejected(null);
  assert.strictEqual(rejectedIds.length, 0);
});

// 7. El caché acepta ids arbitrarios pero con cap: un atacante no puede
//    crecer rejectedIds sin límite (LOW-8: degradación acotada).
t('flood de ids arbitrarios queda acotado por cap', () => {
  resetRejected();
  for (let i = 0; i < 500; i++) markRejected('evil-' + i);
  assert.ok(rejectedIds.length <= 200, 'length=' + rejectedIds.length);
});

// 8. markSeen sigue funcionando para eventos legítimos (sin romper ALTO-3).
t('markSeen todavía registra seen normal (regresión ALTO-3)', () => {
  markSeen('legitimo-1');
  assert.strictEqual(isSeen('legitimo-1'), true);
});

console.log('\nResult: ' + passed + ' ok, ' + failed + ' fail');
process.exit(failed ? 1 : 0);
