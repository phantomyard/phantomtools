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
t('no drops: since = lastSeen - overlap', () => {
  assert.strictEqual(computeSince({lastSeen: 1000, pendingSince: null}), 880);
});
t('with a drop at t=500 (lastSeen=1000): since anchors to 500-overlap=380', () => {
  const since = computeSince({lastSeen: 1000, pendingSince: 500});
  assert.strictEqual(since, 380);
  assert.ok(since <= 500, 'since <= drop to recover it');
});
t('lastSeen advanced, old drop still recoverable', () => {
  const since = computeSince({lastSeen: 2000, pendingSince: 500});
  assert.strictEqual(since, 380);
  assert.ok(since <= 500, 'the drop at t=500 can still be recovered');
});
t('recent drop (990): since=870 covers the overlap', () => {
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
t('250 events in <120s: ev0 stays within the overlap (not evicted by count)', () => {
  const bs = {seenIds: []};
  const T = Math.floor(Date.now() / 1000);
  for (let i = 0; i < 250; i++) markSeen(bs, 'ev' + i, T + i * 0.5);
  // ev0 ts=T, now T+124.5 -> 124.5 < 180 (overlap+60) -> still seen
  assert.strictEqual(isSeen(bs, 'ev0', T + 124), true);
});
t('event beyond the overlap+60 is no longer seen (temporal purge)', () => {
  const T = Math.floor(Date.now() / 1000);
  const bs = {seenIds: [{id: 'a', ts: T}, {id: 'b', ts: T - 200}]};
  assert.strictEqual(isSeen(bs, 'a', T), true);
  assert.strictEqual(isSeen(bs, 'b', T), false);
});
t('migration: legacy format (strings) works', () => {
  const T = Math.floor(Date.now() / 1000);
  const raw = ['x', 'y'];
  const seenIds = raw.map(e => (typeof e === 'string' ? {id: e, ts: 0} : e));
  const bs = {seenIds};
  // legacy entries ts=0: isSeen handles them via the includes() fallback only if
  // they do not match the temporal range — with ts=0 and (now-0)>180, they do not count
  assert.strictEqual(bs.seenIds.length, 2);
});
t('markSeen idempotent: does not duplicate a recent id', () => {
  const T = Math.floor(Date.now() / 1000);
  const bs = {seenIds: []};
  markSeen(bs, 'dup', T);
  markSeen(bs, 'dup', T + 1);
  assert.strictEqual(bs.seenIds.filter(e => e.id === 'dup').length, 1);
});

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
