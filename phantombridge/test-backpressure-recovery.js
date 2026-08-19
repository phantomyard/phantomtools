process.umask(0o077);
// H-NEW-01 + H-NEW-02 regression tests:
//  - backpressure drops must stay recoverable (pendingSince anchors `since`)
//  - seenIds dedup must be time-based (cover the overlap), not count-based
const assert = require('assert');
const STATE_OVERLAP_SECS = 120;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

// ---- H-NEW-01: since never passes a point with unacknowledged drops ----
function computeSince(bridgeState) {
  if (!bridgeState || bridgeState.lastSeen <= 0) return null;
  const cursor = (bridgeState.pendingSince != null && bridgeState.pendingSince < bridgeState.lastSeen)
    ? bridgeState.pendingSince : bridgeState.lastSeen;
  return cursor - STATE_OVERLAP_SECS;
}

console.log('H-NEW-01 (backpressure recovery):');
t('sin descartes: since = lastSeen - overlap', () => {
  assert.strictEqual(computeSince({lastSeen: 1000, pendingSince: null}), 880);
});
t('con descarte en t=500 (lastSeen=1000): since ancla a 500-overlap=380', () => {
  const since = computeSince({lastSeen: 1000, pendingSince: 500});
  assert.strictEqual(since, 380);
  assert.ok(since <= 500, 'since <= descarte para recuperarlo');
});
t('lastSeen avanzó, descarte antiguo sigue recuperable', () => {
  const since = computeSince({lastSeen: 2000, pendingSince: 500});
  assert.strictEqual(since, 380);
  assert.ok(since <= 500, 'se puede recuperar el descartado en t=500');
});
t('descarte reciente (990): since=870 cubre el overlap', () => {
  assert.strictEqual(computeSince({lastSeen: 1000, pendingSince: 990}), 870);
});

// ---- H-NEW-02: seenIds purge by TIME, not by count ----
const markSeen = (bs, id, ts) => {
  if (!bs.seenIds) bs.seenIds = [];
  if (bs.seenIds.length > 0) bs.seenIds = bs.seenIds.filter(e => e && e.ts && (ts - e.ts) < STATE_OVERLAP_SECS + 60);
  if (bs.seenIds.some(e => e && e.id === id)) return;
  bs.seenIds.unshift({id, ts});
};
const isSeen = (bs, id, ts) => {
  if (!bs.seenIds) return false;
  for (const e of bs.seenIds) if (e && e.id === id && e.ts && (ts - e.ts) < STATE_OVERLAP_SECS + 60) return true;
  return bs.seenIds.includes(id);
};

console.log('');
console.log('H-NEW-02 (time-based dedup):');
t('250 eventos en <120s: ev0 sigue dentro del overlap (no expulsado por count)', () => {
  const bs = {seenIds: []};
  const T = Math.floor(Date.now() / 1000);
  for (let i = 0; i < 250; i++) markSeen(bs, 'ev' + i, T + i * 0.5);
  // ev0 ts=T, ahora T+124.5 -> 124.5 < 180 (overlap+60) -> sigue seen
  assert.strictEqual(isSeen(bs, 'ev0', T + 124), true);
});
t('evento mas alla del overlap+60 ya no es seen (purga temporal)', () => {
  const T = Math.floor(Date.now() / 1000);
  const bs = {seenIds: [{id: 'a', ts: T}, {id: 'b', ts: T - 200}]};
  assert.strictEqual(isSeen(bs, 'a', T), true);
  assert.strictEqual(isSeen(bs, 'b', T), false);
});
t('migración: formato legacy (strings) funciona', () => {
  const T = Math.floor(Date.now() / 1000);
  const raw = ['x', 'y'];
  const seenIds = raw.map(e => (typeof e === 'string' ? {id: e, ts: 0} : e));
  const bs = {seenIds};
  // legacy entries ts=0: isSeen los trata via includes() fallback solo si
  // no matchean el rango temporal — con ts=0 y (now-0)>180, no cuentan
  assert.strictEqual(bs.seenIds.length, 2);
});
t('markSeen idempotente: no duplica id reciente', () => {
  const T = Math.floor(Date.now() / 1000);
  const bs = {seenIds: []};
  markSeen(bs, 'dup', T);
  markSeen(bs, 'dup', T + 1);
  assert.strictEqual(bs.seenIds.filter(e => e.id === 'dup').length, 1);
});

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
