process.umask(0o077);
// Unit tests of the anti-loop (content dedup, pair rate, request_id
// short-circuit). No relay, no XMPP.
// Usage: node test-antiloop.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');

const TEST_NSEC = nip19.nsecEncode(generateSecretKey());
process.env.PHANTOMBRIDGE_TEST_NSEC = TEST_NSEC;
const TEST_DIR = path.join(__dirname, '.test-tmp-antiloop');
fs.mkdirSync(TEST_DIR, {recursive: true});
fs.writeFileSync(path.join(TEST_DIR, 'config.json'), JSON.stringify({
  mode: 'nostr',
  nick: 'secretario',
  httpPort: 18091,
  nostr: {relay: 'ws://127.0.0.1:19998', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'},
  agents: {
    roberto: getPublicKey(generateSecretKey()),
    alma: getPublicKey(generateSecretKey()),
    paco: getPublicKey(generateSecretKey()),
    pepa: getPublicKey(generateSecretKey()),
  },
  routing: {
    permissions: {
      roberto: ['alma', 'paco', 'pepa'],
      alma: ['roberto'],
      paco: ['*'],
    },
    default: 'deny',
  },
  antiloop: {
    hashWindowMs: 3600000,  // F3-01: aligned with the production default (1h)
    pairWindowMs: 60000,
    pairMax: 3,        // lowered on purpose for the rate test
    pairHourMax: 10,   // F3-01: hourly limit (default)
    reqWindowMs: 600000,
    reqMax: 4,         // lowered on purpose for the short-circuit test
  },
}, null, 2));

process.env.PHANTOMBRIDGE_CONFIG = path.join(TEST_DIR, 'config.json');
// LOW-9: loadState() distingue ENOENT vs corrupción y aborta si el shape es
// inesperado. Limpiamos cualquier .bridge-state.json residual de corridas
// anteriores para que el test no dependa de un artefacto obsoleto.
fs.rmSync(path.join(TEST_DIR, '.bridge-state.json'), {force: true});
const {antiLoopCheck, antiLoopRollback, resolveRid, ANTILOOP, parseEnvelope, envelopeMac, stampEnvelope, extractRid} = require('./bridge.js');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.log('  FAIL:', name, '—', e.message); }
}

// Reset del estado entre tests
function reset() {
  ANTILOOP.hashes.clear();
  ANTILOOP.pairs.clear();
  ANTILOOP.pairHours.clear();
  ANTILOOP.requests.clear();
  ANTILOOP.routed = 0;
  ANTILOOP.dropped = {hash: 0, fuzzy: 0, pair: 0, request: 0, cycle: 0, hops: 0, expired: 0};
  ANTILOOP.evictedHashes = 0;
  ANTILOOP.lastSweep = 0;
  ANTILOOP.nextAdmissionId = 1;
}

// Signs an envelope with the bridge's own MAC key (bridgeSk) so the
// fail-closed auth model accepts it. Mirrors stampEnvelope()'s signing but
// preserves the caller-supplied hops/trace/expires/rid exactly. bridgeSk is
// derived from the config nsec written by this test, so the same key signs
// and verifies.
function signEnvelope(env, rest) {
  const unsigned = {...env};
  delete unsigned.sig;
  const sig = envelopeMac(unsigned, rest);
  return '[env] ' + JSON.stringify({...unsigned, sig}) + '\n' + rest;
}

console.log('Anti-loop tests:');

t('mensaje nuevo pasa', () => {
  reset();
  assert.strictEqual(antiLoopCheck('roberto', 'alma', 'REQUEST aquaponics-united-20260811-0001: hola').ok, true);
});

