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

require('./testlib.js').setup();
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

t('drop under backpressure -> id enters the dropped[] ledger', () => {
  resetState();
  recordDropped('dropped-ev-001');
  assert.ok(bs().dropped.some(d => d.id === 'dropped-ev-001'), 'id in ledger');
});

t('pendingSince sticky: NOT released while there are pending drops', () => {
  resetState();
  bs().dropped = [{id: 'p1', ts: 1000}];
  bs().pendingSince = 1000;
  releasePendingSinceIfRecovered();
  assert.strictEqual(bs().pendingSince, 1000, 'pendingSince sigue anclado con drops pendientes');
});

t('recovery: on seeing (markSeen) an dropped id it leaves the ledger', () => {
  resetState();
  bs().dropped = [{id: 'p2', ts: 1000}, {id: 'p3', ts: 900}];
  bs().pendingSince = 900;
  markSeen('p2');
  assert.ok(!bs().dropped.some(d => d.id === 'p2'), 'p2 recovered and out of the ledger');
  assert.ok(bs().dropped.some(d => d.id === 'p3'), 'p3 still pending');
  assert.strictEqual(bs().pendingSince, 900, 'pendingSince sigue activo con p3 pendiente');
});

t('pendingSince is released ONLY when the ledger is empty', () => {
  resetState();
  bs().dropped = [{id: 'p4', ts: 800}];
  bs().pendingSince = 800;
  markSeen('p4');
  assert.strictEqual(bs().dropped.length, 0, 'ledger empty');
  assert.strictEqual(bs().pendingSince, null, 'pendingSince released after recovering the last drop');
});

t('pumpNostrQueue with pending drops does NOT clear pendingSince', () => {
  resetState();
  bs().dropped = [{id: 'pump-1', ts: 1000}];
  bs().pendingSince = 1000;
  pumpNostrQueue();
  assert.strictEqual(bs().pendingSince, 1000, 'pump no borra pendingSince con drops pendientes');
});

t('replay of the full scenario: saturate -> drop -> drain -> restore -> recover', () => {
  resetState();
  let droppedId = null;
  for (let i = 0; i < 400 && droppedId === null; i++) {
    const ev = fakeEvent('burst-' + i);
    if (!enqueueGiftWrap(ev)) droppedId = ev.id;
  }
  assert.ok(droppedId !== null, 'a drop occurred under backpressure');
  assert.ok(bs().dropped.some(d => d.id === droppedId), 'the dropped event stayed in the ledger');
  assert.ok(bs().pendingSince != null, 'pendingSince set on drop');
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
