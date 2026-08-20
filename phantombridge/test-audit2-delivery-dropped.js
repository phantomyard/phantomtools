// AUDIT-2 regression test (2nd security audit): bounds the `delivery` ledger
// (MEDIO-2: unbounded growth -> I/O-amplification DoS via per-entry fsync) and
// fixes `DROPPED_MAX` overflow breaking the recovery guarantee (MEDIO-3).
//
// A) delivery ledger:
//    - `delivered` entries expire after DELIVERY_TTL_SECS (lazy eviction).
//    - `pending` entries also expire after the TTL (recoverable by range).
//    - the hard cap DELIVERY_MAX evicts oldest-first (FIFO by ts).
// B) dropped ledger overflow:
//    - dropping > DROPPED_MAX distinct ids sets persistent `droppedOverflow`.
//    - while `droppedOverflow` is set, releasePendingSinceIfRecovered() MUST
//      NOT clear the since-anchor (the evicted ids need range recovery).
//    - once the survivor ledger drains, the flag clears and the anchor MAY
//      release.
//
// We exercise the real bridge functions against an isolated durable state file
// (same pattern as test-persist-audit-alto3.js).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'audit2-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  markDelivery, deliveryStatus, recordDropped, recoverDropped,
  releasePendingSinceIfRecovered, _setBridgeStateForTest, STATE_FILE,
} = bridge;

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

function resetState(overrides) {
  _setBridgeStateForTest(Object.assign({
    relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, delivery: {},
  }, overrides || {}));
}

assert.strictEqual(STATE_FILE, tmpState, 'STATE_FILE debe apuntar al temp para el test');

console.log('AUDIT-2: delivery ledger acotado + DROPPED_MAX overflow:');

// ---- A) delivery ledger TTL/cap (opción B: watermark para delivered) ----
t('delivered NO expira por reloj de pared (watermark, opción B)', () => {
  resetState();
  markDelivery('a-1', 'delivered');
  // Forzar ts muy antiguo (hace 2h) pero lastSeen SIN avanzar (downtime).
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false,
    delivery: {'a-1': {status: 'delivered', ts: Math.floor(Date.now() / 1000) - (2 * 3600)}}});
  // Cualquier markDelivery posterior dispara el sweep lazy.
  markDelivery('a-2', 'pending');
  // delivered NO debe expirar por reloj: lastSeen no avanzó más allá de su ventana.
  assert.strictEqual(deliveryStatus('a-1'), 'delivered', 'delivered sobrevive a downtime largo (watermark)');
  assert.strictEqual(deliveryStatus('a-2'), 'pending', 'a-2 presente');
});

t('delivered expira SOLO si el watermark de recuperación avanzó por delante de su ventana', () => {
  resetState();
  const now = Math.floor(Date.now() / 1000);
  // a-1 entregado hace 10 min, pero el WATERMARK DE RECUPERACIÓN (recoveryWatermark)
  // avanzó a hace 5 min (el bridge PROCESÓ/admitió eventos tras a-1) -> el replay
  // (since) ya no lo cubre -> expira. NOTA (Fase 2 / AUDIT-10): la expiración
  // NO depende de lastSeen (cursor de recepción), sino del watermark real.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now - 300, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: now - 300,
    delivery: {'a-1': {status: 'delivered', ts: now - 600}}});
  markDelivery('a-2', 'pending');
  assert.strictEqual(deliveryStatus('a-1'), null, 'delivered expira cuando el watermark ya pasó su ventana');
  assert.strictEqual(deliveryStatus('a-2'), 'pending', 'a-2 presente');
});

t('pending expira por reloj de pared (PENDING_TTL_SECS)', () => {
  resetState();
  // pending de hace 25h (PENDING_TTL_SECS=24h) -> expira.
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false,
    delivery: {'p-1': {status: 'pending', ts: Math.floor(Date.now() / 1000) - (25 * 3600)}}});
  markDelivery('p-2', 'delivered');
  assert.strictEqual(deliveryStatus('p-1'), null, 'pending expirado (>24h)');
});