t('identical repeated message is dropped (dedup)', () => {
  reset();
  antiLoopCheck('roberto', 'alma', 'texto identico');
  const r = antiLoopCheck('roberto', 'alma', 'texto identico');
  assert.ok(r && r.ok === false && r.reason === 'dedup', 'esperaba dedup, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.hash, 1);
});

t('mismo texto a distinto receptor NO se dropea', () => {
  reset();
  antiLoopCheck('roberto', 'alma', 'texto identico');
  assert.strictEqual(antiLoopCheck('roberto', 'pepa', 'texto identico').ok, true);
});

t('F2-05: trivial reformatting (spaces/uppercase/punctuation) dropped by canonical', () => {
  reset();
  antiLoopCheck('roberto', 'alma', 'Hola, ¿cómo estás?  Confirma la reunión.');
  // Same content with different whitespace/punctuation/case
  const r = antiLoopCheck('roberto', 'alma', '  hola como estas confirma la reunion  ');
  assert.ok(r && r.ok === false && r.reason === 'dedup', 'expected canonical dedup, got: ' + JSON.stringify(r));
});

t('F2-05: different envelope (new rid) with SAME content is dropped', () => {
  reset();
  // Real scenario: bot A publishes WITHOUT envelope (non-cooperative) -> admitted
  assert.strictEqual(antiLoopCheck('roberto', 'alma', 'REQUEST aquaponics-united-20260811-0001: confirma la reunion').ok, true);
  // The same bot re-publishes the SAME body with a new rid (regenerates the
  // identifier) -> the rid is metadata, not content: must fall into dedup
  const r = antiLoopCheck('roberto', 'alma', 'REQUEST aquaponics-united-20260811-9999: confirma la reunion');
  assert.ok(r && r.ok === false && r.reason === 'dedup', 'expected canonical dedup (F2-05), got: ' + JSON.stringify(r));
});

t('F2-05: near-identical paraphrase (shingles) dropped by fuzzy', () => {
  reset();
  antiLoopCheck('roberto', 'alma', 'Necesito que confirmes la reunion de manana con el equipo');
  // Paraphrase: same key words, slightly different order/articles
  const r = antiLoopCheck('roberto', 'alma', 'necesito que confirmes manana la reunion con el equipo');
  assert.ok(r && r.ok === false && r.reason === 'fuzzy', 'expected fuzzy, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.fuzzy, 1);
});

t('F2-05: legitimately different messages are NOT dropped (fuzzy)', () => {
  reset();
  antiLoopCheck('roberto', 'alma', 'Necesito que confirmes la reunion de manana con el equipo');
  const r = antiLoopCheck('roberto', 'alma', 'Actualiza el informe trimestral de gastos por favor');
  assert.ok(r && r.ok === true, 'expected ok, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.fuzzy, 0);
});

t('F2-05: same content to a different pair is NOT dropped (broadcast)', () => {
  reset();
  antiLoopCheck('roberto', 'alma', 'Hola, ¿cómo estás?  Confirma la reunión.');
  assert.strictEqual(antiLoopCheck('roberto', 'pepa', 'hola como estas confirma la reunion').ok, true);
});

t('F2-05: short message (1 word) reformatted is dropped', () => {
  reset();
  antiLoopCheck('roberto', 'alma', 'HOLA');
  const r = antiLoopCheck('roberto', 'alma', 'hola!');
  assert.ok(r && r.ok === false && r.reason === 'dedup', 'expected short canonical dedup, got: ' + JSON.stringify(r));
});

t('F2-05: diacritics do not break dedup (ñ/accents)', () => {
  reset();
  antiLoopCheck('roberto', 'alma', 'mañana nos vemos en la estación');
  const r = antiLoopCheck('roberto', 'alma', 'manana nos vemos en la estacion');
  assert.ok(r && r.ok === false && r.reason === 'dedup', 'expected canonical dedup with diacritics, got: ' + JSON.stringify(r));
});

t('F2-05: configurable fuzzyThreshold — below it does NOT drop', () => {
  reset();
  const before = ANTILOOP.fuzzyThreshold;
  ANTILOOP.fuzzyThreshold = 0.99;  // extreme: almost nothing is "near-identical"
  antiLoopCheck('roberto', 'alma', 'Necesito que confirmes la reunion de manana con el equipo');
  const r = antiLoopCheck('roberto', 'alma', 'necesito que confirmes manana la reunion con el equipo');
  ANTILOOP.fuzzyThreshold = before;
  assert.ok(r && r.ok === true, 'expected ok with threshold 0.99, got: ' + JSON.stringify(r));
});

t('pair rate: excess is dropped', () => {
  reset();
  ANTILOOP.pairMax = 3;
  assert.strictEqual(antiLoopCheck('alma', 'roberto', 'm1').ok, true);
  assert.strictEqual(antiLoopCheck('alma', 'roberto', 'm2').ok, true);
  assert.strictEqual(antiLoopCheck('alma', 'roberto', 'm3').ok, true);
  const r = antiLoopCheck('alma', 'roberto', 'm4');
  assert.ok(r && r.ok === false && r.reason === 'rate', 'expected rate, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.pair, 1);
});

t('pair rate independent per direction', () => {
  reset();
  ANTILOOP.pairMax = 3;
  antiLoopCheck('alma', 'roberto', 'm1');
  antiLoopCheck('alma', 'roberto', 'm2');
  antiLoopCheck('alma', 'roberto', 'm3');
  // roberto -> alma has its own window: passes
  assert.strictEqual(antiLoopCheck('roberto', 'alma', 'otro').ok, true);
});

t('F3-01: SLOW loop with same content cut by 1h dedup window', () => {
  reset();
  // Simulates a slow loop: same request every 15 min for 1h (fake clock).
  // WITHOUT rid (no envelope nor REQUEST): isolates the content dedup.
  const base = Date.now();
  let now = base;
  const origNow = Date.now;
  Date.now = () => now;
  try {
    antiLoopCheck('roberto', 'alma', 'confirma la reunion de manana con el equipo');
    now += 15 * 60000; // +15 min
    const r2 = antiLoopCheck('roberto', 'alma', 'confirma la reunion de manana con el equipo');
    assert.ok(r2 && r2.ok === false && r2.reason === 'dedup', 'expected dedup (1h window), got: ' + JSON.stringify(r2));
    assert.strictEqual(ANTILOOP.dropped.hash, 1);
  } finally { Date.now = origNow; }
});

t('F3-01: SLOW loop with different content cut by hourly limit', () => {
  reset();
  const base = Date.now();
  let now = base;
  const origNow = Date.now;
  Date.now = () => now;
  try {
    // 10 messages in 45 min with different content (rewrite): they pass.
    // 5 min between messages -> the 10th lands at minute 45, inside the hour.
    for (let i = 0; i < 10; i++) {
      assert.strictEqual(antiLoopCheck('roberto', 'alma', 'msg lento ' + i).ok, true);
      now += 5 * 60000; // +5 min between messages
    }
    // The 11th in the same hour falls to the hourly limit (pairHourMax=10).
    const r = antiLoopCheck('roberto', 'alma', 'msg lento 11');
    assert.ok(r && r.ok === false && r.reason === 'rate', 'expected hourly rate, got: ' + JSON.stringify(r));
    assert.strictEqual(ANTILOOP.dropped.pair, 1);
  } finally { Date.now = origNow; }
});

t('F3-01: hourly limit does NOT break normal legitimate traffic', () => {
  reset();
  // 5 one-off messages in an hour: they pass without issue.
  const base = Date.now();
  let now = base;
  const origNow = Date.now;
  Date.now = () => now;
  try {
    for (let i = 0; i < 5; i++) {
      assert.strictEqual(antiLoopCheck('roberto', 'alma', 'peticion ' + i).ok, true);
      now += 12 * 60000; // +12 min
    }
  } finally { Date.now = origNow; }
});

t('F3-01: rollback compensates the hourly mark (same admission)', () => {
  reset();
  const base = Date.now();
  let now = base;
  const origNow = Date.now;
  Date.now = () => now;
  try {
    const r = antiLoopCheck('roberto', 'alma', 'msg rollback');
    assert.ok(r && r.ok === true, 'expected ok, got: ' + JSON.stringify(r));
    // After the rollback, the hourly mark must disappear.
    antiLoopRollback(r.admission);
    assert.strictEqual(ANTILOOP.pairHours.size, 0, 'pairHours must be empty after rollback');
    // And the next message passes (no limit consumed).
    assert.strictEqual(antiLoopCheck('roberto', 'alma', 'msg rollback 2').ok, true);
  } finally { Date.now = origNow; }
});

t('request_id: alternating ping-pong cut by EDGE (faster than counting)', () => {
  reset();
  ANTILOOP.pairMax = 1000; // isolate from rate
  const rid = 'aquaponics-united-20260811-0007';
  // A->B (req), B->A (resp): edges (A,B) and (B,A) — legitimate, passes.
  assert.strictEqual(antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' msg 1').ok, true);
  assert.strictEqual(antiLoopCheck('alma', 'roberto', 'INFORM ' + rid + ' respuesta').ok, true);
  // A->B again with the SAME rid: edge (A,B) already traversed -> cycle
  const r = antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' msg 3');
  assert.ok(r && r.ok === false && r.reason === 'cycle', 'expected cycle (repeated edge), got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.cycle, 1);
});

t('request_id: star pattern (different edges) short-circuits by count', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  const rid = 'aquaponics-united-20260811-0008';
  // 4 different edges (A->B, A->C, A->D, B->A): all pass, count grows.
  assert.strictEqual(antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' 1').ok, true);
  assert.strictEqual(antiLoopCheck('roberto', 'paco', 'REQUEST ' + rid + ' 2').ok, true);
  assert.strictEqual(antiLoopCheck('roberto', 'pepa', 'REQUEST ' + rid + ' 3').ok, true);
  assert.strictEqual(antiLoopCheck('alma', 'roberto', 'INFORM ' + rid + ' 4').ok, true);
  // 5th appearance with a new edge: count 5 > reqMax=4 -> short-circuit
  const r = antiLoopCheck('alma', 'paco', 'INFORM ' + rid + ' 5');
  assert.ok(r && r.ok === false && r.reason === 'request', 'expected request, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.request, 1);
});

t('different request_id does not short-circuit', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  for (let i = 1; i <= 10; i++) {
    const rid = 'aquaponics-united-20260811-' + String(1000 + i);
    // DIFFERENT bodies: the rid must not short-circuit on its own. If the
    // body were the same, F2-05 (canonical dedup after stripRids) would
    // correctly drop it as repeated content.
    assert.strictEqual(antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' mensaje numero ' + i).ok, true);
  }
});

