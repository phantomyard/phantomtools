// AUDIT kaieriksen M05 (🔴 BLOQUEANTE): el download directo de recordings
// estaba SIN autenticar — bind a 127.0.0.1 no es barrera de auth en host
// compartido (cualquier proceso local que alcance el puerto leía cada MP4).
// Fix: exige requireAdmin en GET /recordings/:name antes de servir el archivo.
//
// Verifica que el código aplicó el gate en la ruta de descarga directa.
const assert = require('assert');
const fs = require('fs');
const src = fs.readFileSync('./bridge.js', 'utf8');
let passed = 0, failed = 0;
function t(n, fn){ try { fn(); console.log('  ok:', n); passed++; }
  catch(e){ console.error('  FAIL:', n, '-', e.message); failed++; } }

t('ruta /recordings/:name exige requireAdmin antes de servir el archivo', () => {
  const idx = src.indexOf("req.url.startsWith('/recordings/')");
  assert.ok(idx > 0, 'ruta /recordings/:name no encontrada');
  // Ventana desde el inicio de la ruta hasta el createReadStream (con margen).
  const createIdx = src.indexOf('createReadStream', idx);
  assert.ok(createIdx > idx, 'createReadStream no encontrado tras la ruta');
  const after = src.slice(idx, createIdx + 200);
  assert.ok(after.includes('requireAdmin'),
    'requireAdmin debe estar entre el inicio de la ruta y el createReadStream');
  // El gate debe preceder al createReadStream (denegar antes de abrir el fichero).
  const adminIdx = after.indexOf('requireAdmin');
  const streamIdx = after.indexOf('createReadStream');
  assert.ok(adminIdx >= 0 && adminIdx < streamIdx,
    'requireAdmin debe preceder al createReadStream');
});
t('anotación AUDIT M05 presente', () => {
  assert.ok(src.includes('AUDIT kaieriksen M05'), 'falta anotación M05');
});
t('el listado /recordings TAMBIÉN exige admin (cierra bypass de signed URLs)', () => {
  // AUDIT M05 BLOQUEANTE 1 (kaieriksen): el listado /recordings entregaba
  // las signed URLs (mintDownloadUrl -> bearer 24h) de forma PÚBLICA, con lo
  // que cualquier cliente saltaba el requireAdmin de /recordings/:name
  // descargando via /dl/... . Fail-closed: el listado también debe exigir el
  // admin token. El listado para agentes Nostr autenticados sigue cubierto
  // por el DM `recordings` (gate M01 agentCanOperateRoom).
  const listIdx = src.indexOf("req.url === '/recordings'");
  const dlIdx = src.indexOf("req.url.startsWith('/recordings/')");
  assert.ok(listIdx > 0 && dlIdx > listIdx,
    'el matcher del listado debe ir antes que el de descarga (orden del if/else)');
  // Entre el matcher del listado y el del download debe haber un requireAdmin
  // (el listado NO debe seguir público).
  const window_ = src.slice(listIdx, dlIdx);
  assert.ok(window_.includes('requireAdmin'),
    'el listado /recordings debe exigir requireAdmin (fail-closed, no signed URLs públicas)');
});
console.log(`\nAUDIT M05 (recordings auth) Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