t('cap duro DELIVERY_MAX: evicta pending viejos, NO delivered inmaduros (fail-closed)', () => {
  resetState();
  const now = Math.floor(Date.now() / 1000);
  // Llenar con pending viejos (evictables) para probar el cap FIFO por pending.
  const entry = {};
  for (let i = 0; i < 10050; i++) entry['x-' + i] = {status: 'pending', ts: now - 25 * 3600 + i};
  _setBridgeStateForTest({relay: 'ws://test.local', lastSeen: now, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false,
    delivery: entry});
  markDelivery('fresh', 'pending');
  const remaining = bridge.getBridgeState().delivery;
  const keys = Object.keys(remaining);
  assert.ok(keys.length <= 10001, 'ledger acotado tras cap: ' + keys.length);
  assert.ok(keys.includes('fresh'), 'la entrada nueva sobrevive');
});

// ---- B) DROPPED_MAX overflow ----
t('>DROPPED_MAX drops -> se marca droppedOverflow', () => {
  resetState();
  for (let i = 0; i < 5010; i++) recordDropped('d-' + i);
  const st = bridge.getBridgeState();
  assert.strictEqual(st.droppedOverflow, true, 'droppedOverflow activo');
  assert.ok(st.dropped.length <= 5000, 'ledger de drops acotado: ' + st.dropped.length);
});

t('con droppedOverflow, pendingSince NO se libera aunque dropped esté vacío', () => {
  resetState({pendingSince: 1000, dropped: [], droppedOverflow: true});
  releasePendingSinceIfRecovered();
  assert.strictEqual(bridge.getBridgeState().pendingSince, 1000, 'anchor se mantiene ante overflow');
});

t('sin overflow y dropped vacío -> pendingSince se libera', () => {
  resetState({pendingSince: 1000, dropped: [], droppedOverflow: false});
  releasePendingSinceIfRecovered();
  assert.strictEqual(bridge.getBridgeState().pendingSince, null, 'anchor liberado');
});

t('recover todos los drops -> droppedOverflow se limpia', () => {
  resetState({pendingSince: 1000, dropped: [{id: 'd-1', ts: 1}, {id: 'd-2', ts: 2}], droppedOverflow: true});
  recoverDropped('d-1');
  assert.strictEqual(bridge.getBridgeState().droppedOverflow, true, 'sigue en overflow (queda d-2)');
  recoverDropped('d-2');
  assert.strictEqual(bridge.getBridgeState().droppedOverflow, false, 'overflow limpio al drenar');
  releasePendingSinceIfRecovered();
  assert.strictEqual(bridge.getBridgeState().pendingSince, null, 'anchor liberado tras drenar');
});

t('droppedOverflow persiste en disco (serializacio/restore)', () => {
  resetState({pendingSince: 5, dropped: [{id: 'z-1', ts: 1}], droppedOverflow: true});
  if (fs.existsSync(tmpState)) fs.unlinkSync(tmpState);
  markDelivery('keep', 'delivered'); // fuerza flush durable
  const s = JSON.parse(fs.readFileSync(tmpState, 'utf8'));
  assert.strictEqual(s.droppedOverflow, true, 'droppedOverflow en el fichero');
  // restore como loadState()
  _setBridgeStateForTest({relay: s.relay || 'ws://test.local', lastSeen: s.lastSeen || 0,
    seenIds: s.seenIds || [], pendingSince: s.pendingSince != null ? s.pendingSince : null,
    dropped: s.dropped || [], droppedOverflow: !!s.droppedOverflow,
    delivery: s.delivery || {}});
  assert.strictEqual(bridge.getBridgeState().droppedOverflow, true, 'overflow restaurado tras restart');
});

// cleanup
resetState();
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
