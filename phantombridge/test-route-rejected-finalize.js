// 🟡 BAJO (auditoría): handleRoute() debe finalizar los rechazos DETERMINISTAS.
//
// El evento ya fue admitido como `pending` por handleIncomingGiftWrap() antes
// de llamar a handleRoute(). Si handleRoute() hace un `return` a secas en un
// rechazo que el retry NUNCA cambiará (agente inexistente, sin permiso,
// anti-loop bloqueado), la entrada `pending` queda consumiendo el ledger hasta
// PENDING_TTL_SECS — retries inútiles que nunca van a cambiar de resultado.
//
// Corrección: en esos tres caminos se llama finishDelivery(giftWrapId, false,
// true) (rejected=true) -> la entrada `pending` se elimina del ledger.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'route-rej-'));
const tmpState = path.join(tmpDir, 'state.json');
const baseConfig = require('./testlib.js').baseConfig();
baseConfig.stateFile = tmpState;
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));
process.env.PHANTOMBRIDGE_CONFIG = tmpConfigPath;

const bridge = require('./bridge.js');
const {
  handleRoute, markDelivery, deliveryStatus, _setBridgeStateForTest,
} = bridge;

let passed = 0, failed = 0;
let _chain = Promise.resolve();
function t(name, fn) {
  _chain = _chain.then(async () => {
    try { await fn(); console.log('  ok:', name); passed++; }
    catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
  });
}

// Estado base del ledger; admitimos un `pending` como haría
// handleIncomingGiftWrap() antes de delegar en handleRoute().
function freshLedger() {
  return {relay: 'ws://test.local', lastSeen: 0, seenIds: [], pendingSince: null,
    dropped: [], droppedOverflow: false, recoveryWatermark: 0, delivery: {}};
}

// handleRoute lanza publishDM a agentes; no queremos red real. Vamos a probar
// los TRES caminos de rechazo determinista que hacen `return` antes de llegar
// al publishDM, así que no necesita mock de publishDM salvo que el camino
// llegue al publish — que no es el caso. De todos modos atrapamos cualquier
// fallo de red con try/catch en el harness (publishDM se captura con .catch).
const UNKNOWN_FROM = 'remitente desconocido';

console.log('🟡 handleRoute: rechazos deterministas finalizan el pending (rejected):');

// 1) Agente desconocido: route.to no existe en CONFIG.agents.
t('agente inexistente -> finishDelivery rejected -> pending eliminado', async () => {
  _setBridgeStateForTest(freshLedger());
  // Admitir el pending como haría el handler real.
  assert.ok(markDelivery('gw-1', 'pending'), 'admitido pending');
  const fromPk = 'abc'.padEnd(64, '0');
  // @agente-inexistente: no existe en CONFIG.agents -> toPk undefined.
  const route = {to: 'agente-inexistente', text: 'hola'};
  // Envolvemos para que un posible publishDM fallido no tumbe el assert.
  await handleRoute(UNKNOWN_FROM, fromPk, route, 'gw-1').catch(() => {});
  assert.strictEqual(deliveryStatus('gw-1'), null,
    'pending eliminado tras agente inexistente (rejected)');
});

// 2) Sin permiso: routingAllowed() == false (default deny y sin regla).
t('sin permiso (routingAllowed false) -> finishDelivery rejected -> pending eliminado', async () => {
  _setBridgeStateForTest(freshLedger());
  // Elegimos un par sin regla de permisos y default deny: from -> to denegado.
  // CONFIG.agents real tiene remitente y el @to; el from no está en
  // routing.permissions y default deny -> routingAllowed false.
  assert.ok(markDelivery('gw-2', 'pending'), 'admitido pending');
  const fromPk = 'def'.padEnd(64, '0');
  // Tomamos un @to que SÍ existe pero cuyo from no tiene permiso.
  const toName = Object.keys(bridge.CONFIG.agents || {})[0] || 'dave';
  const route = {to: toName, text: 'hola'};
  await handleRoute(UNKNOWN_FROM, fromPk, route, 'gw-2').catch(() => {});
  assert.strictEqual(deliveryStatus('gw-2'), null,
    'pending eliminado tras sin permiso (rejected)');
});

