// ALTO-2 regression test (audit 462e62b): `pendingSince` must be STICKY and
// only released once the dropped gift-wraps are REALLY recovered, not merely
// when the local queue drains.
//
// The old bug: pumpNostrQueue() set `pendingSince = null` as soon as the
// local queue was empty. But draining the queue ≠ the dropped events were
// processed; a drop under backpressure could then fall outside the 120s
// reconnect overlap and be lost PERMANENTLY.
//
// This exercises the REAL bridge functions (enqueueGiftWrap, recordDropped,
// pumpNostrQueue, releasePendingSinceIfRecovered, markSeen) via the exported
// module — not a copied computeSince() math helper.
const assert = require('assert');

const bridge = require('./bridge.js');
const {
  enqueueGiftWrap, pumpNostrQueue, recordDropped,
  releasePendingSinceIfRecovered, markSeen, getBridgeState, _setBridgeStateForTest,
} = bridge;

// Seed a working module-level bridgeState so the exported functions operate
// on real state (mirrors NOSTR_MODE init, but isolated from a state file).
_setBridgeStateForTest({
  relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: [],
});

const bs = () => getBridgeState();

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

// Wrap-like stubs; enqueueGiftWrap only reads .id on drop.
const fakeEvent = (id) => ({id, kind: 1059, content: 'x', pubkey: 'a'.repeat(64)});

function resetState() {
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: []});
}

console.log('ALTO-2 (pendingSince sticky + dropped ledger):');

t('drop bajo backpressure -> id entra en ledger dropped[]', () => {
  resetState();
  recordDropped('dropped-ev-001');
  assert.ok(bs().dropped.some(d => d.id === 'dropped-ev-001'), 'id en ledger');
});

t('pendingSince sticky: NO se libera mientras haya drops pendientes', () => {
  resetState();
  bs().dropped = [{id: 'p1', ts: 1000}];
  bs().pendingSince = 1000;
  releasePendingSinceIfRecovered();
  assert.strictEqual(bs().pendingSince, 1000, 'pendingSince sigue anclado con drops pendientes');
});

t('recovery: al ver (markSeen) un id descartado sale del ledger', () => {
  resetState();
  bs().dropped = [{id: 'p2', ts: 1000}, {id: 'p3', ts: 900}];
  bs().pendingSince = 900;
  markSeen('p2');
  assert.ok(!bs().dropped.some(d => d.id === 'p2'), 'p2 recuperado y fuera del ledger');
  assert.ok(bs().dropped.some(d => d.id === 'p3'), 'p3 sigue pendiente');
  assert.strictEqual(bs().pendingSince, 900, 'pendingSince sigue activo con p3 pendiente');
});

t('pendingSince se libera SOLO cuando el ledger queda vacío', () => {
  resetState();
  bs().dropped = [{id: 'p4', ts: 800}];
  bs().pendingSince = 800;
  markSeen('p4');
  assert.strictEqual(bs().dropped.length, 0, 'ledger vacío');
  assert.strictEqual(bs().pendingSince, null, 'pendingSince liberado tras recuperar el último drop');
});

t('pumpNostrQueue con drops pendientes NO limpia pendingSince', () => {
  resetState();
  bs().dropped = [{id: 'pump-1', ts: 1000}];
  bs().pendingSince = 1000;
  pumpNostrQueue();
  assert.strictEqual(bs().pendingSince, 1000, 'pump no borra pendingSince con drops pendientes');
});

t('replay del escenario completo: saturar -> drop -> drenar -> restaurar -> recuperar', () => {
  resetState();
  let droppedId = null;
  for (let i = 0; i < 400 && droppedId === null; i++) {
    const ev = fakeEvent('burst-' + i);
    if (!enqueueGiftWrap(ev)) droppedId = ev.id;
  }
  assert.ok(droppedId !== null, 'se produjo un descarte bajo backpressure');
  assert.ok(bs().dropped.some(d => d.id === droppedId), 'el descartado quedó en el ledger');
  assert.ok(bs().pendingSince != null, 'pendingSince marcado al descartar');
  pumpNostrQueue();
  assert.strictEqual(bs().pendingSince != null, true, 'pendingSince activo tras drenar la cola');
  markSeen(droppedId);
  markSeen('restore-x');
  assert.strictEqual(bs().pendingSince, null, 'pendingSince liberado tras recuperar');
  resetState();
});

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