t('without request_id it does not short-circuit even with many messages', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  ANTILOOP.pairHourMax = 1000; // F3-01: also isolate the hourly limit
  for (let i = 1; i <= 20; i++) {
    assert.strictEqual(antiLoopCheck('roberto', 'alma', 'conversacion normal ' + i).ok, true);
  }
});

t('count short-circuit: only alerts ONCE per request_id', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  ANTILOOP.reqMax = 2; // lowered on purpose
  // Pattern with new edges on each message: the count is what cuts.
  const rid = 'aquaponics-united-20260811-0009';
  const destinos = ['alma', 'paco', 'pepa'];
  let drops = 0;
  for (let i = 1; i <= 12; i++) {
    const r = antiLoopCheck('roberto', destinos[i % 3], 'REQUEST ' + rid + ' m' + i);
    if (r && !r.ok) drops++;
  }
  assert.ok(drops > 0, 'there should be drops by count');
  assert.strictEqual(ANTILOOP.dropped.request, 1); // only the first one that trips
});

t('order: the repeated edge cuts BEFORE the pair rate', () => {
  reset();
  ANTILOOP.pairMax = 3;
  const rid = 'aquaponics-united-20260811-0010';
  // 4 messages pass (2 unique edges), the 5th repeats the edge (A,B)
  // -> cycle, BEFORE rate (3) could trip.
  antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' a');
  antiLoopCheck('alma', 'roberto', 'INFORM ' + rid + ' b');
  antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' c');
  antiLoopCheck('alma', 'roberto', 'INFORM ' + rid + ' d');
  const r = antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' e');
  assert.ok(r && r.ok === false && r.reason === 'cycle', 'expected cycle, got: ' + JSON.stringify(r));
});

// --- Protocol envelope (norma v1.3) ---

t('envelope: parseEnvelope extracts JSON and rest', () => {
  const p = parseEnvelope('[env] {"rid":"aquaponics-united-20260811-0001","hops":1,"trace":["roberto","alma"],"expires":1755000000000}\nREQUEST hola');
  assert.ok(p, 'should parse');
  assert.strictEqual(p.env.rid, 'aquaponics-united-20260811-0001');
  assert.strictEqual(p.env.hops, 1);
  assert.deepStrictEqual(p.env.trace, ['roberto', 'alma']);
  assert.strictEqual(p.rest, 'REQUEST hola');
});