// 3) Anti-loop bloqueado: antiLoopCheck() == false (mismo contenido duplicado).
t('anti-loop bloqueado -> finishDelivery rejected -> pending eliminado', async () => {
  _setBridgeStateForTest(freshLedger());
  assert.ok(markDelivery('gw-3', 'pending'), 'admitido pending');
  const fromPk = '123'.padEnd(64, '0');
  const toName = Object.keys(bridge.CONFIG.agents || {})[0] || 'dave';
  const route = {to: toName, text: '@' + toName + ' duplicado-para-antiloop'};
  // Ejecutar dos veces el mismo texto: la segunda vez antiLoopCheck debe
  // bloquear (content dedup) dentro de la ventana -> rejected.
  await handleRoute(UNKNOWN_FROM, fromPk, route, 'gw-3').catch(() => {});
  // Admitir otro pending para el segundo intento con el MISMO contenido.
  _setBridgeStateForTest(freshLedger());
  assert.ok(markDelivery('gw-4', 'pending'), 'admitido pending 2');
  await handleRoute(UNKNOWN_FROM, fromPk, route, 'gw-4').catch(() => {});
  assert.strictEqual(deliveryStatus('gw-4'), null,
    'pending eliminado tras anti-loop (rejected)');
});

// 4) CONTROL: un ruteo legítimo (con permiso) NO debe dejar pending ni borrar
//    nada de forma incorrecta — al finalizar el publishDM falla por red pero el
//    pending se conserva (retry legítimo) o se marca. Aquí solo verificamos que
//    el rejected no se dispara en un camino NO determinista de denial (no
//    interesa el publish real). Aseguramos que el fix no rompe el flujo normal:
//    con un permiso otorgado, handleRoute NO llama finishDelivery(rejected).
t('control: ruteo con permiso NO dispara rejected', async () => {
  _setBridgeStateForTest(freshLedger());
  const toName = Object.keys(bridge.CONFIG.agents || {})[0] || 'dave';
  // Asumimos que el primer agente permisivo puede escribir a si mismo no —
  // mejor: usamos un from que SÍ tenga permiso (si existe en routing.perms).
  const perms = (bridge.CONFIG.routing && bridge.CONFIG.routing.permissions) || {};
  const grantedFrom = Object.keys(perms).find(f =>
    (perms[f] || []).some(t => t === toName || t === '*'));
  if (!grantedFrom) {
    console.log('  (skip) no hay from con permiso en config para el control');
    return;
  }
  const fromPk = bridge.CONFIG.agents[grantedFrom];
  assert.ok(markDelivery('gw-5', 'pending'), 'admitido pending');
  const route = {to: toName, text: 'control legitimo'};
  // publishDM fallará por red (sin relay) -> se captura; lo importante: NO se
  // debe eliminar el pending por un rechazo determinista (sigue pending).
  await handleRoute(grantedFrom, fromPk, route, 'gw-5').catch(() => {});
  // Con permiso, el flujo llega al publishDM (falla por red) -> pending se
  // conserva (retry legítimo) o pasa a rejected solo si el handler lo decidió.
  const st = deliveryStatus('gw-5');
  // Aceptamos 'pending' (retry legítimo, publish fallido) — nunca rechazo
  // determinista por permiso.
  assert.ok(st === 'pending', 'con permiso el pending se conserva (retry legítimo)');
});

// cleanup
_setBridgeStateForTest(freshLedger());
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

_chain.then(() => {
  console.log('');
  console.log(`Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
}).catch((e) => { console.error('FATAL:', e && e.message); process.exit(1); });
