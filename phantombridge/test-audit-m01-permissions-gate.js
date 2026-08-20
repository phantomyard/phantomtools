// AUDIT kaieriksen M01 (🔴 BLOQUEANTE del PR #24 phantomyard):
//   "the configured permissions are never enforced on the agent-controlled
//    Jitsi paths... roomAgents only limits recipients and does not authorize
//    the sender. Gate every room command and message by sender plus room
//    scope before accepting it."
//
// Confirmado en el código previo al fix: CONFIG.permissions NO se leía en
// ningún sitio; roomAgents solo filtraba DESTINATARIOS y routingPerms solo
// aplicaba a DM↔DM. Cualquier agente autenticado podía join/leave/inject/
// recordings en cualquier sala.
//
// Fix: helper `agentCanOperateRoom(sender, room)` (y su lógica pura
// `evalRoomPermission`) que resuelve contra
//   "permissions": { "full": [...], "restricted": { room: [agents] } }
// con fail-closed, y los gates se aplican en join/leave/inject/recordings.
// SIN bloque `permissions` configura el helper -> true (compat: comportamiento
// legacy previo, sin romper despliegues sin permisos).
//
// ESTE TEST PRUEBA LA MATRIZ REAL (crítica de la revisión §4): ejercita la
// lógica de decisión contra configs AISLADOS — incluidos los bugs fail-closed
// (permissions:{}, permissions mal formado) y verifica el orden de los gates
// reales y el avance del watermark (BLOQUEANTE 2) en el código.
const assert = require('assert');
const fs = require('fs');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

// --- Matriz de decisión pura (sin cargar el módulo, sin red) ---
// evalRoomPermission(permConfig, senderName, room):
//   undefined          -> legacy/open
//   {}                 -> fail-closed
//   {full:[]}          -> deny
//   {restricted:{}}    -> deny
//   {full:[a]}         -> a en cualquier sala + room-agnostic
//   {restricted:{m:[b]}}-> b solo en m; room-agnostic exige full
//   {full:'a'} (mal)   -> fail-closed

function evalP(permConfig, sender, room) {
  return bridgeModule.evalRoomPermission(permConfig, sender, room);
}
// Cargamos una vez el módulo (basta para la lógica pura exportada; el config
// base del repo se usa por compatibilidad con la carga, no para la matriz).
require('./testlib.js').setup();
const bridgeModule = require('./bridge.js');

t('legacy: sin bloque permissions -> open (compat despliegues existentes)', () => {
  assert.strictEqual(evalP(undefined, 'alice', 'mia'), true);
  assert.strictEqual(evalP(undefined, 'algún-agente', null), true);
});

t('ALTO FIX: permissions:{} (bloque vacío) -> fail-closed, NO legacy', () => {
  // El bug detectado en la revisión: `permissions: {}` se interpretaba como
  // ausencia -> legacy/open. Ahora un bloque presente (aunque vacío) deniega.
  assert.strictEqual(evalP({}, 'alice', 'mia'), false, '{} debe denegar a alice');
  assert.strictEqual(evalP({}, 'alice', null), false, '{} room-agnostic deniega');
});

t('full:[] -> deny (nadie tiene full)', () => {
  assert.strictEqual(evalP({ full: [] }, 'alice', 'mia'), false);
  assert.strictEqual(evalP({ full: [] }, 'bob', null), false);
});

t('restricted:{} -> deny (sin full ni rooms)', () => {
  assert.strictEqual(evalP({ restricted: {} }, 'alice', 'mia'), false);
});

t('full:[alice] -> alice opera cualquier sala; bob no', () => {
  assert.strictEqual(evalP({ full: ['alice'] }, 'alice', 'mia'), true);
  assert.strictEqual(evalP({ full: ['alice'] }, 'alice', null), true); // room-agnostic
  assert.strictEqual(evalP({ full: ['alice'] }, 'bob', 'mia'), false);
});

t('restricted:{mia:[bob]} -> bob en mia; fuera de mia sin full denegado', () => {
  assert.strictEqual(evalP({ restricted: { mia: ['bob'] } }, 'bob', 'mia'), true);
  assert.strictEqual(evalP({ restricted: { mia: ['bob'] } }, 'bob', 'otra'), false);
  assert.strictEqual(evalP({ restricted: { mia: ['bob'] } }, 'alice', 'mia'), false);
  assert.strictEqual(evalP({ restricted: { mia: ['bob'] } }, 'bob', null), false); // room-agnostic exige full
});

t('ALTO FIX: full mal formado (string) -> fail-closed, no legacy', () => {
  // Un bloque permissions presente pero mal formado NO debe abrir el puente.
  assert.strictEqual(evalP({ full: 'alice' }, 'alice', 'mia'), false, 'full:string debe fail-closed');
});