t('envelope: message without [env] line is not parsed', () => {
  assert.strictEqual(parseEnvelope('REQUEST normal sin envelope'), null);
  assert.strictEqual(parseEnvelope('[env] json-malformado'), null);
});

t('envelope: stampEnvelope creates new envelope (hops=1, trace, expires, rid)', () => {
  const stamped = stampEnvelope('REQUEST aquaponics-united-20260811-0002: ¿puedes revisar?', 'roberto', 'alma');
  const p = parseEnvelope(stamped);
  assert.ok(p, 'stamp must create envelope: ' + stamped);
  assert.strictEqual(p.env.hops, 1);
  assert.deepStrictEqual(p.env.trace, ['roberto', 'alma']);
  assert.ok(p.env.expires > Date.now(), 'expires future');
  assert.strictEqual(p.env.rid, 'aquaponics-united-20260811-0002');
  assert.strictEqual(p.rest, 'REQUEST aquaponics-united-20260811-0002: ¿puedes revisar?');
});

t('envelope: stampEnvelope updates existing envelope (hops++, trace++)', () => {
  const envText = signEnvelope({rid: 'aquaponics-united-20260811-0003', hops: 1, trace: ['roberto', 'alma'], expires: 1755000000000}, 'INFORM respuesta');
  const stamped = stampEnvelope(envText, 'alma', 'roberto');
  const p = parseEnvelope(stamped);
  assert.ok(p);
  assert.strictEqual(p.env.hops, 2);
  assert.deepStrictEqual(p.env.trace, ['roberto', 'alma', 'roberto']);
  assert.strictEqual(p.env.expires, 1755000000000); // keeps the original expires
  assert.strictEqual(p.rest, 'INFORM respuesta');
});

t('envelope: expired envelope is dropped', () => {
  reset();
  const text = signEnvelope({rid: 'aquaponics-united-20260811-0004', hops: 1, trace: ['roberto', 'alma'], expires: Date.now() - 1000}, 'REQUEST caducado');
  const r = antiLoopCheck('roberto', 'alma', text);
  assert.ok(r && r.ok === false && r.reason === 'expired', 'expected expired, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.expired, 1);
});

t('envelope: hops >= maxHops is dropped', () => {
  reset();
  const text = signEnvelope({rid: 'aquaponics-united-20260811-0005', hops: 3, trace: ['a', 'b', 'c'], expires: Date.now() + 3600000}, 'REQUEST');
  const r = antiLoopCheck('c', 'd', text);
  assert.ok(r && r.ok === false && r.reason === 'hops', 'expected hops, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.hops, 1);
});

t('envelope: creative loop (NEW rid, NEW text) cut by trace edge', () => {
  reset();
  // A->B (REQUEST rid1): stamp -> trace [roberto,alma], hops 1.
  antiLoopCheck('roberto', 'alma', signEnvelope({rid: 'aquaponics-united-20260811-0101', hops: 1, trace: ['roberto'], expires: Date.now() + 3600000}, 'REQUEST uno'));
  // B->A (INFORM rid2 — NEW rid, NEW text): B keeps the received envelope
  // [roberto,alma] (hops 1). Edge (alma,roberto) is NOT there -> passes.
  antiLoopCheck('alma', 'roberto', signEnvelope({rid: 'aquaponics-united-20260811-0102', hops: 1, trace: ['roberto', 'alma'], expires: Date.now() + 3600000}, 'INFORM dos'));
  // A->B again (NEW rid): A keeps the envelope [roberto,alma,roberto]
  // (hops 2). Edge (roberto,alma) IS already in the trace -> cycle.
  const r = antiLoopCheck('roberto', 'alma', signEnvelope({rid: 'aquaponics-united-20260811-0103', hops: 2, trace: ['roberto', 'alma', 'roberto'], expires: Date.now() + 3600000}, 'REQUEST tres'));
  assert.ok(r && r.ok === false && r.reason === 'cycle', 'expected cycle, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.dropped.cycle, 1);
});

t('envelope: legitimate A->B->A reply is NOT dropped (distinct edges)', () => {
  reset();
  // REQUEST A->B: the sender envelope only carries its own origin.
  assert.strictEqual(antiLoopCheck('roberto', 'alma', '[env] {"rid":"aquaponics-united-20260811-0201","hops":1,"trace":["roberto"],"expires":' + (Date.now() + 3600000) + '}\nREQUEST').ok, true);
  // Reply B->A: B keeps the received envelope [roberto,alma]; the
  // edge (alma,roberto) is not in the trace -> passes.
  assert.strictEqual(antiLoopCheck('alma', 'roberto', '[env] {"rid":"aquaponics-united-20260811-0202","hops":1,"trace":["roberto","alma"],"expires":' + (Date.now() + 3600000) + '}\nINFORM').ok, true);
  assert.strictEqual(ANTILOOP.dropped.cycle, 0);
});

t('envelope: envelope rid feeds the short-circuit', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  const rid = 'aquaponics-united-20260811-0301';
  // Same rid inside the envelope, distinct edges (star), trace without
  // prior edges (only the origin) so the count is what cuts.
  antiLoopCheck('roberto', 'alma', '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\n1');
  antiLoopCheck('roberto', 'paco', '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\n2');
  antiLoopCheck('roberto', 'pepa', '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\n3');
  antiLoopCheck('alma', 'roberto', '[env] {"rid":"' + rid + '","hops":1,"trace":["alma"]}\n4');
  const r = antiLoopCheck('alma', 'paco', '[env] {"rid":"' + rid + '","hops":1,"trace":["alma"]}\n5');
  assert.ok(r && r.ok === false && r.reason === 'request', 'expected request, got: ' + JSON.stringify(r));
});

// ---- F2-02: strict type/range validation of the envelope ----

t('F2-02: hops -Infinity / negative / float / string rejected (invalid envelope)', () => {
  reset();
  // -Infinity: previously passed (truthy) and nullified the hops limit.
  assert.strictEqual(parseEnvelope('[env] {"hops":"-Infinity","trace":[],"expires":' + (Date.now() + 3600000) + '}\nREQUEST'), null, '-Infinity must be rejected');
  assert.strictEqual(parseEnvelope('[env] {"hops":-1,"trace":[],"expires":' + (Date.now() + 3600000) + '}\nREQUEST'), null, 'negative hops must be rejected');
  assert.strictEqual(parseEnvelope('[env] {"hops":1.5,"trace":[],"expires":' + (Date.now() + 3600000) + '}\nREQUEST'), null, 'float hops must be rejected');
  assert.strictEqual(parseEnvelope('[env] {"hops":"2","trace":[],"expires":' + (Date.now() + 3600000) + '}\nREQUEST'), null, 'string hops must be rejected (no coercion)');
});

t('F2-02: invalid expires (string / 0 / negative) rejected — not immortal', () => {
  reset();
  assert.strictEqual(parseEnvelope('[env] {"hops":1,"trace":[],"expires":"not-a-date"}\nREQUEST'), null, 'string expires must be rejected');
  assert.strictEqual(parseEnvelope('[env] {"hops":1,"trace":[],"expires":0}\nREQUEST'), null, 'expires 0 must be rejected');
  assert.strictEqual(parseEnvelope('[env] {"hops":1,"trace":[],"expires":-5}\nREQUEST'), null, 'negative expires must be rejected');
});

t('F2-02: non-string trace elements rejected; missing trace -> default []', () => {
  reset();
  assert.strictEqual(parseEnvelope('[env] {"hops":1,"trace":[123],"expires":' + (Date.now() + 3600000) + '}\nREQUEST'), null, 'trace with numbers must be rejected');
  const p = parseEnvelope('[env] {"hops":1,"expires":' + (Date.now() + 3600000) + '}\nREQUEST');
  assert.ok(p && Array.isArray(p.env.trace) && p.env.trace.length === 0, 'missing trace -> default []');
});

// ---- F2-06: full first line as JSON (no non-greedy regex) ----

t('F2-06: envelope with } inside a JSON string is fully parsed', () => {
  reset();
  const text = '[env] {"rid":"aquaponics-united-20260811-0401","hops":1,"trace":["roberto"],"expires":' + (Date.now() + 3600000) + ',"meta":"texto } con llave"}\nREQUEST llaves';
  const p = parseEnvelope(text);
  assert.ok(p, 'must parse the full JSON');
  assert.strictEqual(p.env.meta, 'texto } con llave');
  assert.strictEqual(p.rest, 'REQUEST llaves');
  // And antiLoopCheck must not crash nor lose the envelope:
  assert.strictEqual(antiLoopCheck('roberto', 'alma', text).ok, true);
});

// ---- F2-04: envelope rid takes precedence over the text ----

t('F2-04: envelope rid wins; free text does NOT pollute the counter', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  const envRid = 'aquaponics-united-20260811-0501';
  // Text mentioning another rid (room-...) but the envelope says envRid:
  // the counter must track envRid, NOT the rid in the text.
  const text = '[env] {"rid":"' + envRid + '","hops":1,"trace":["roberto"]}\nusa room-20260811-0007 por favor';
  const r = antiLoopCheck('roberto', 'alma', text);
  assert.strictEqual(r.ok, true, 'must not be dropped');
  assert.ok(ANTILOOP.requests.has(envRid), 'must track the envelope rid');
  assert.ok(!ANTILOOP.requests.has('room-20260811-0007'), 'must NOT track the free-text rid');
});

t('F2-04: without envelope, free text is used as best-effort fallback', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  const r = antiLoopCheck('roberto', 'alma', 'REQUEST room-20260811-0007 sin envelope');
  assert.strictEqual(r.ok, true);
  assert.ok(ANTILOOP.requests.has('room-20260811-0007'), 'textual fallback still active without envelope');
});

t('F2-04: extractRid does not touch the envelope rid (env.rid authoritative)', () => {
  reset();
  const parsed = parseEnvelope('[env] {"rid":"aquaponics-united-20260811-0601","hops":1,"trace":["roberto"]}\nREQUEST');
  assert.strictEqual(resolveRid(parsed, '[env] {"rid":"x-20260811-9999"}\nREQUEST'), 'aquaponics-united-20260811-0601', 'env.rid wins over the text');
});

// ---- F2-08: hard cap on requests map entries ----

t('F2-08: requestMax evicts the LRU entry (by last)', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  ANTILOOP.requestMax = 3;
  const t0 = Date.now();
  antiLoopCheck('roberto', 'alma', '[env] {"rid":"aquaponics-united-20260811-0701","hops":1,"trace":["roberto"]}\nA');
  antiLoopCheck('roberto', 'paco', '[env] {"rid":"aquaponics-united-20260811-0702","hops":1,"trace":["roberto"]}\nB');
  antiLoopCheck('roberto', 'pepa', '[env] {"rid":"aquaponics-united-20260811-0703","hops":1,"trace":["roberto"]}\nC');
  // Fourth distinct entry -> the map must stay at 3 (evicts the oldest).
  antiLoopCheck('roberto', 'paco', '[env] {"rid":"aquaponics-united-20260811-0704","hops":1,"trace":["roberto"]}\nD');
  assert.strictEqual(ANTILOOP.requests.size, 3, 'requestMax must bound the map');
  assert.ok(!ANTILOOP.requests.has('aquaponics-united-20260811-0701'), 'must evict the oldest entry');
  void t0;
});