t('sender vacío nunca tiene permiso (fail cerrado)', () => {
  assert.strictEqual(bridgeModule.evalRoomPermission(undefined, null, 'mia'), false);
  assert.strictEqual(bridgeModule.evalRoomPermission(undefined, '', 'mia'), false);
});

t('agentCanOperateRoom es una función exportada (gate real)', () => {
  assert.strictEqual(typeof bridgeModule.agentCanOperateRoom, 'function');
});

// --- Verificación de que los gates REALES están aplicados en el código ---
const src = fs.readFileSync('./bridge.js', 'utf8');
t('gates aplicados: recordings + [room] text + join/leave', () => {
  const uses = (src.match(/agentCanOperateRoom\(/g) || []).length;
  // grabaciones(1) + inyección(1) + handleJoinLeave(1) = 3 llamadas
  assert.ok(uses >= 3, 'se esperaban >=3 llamadas al gate, hay ' + uses);
});

t('BLOQUEANTE 2 FIX: processWatermark NO corre antes del gate M01', () => {
  // El watermark de recuperación debe avanzar SOLO por reloj local del bridge
  // (progreso real confirmado del stream), nunca con `created_at` del emisor, y
  // nunca en la admisión (antes del gate). Verificamos que la admisión ya NO
  // llama a processWatermark y que finishDelivery usa advanceRecoveryWatermark().
  const admissionBlock = src.slice(
    src.indexOf('const admitted = markDelivery'),
    src.indexOf('const content = unwrapped.content'));
  assert.ok(!admissionBlock.includes('processWatermark(wrapTs)'),
    'processWatermark(wrapTs) NO debe ejecutarse en la admisión (adelanta cursor con created_at hostil)');
  const fin = src.indexOf('function finishDelivery(id, ok, rejected)');
  // Encontrar el cierre REAL de la función (balanceando llaves), no el primer
  // '\n}' (que cortaría en el cierre del if (rejected) interno).
  let depth = 0, finEnd = fin;
  for (; finEnd < src.length; finEnd++) {
    if (src[finEnd] === '{') depth++;
    else if (src[finEnd] === '}') { depth--; if (depth === 0) break; }
  }
  const finBlock = src.slice(fin, finEnd + 1);
  // OPCION2: el avance del watermark usa advanceRecoveryWatermark() (reloj
  // local del bridge), no processWatermark(wrapTs) (created_at del emisor).
  // Buscamos la LLAMADA real (con ';' — el comentario explicativo nombra la
  // funcion sin llamarla y no debe contar como código).
  assert.ok(finBlock.includes('advanceRecoveryWatermark();'),
    'finishDelivery debe avanzar el watermark por reloj local (OPCION2)');
  const delivPos = finBlock.indexOf("markDelivery(id, 'delivered')");
  const wmPos = finBlock.indexOf('advanceRecoveryWatermark();');
  assert.ok(wmPos > delivPos,
    'el avance del watermark debe ir DESPUÉS de marcar delivered (solo tras éxito)');
});

t('BLOQUEANTE 2 FIX: el cursor no se alimenta con timestamps externos (proceso eliminado)', () => {
  // AUDIT-M01-OPCION2-FIX: processWatermark(ts) fue ELIMINADO por completo.
  // Alimentar el watermark con un timestamp del wire (created_at del emisor)
  // reintroduce la superficie de ataque. El único avance legítimo es
  // advanceRecoveryWatermark() con paso acotado (RECOVERY_WATERMARK_STEP_SECS),
  // que NUNCA salta a Date.now() tras un downtime.
  assert.ok(!/function processWatermark\(/.test(src),
    'processWatermark(ts) debe haber sido eliminado (no alimentar el cursor con ts del emisor)');
  assert.ok(src.includes('RECOVERY_WATERMARK_STEP_SECS'),
    'el avance debe estar ACOTADO por un paso (RECOVERY_WATERMARK_STEP_SECS), no saltar a now');
  assert.ok(src.includes('Math.min(prev + RECOVERY_WATERMARK_STEP_SECS, now)'),
    'el watermark avanza a min(prev+paso, now): nunca un salto libre a Date.now()');
});

t('BLOQUEANTE 1 FIX: GET /recordings exige admin (cierra signed URLs públicas)', () => {
  const listIdx = src.indexOf("req.url === '/recordings'");
  const dlIdx = src.indexOf("req.url.startsWith('/recordings/')");
  assert.ok(listIdx > 0 && dlIdx > listIdx, 'matcher del listado debe ir antes que el download');
  const window_ = src.slice(listIdx, dlIdx);
  assert.ok(window_.includes('requireAdmin'),
    'el listado /recordings debe exigir requireAdmin (fail-closed, no signed URLs públicas)');
});

t('README documenta el gate y el fail-closed', () => {
  const readme = fs.readFileSync('./README.md', 'utf8');
  assert.ok(/permissions/i.test(readme), 'README debería documentar permissions');
});

console.log(`\nAUDIT M01 (permissions gate) Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