// ---- F2-09: observable hash eviction + documented degradation ----

t('F2-09: hashMax eviction increments evictedHashes', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  ANTILOOP.hashMax = 5;
  const t0 = Date.now();
  for (let i = 0; i < 6; i++) {
    antiLoopCheck('roberto', 'alma', '[env] {"rid":"aquaponics-united-20260811-080' + i + '","hops":1,"trace":["roberto"]}\nmsg ' + i);
  }
  assert.strictEqual(ANTILOOP.hashes.size, 5, 'hashMax bounds the map');
  assert.strictEqual(ANTILOOP.evictedHashes, 1, 'one eviction must be counted');
  void t0;
});

// ---- F2-10: state rollback when the publish fails ----

t('F2-10: antiLoopRollback compensates consumed hash, pair and rid', () => {
  reset();
  ANTILOOP.pairMax = 3;
  const rid = 'aquaponics-united-20260811-0901';
  const text = '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\nREQUEST rollback';
  const beforeHashes = ANTILOOP.hashes.size;
  const beforePairs = ANTILOOP.pairs.size;
  const beforeReqs = ANTILOOP.requests.size;
  const a1 = antiLoopCheck('roberto', 'alma', text);
  assert.strictEqual(a1.ok, true, 'passes');
  assert.strictEqual(ANTILOOP.hashes.size, beforeHashes + 1, 'hash registered');
  assert.strictEqual(ANTILOOP.pairs.get('roberto|alma').length, 1, 'pair registered');
  assert.strictEqual(ANTILOOP.requests.get(rid).count, 1, 'rid registered');
  // Simulates a failed publishDM -> rollback with the admission token:
  antiLoopRollback(a1.admission);
  assert.strictEqual(ANTILOOP.hashes.size, beforeHashes, 'hash compensated');
  assert.strictEqual(ANTILOOP.pairs.size, beforePairs, 'pair compensated');
  assert.strictEqual(ANTILOOP.requests.size, beforeReqs, 'rid compensated');
});

t('F2-10: after rollback, the sender retry does NOT hit a false positive', () => {
  reset();
  ANTILOOP.pairMax = 3;
  const rid = 'aquaponics-united-20260811-0902';
  const text = '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\nREQUEST reintento';
  const a1 = antiLoopCheck('roberto', 'alma', text);
  assert.strictEqual(a1.ok, true, 'attempt 1 passes');
  antiLoopRollback(a1.admission); // publish failed
  const a2 = antiLoopCheck('roberto', 'alma', text);
  assert.strictEqual(a2.ok, true, 'retry passes (no false positive)');
  assert.strictEqual(ANTILOOP.dropped.dedup || 0, 0, 'must not count dedup');
  assert.strictEqual(ANTILOOP.dropped.pair, 0, 'must not count rate');
});

// ---- F2-R02: rollback by ADMISSION TOKEN (concurrency, audit 3) ----
// Between the antiLoopCheck() COMMIT and the post-publishDM rollback there
// is an `await` that releases the event loop: another concurrent admission
// may touch the same structures. The rollback receives the COMMIT admission
// token to undo EXACTLY that admission (specific pair mark, specific
// request instance, hash with specific timestamp), never another one.

t('F2-R02: two concurrent admissions of the same pair — rolling one back does NOT remove the other mark', () => {
  reset();
  ANTILOOP.pairMax = 3;
  // m1 and m2 A->B admitted: pairs[A|B] = [mark1, mark2]
  const a1 = antiLoopCheck('roberto', 'alma', 'm1');
  const a2 = antiLoopCheck('roberto', 'alma', 'm2');
  assert.strictEqual(a1.ok && a2.ok, true, 'both admitted');
  assert.strictEqual(ANTILOOP.pairs.get('roberto|alma').length, 2, 'two marks');
  // m1 fails on publish -> rollback of m1 (NOT array pop):
  antiLoopRollback(a1.admission);
  const rest = ANTILOOP.pairs.get('roberto|alma');
  assert.strictEqual(rest.length, 1, 'only the m2 mark remains');
  assert.strictEqual(rest[0].id, a2.admission.admissionId, 'remaining mark is m2 (identity by admissionId)');
});

t('F2-R02: reverse order — m2 fails first, m1 stays registered', () => {
  reset();
  ANTILOOP.pairMax = 3;
  const a1 = antiLoopCheck('roberto', 'alma', 'm1');
  const a2 = antiLoopCheck('roberto', 'alma', 'm2');
  antiLoopRollback(a2.admission); // m2 fails (completed first)
  const rest = ANTILOOP.pairs.get('roberto|alma');
  assert.strictEqual(rest.length, 1, 'one mark remains');
  assert.strictEqual(rest[0].id, a1.admission.admissionId, 'remaining mark is m1 (identity by admissionId)');
});

t('F2-R02: two concurrent admissions of the same RID (distinct edges) — rolling one back decrements ONLY its own', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  const rid = 'aquaponics-united-20260811-1101';
  const a1 = antiLoopCheck('roberto', 'alma', '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\nA');
  const a2 = antiLoopCheck('alma', 'paco', '[env] {"rid":"' + rid + '","hops":1,"trace":["alma"]}\nB');
  assert.strictEqual(a1.ok && a2.ok, true, 'both admitted');
  const r = ANTILOOP.requests.get(rid);
  assert.strictEqual(r.count, 2, 'count=2 (two admissions)');
  assert.ok(r.edges.has('roberto|alma') && r.edges.has('alma|paco'), 'both edges');
  // A->B fails -> rollback: count=1, edge A|B removed, edge B|C intact.
  antiLoopRollback(a1.admission);
  const r2 = ANTILOOP.requests.get(rid);
  assert.strictEqual(r2.count, 1, 'count decremented ONLY one unit');
  assert.ok(!r2.edges.has('roberto|alma'), 'edge of the failed admission removed');
  assert.ok(r2.edges.has('alma|paco'), 'edge of the concurrent admission intact');
});

t('F2-R02: same RID and SAME edge — the check cuts by cycle (the Map case is defense in depth)', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  const rid = 'aquaponics-united-20260811-1102';
  // Two A->B messages with the SAME rid (different text -> dedup does not cut):
  const a1 = antiLoopCheck('roberto', 'alma', '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\nX');
  assert.strictEqual(a1.ok, true, 'first admission OK');
  assert.strictEqual(ANTILOOP.requests.get(rid).edges.get('roberto|alma'), 1, 'edge with 1 occurrence (Map)');
  // The 2nd with the SAME edge cuts by cycle BEFORE the COMMIT:
  const r = antiLoopCheck('roberto', 'alma', '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\nY');
  assert.strictEqual(r.ok, false, 'second with same edge is dropped');
  assert.strictEqual(r.reason, 'cycle', 'reason=cycle');
  assert.strictEqual(ANTILOOP.requests.get(rid).count, 1, 'count intact (1 real admission)');
  // Rolling back the only admission clears the entry completely:
  antiLoopRollback(a1.admission);
  assert.ok(!ANTILOOP.requests.has(rid), 'entry clean after rollback');
});

t('F2-R02: eviction + re-creation of the same RID — the old admission rollback does NOT touch the new one', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  ANTILOOP.requestMax = 2;
  const rid = 'aquaponics-united-20260811-1103';
  // Admission 1 (RID X) + filler to evict:
  const a1 = antiLoopCheck('roberto', 'alma', '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\nA');
  antiLoopCheck('roberto', 'paco', '[env] {"rid":"aquaponics-united-20260811-1199","hops":1,"trace":["roberto"]}\nB');
  // Third distinct entry evicts the oldest (RID X left the map):
  antiLoopCheck('roberto', 'pepa', '[env] {"rid":"aquaponics-united-20260811-1198","hops":1,"trace":["roberto"]}\nC');
  assert.ok(!ANTILOOP.requests.has(rid), 'RID X evicted');
  // The same RID reappears (new instance):
  const a2 = antiLoopCheck('roberto', 'alma', '[env] {"rid":"' + rid + '","hops":1,"trace":["roberto"]}\nD');
  assert.ok(ANTILOOP.requests.has(rid), 'RID X re-registered (new entry)');
  // Rolling back the OLD admission (a1): must not touch the new entry.
  antiLoopRollback(a1.admission);
  const r = ANTILOOP.requests.get(rid);
  assert.strictEqual(r.count, 1, 'the new entry is NOT decremented (distinct instance)');
  assert.ok(r.edges.has('roberto|alma'), 'the new entry edge intact');
});

// ---- F2-R03/R04: MONOTONIC admission identity (audit 3 bis) ----
// Date.now() is NOT a unique identifier: two admissions can land on the
// same millisecond (same pairTs/hashTs). If the rollback looked up by
// timestamp, `indexOf` would return the FIRST match (the mark of ANOTHER
// admission) and the hash re-registered in the same ms would be confused
// with the original. Fix: monotonic `admissionId` counter; the marks are
// stored as {id, ts} and the rollback looks up by id, never by ts. These
// tests use a fake clock to force exactly the auditor scenario.

function withFakeNow(fakeNow, fn) {
  const realNow = Date.now;
  Date.now = () => fakeNow;
  try { fn(); }
  finally { Date.now = realNow; }
}

t('F2-R03: two admissions in the SAME ms — rolling m2 back leaves the m1 mark (identity by id)', () => {
  reset();
  ANTILOOP.pairMax = 3;
  withFakeNow(1000, () => {
    const a1 = antiLoopCheck('roberto', 'alma', 'm1');
    const a2 = antiLoopCheck('roberto', 'alma', 'm2');
    assert.strictEqual(a1.ok && a2.ok, true, 'both admitted');
    assert.strictEqual(a2.admission.admissionId, a1.admission.admissionId + 1, 'monotonic ids');
    const arr = ANTILOOP.pairs.get('roberto|alma');
    assert.strictEqual(arr.length, 2, 'two marks');
    assert.strictEqual(arr[0].ts, arr[1].ts, 'both with the SAME timestamp (the auditor scenario)');
    assert.notStrictEqual(arr[0].id, arr[1].id, 'but with distinct ids');
    // m2 fails -> rollback of m2: MUST leave the m1 mark (with the
    // timestamp fix it would have removed the FIRST match = m1).
    antiLoopRollback(a2.admission);
    const rest = ANTILOOP.pairs.get('roberto|alma');
    assert.strictEqual(rest.length, 1, 'one mark remains');
    assert.strictEqual(rest[0].id, a1.admission.admissionId, 'the remaining mark is M1 (not M2)');
  });
});

t('F2-R03: reverse order in the same ms — rolling m1 back leaves the m2 mark', () => {
  reset();
  ANTILOOP.pairMax = 3;
  withFakeNow(1000, () => {
    const a1 = antiLoopCheck('roberto', 'alma', 'm1');
    const a2 = antiLoopCheck('roberto', 'alma', 'm2');
    antiLoopRollback(a1.admission); // m1 fails
    const rest = ANTILOOP.pairs.get('roberto|alma');
    assert.strictEqual(rest.length, 1, 'one mark remains');
    assert.strictEqual(rest[0].id, a2.admission.admissionId, 'the remaining mark is M2');
  });
});

t('F2-R04: hash evicted and re-registered with the SAME ts — the old admission rollback does NOT remove the new entry', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  ANTILOOP.hashMax = 2;
  withFakeNow(1000, () => {
    const a1 = antiLoopCheck('roberto', 'alma', 'texto X'); // hash H, id=1
    const H = a1.admission.hash;
    antiLoopCheck('roberto', 'paco', 'texto Y');            // id=2, hashes={H,H2}
    antiLoopCheck('roberto', 'pepa', 'texto Z');            // id=3 -> hashMax evicts H (the oldest)
    assert.ok(!ANTILOOP.hashes.has(H), 'H evicted by hashMax');
    const a2 = antiLoopCheck('roberto', 'alma', 'texto X'); // same content -> re-registers H, id=4
    assert.ok(ANTILOOP.hashes.has(H), 'H re-registered');
    assert.strictEqual(ANTILOOP.hashes.get(H).id, a2.admission.admissionId, 'the new entry has the new id');
    assert.strictEqual(ANTILOOP.hashes.get(H).ts, 1000, 'same ts as the original (the auditor scenario)');
    // Rolling back the OLD admission (a1): with ts identity it would have
    // removed the new entry; by id it must not touch it.
    antiLoopRollback(a1.admission);
    assert.ok(ANTILOOP.hashes.has(H), 'the new entry remains (identity by id, not by ts)');
    assert.strictEqual(ANTILOOP.hashes.get(H).id, a2.admission.admissionId, 'still the a2 entry');
  });
});

// ---- F2-R01: TRANSACTIONAL request state (audit 2) ----
// The request_id state (count/edges) must only be consumed when the
// message passes ALL defenses (envelope, request, dedup, rate). A later
// dedup/rate drop must NOT inflate the counter nor register edges —
// previously the state was mutated before those defenses and an attacker
// could push count up to reqMax or seed edges with messages that were
// never admitted (false short-circuits and false cycles).

t('F2-R01: dedup drop (no rid) neither creates nor consumes request state', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  const text = 'mensaje sin rid duplicado';
  assert.strictEqual(antiLoopCheck('roberto', 'alma', text).ok, true, 'admitted');
  const beforeReqs = ANTILOOP.requests.size;
  const beforeHashes = ANTILOOP.hashes.size;
  const r = antiLoopCheck('roberto', 'alma', text); // exact duplicate
  assert.ok(r && r.ok === false && r.reason === 'dedup', 'expected dedup, got: ' + JSON.stringify(r));
  assert.strictEqual(ANTILOOP.requests.size, beforeReqs, 'does not create request entries');
  assert.strictEqual(ANTILOOP.hashes.size, beforeHashes, 'the drop does not re-register the hash');
});

t('F2-R01: message dropped by RATE neither registers an edge nor consumes quota (no false cycle)', () => {
  reset();
  ANTILOOP.pairMax = 2;
  const rid = 'aquaponics-united-20260811-1002';
  // Fills the A->B rate window (no rid):
  assert.strictEqual(antiLoopCheck('roberto', 'alma', 'mensaje normal 1').ok, true);
  assert.strictEqual(antiLoopCheck('roberto', 'alma', 'mensaje normal 2').ok, true);
  // REQUEST with new rid A->B: falls to RATE (full window).
  const r = antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' peticion');
  assert.ok(r && r.ok === false && r.reason === 'rate', 'expected rate, got: ' + JSON.stringify(r));
  // The rid state must NOT have been created/consumed:
  assert.ok(!ANTILOOP.requests.has(rid), 'rid must NOT be registered if the message falls to rate');
  // Simulates time passing (rate window expired):
  ANTILOOP.pairs.delete('roberto|alma');
  // Legitimate retry of the same rid A->B: MUST pass (no false cycle).
  const r2 = antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' peticion');
  assert.strictEqual(r2.ok, true, 'legitimate retry must pass, got: ' + JSON.stringify(r2));
  assert.strictEqual(ANTILOOP.requests.get(rid).count, 1, 'count=1 after the real admission');
  assert.ok(ANTILOOP.requests.get(rid).edges.has('roberto|alma'), 'edge registered ONLY after real admission');
});

t('F2-R01: request drops (already tripped) do NOT keep incrementing the counter', () => {
  reset();
  ANTILOOP.pairMax = 1000;
  ANTILOOP.reqMax = 2;
  const rid = 'aquaponics-united-20260811-1003';
  // m1, m2 admitted (count=2). m3 (new edge) -> request drop (tripped).
  assert.strictEqual(antiLoopCheck('roberto', 'alma', 'REQUEST ' + rid + ' 1').ok, true);
  assert.strictEqual(antiLoopCheck('roberto', 'paco', 'REQUEST ' + rid + ' 2').ok, true);
  const r = antiLoopCheck('roberto', 'pepa', 'REQUEST ' + rid + ' 3');
  assert.ok(r && r.ok === false && r.reason === 'request', 'expected request, got: ' + JSON.stringify(r));
  // The counter reflects ONLY real admissions (2), not the later drops:
  assert.strictEqual(ANTILOOP.requests.get(rid).count, 2, 'count = real admissions');
  assert.strictEqual(ANTILOOP.dropped.request, 1, 'short-circuit counted once');
});

console.log('\nResult:', passed, 'ok,', failed, 'fail');
fs.rmSync(TEST_DIR, {recursive: true, force: true});
process.exit(failed ? 1 : 0);
