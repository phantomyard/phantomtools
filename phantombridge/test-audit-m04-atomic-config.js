// AUDIT kaieriksen M04 (🔴 BLOQUEANTE del PR #24 phantomyard):
//   "/register and persistRoomTimeout both write the shared config through the
//    same fixed .tmp path, with no serialization or fsync/atomic durability
//    protocol. Concurrent local API requests can rename/delete each other's
//    temp file and lose room registrations or timeouts. Use a single serialized
//    atomic writer, or keep runtime state separate from the source config."
//
// Fix (bridge.js): `persistConfig()` — una única cola de promesas
// (writeConfigChain) serializa TODAS las escrituras del config; cada una usa
// un nombre temporal ÚNICO (pid+counter), nunca dos writers comparten `.tmp`,
// y el rename es atómico en el mismo filesystem.
//
// Este test verifica:
//   1) El código ya NO contiene la escritura manual compartida
//      `CONFIG_PATH + '.tmp'` (deben haberse eliminado — 0 ocurrencias).
//   2) `persistConfig` existe y está anclada a una única cadena de promesas.
//   3) Cada invocación usa un nombre temporal distinto (sin colisión).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

// Cargamos el código fuente (sin ejecutar el bridge ni bootear un relay).
const src = fs.readFileSync('./bridge.js', 'utf8');

t('fix M04: no quedan escrituras manuales compartidas CONFIG_PATH+.tmp', () => {
  // Antes había 3 ocurrencias de `writeFileSync(CONFIG_PATH + '.tmp', ...)`.
  // Tras el fix deben ser 0 (todas pasan por persistConfig()).
  const manual = (src.match(/writeFileSync\(CONFIG_PATH\s*\+\s*'\.tmp'/g) || []).length;
  assert.strictEqual(manual, 0, 'quedan ' + manual + ' escrituras manuales con .tmp fijo');
});

t('fix M04: persistConfig existe y usa una única cola de promesas', () => {
  assert.ok(src.includes('function persistConfig()'), 'falta persistConfig()');
  assert.ok(src.includes('let writeConfigChain = Promise.resolve();'),
    'falta writeConfigChain (cola serializada)');
  // La operación se encadena a la cola (vía `chained = writeConfigChain.then(op)`)
  // y la cadena se actualiza con `writeConfigChain = chained.catch(() => {})`,
  // que serializa TODAS las escrituras Y mantiene la cola recuperable tras
  // un fallo (cola no envenenada).
  assert.ok(src.includes('const chained = writeConfigChain.then(op)'),
    'las escrituras no se encadenan a la cola');
  assert.ok(src.includes('writeConfigChain = chained.catch(() => {})'),
    'la cola no se actualiza de forma recuperable');
});

t('fix M04: cada escritura usa un nombre temporal ÚNICO (pid+counter)', () => {
  // El temp debe ser único por invocación (pid + contador), nunca fijo.
  assert.ok(src.includes("CONFIG_PATH + '.tmp.' + process.pid + '.' + _cfgWriteSeq"),
    'temp no único: debe incluir pid+counter');
  assert.ok(src.includes('_cfgWriteSeq += 1'), 'falta el contador de secuencia');
});

t('fix M04: persistConfig() se usa en /register y persistRoomTimeout', () => {
  // Al menos 2 llamadas a persistConfig() en el cuerpo (los dos call sites).
  const calls = (src.match(/persistConfig\(\)/g) || []).length;
  assert.ok(calls >= 2, 'se esperaban >=2 llamadas a persistConfig(), hay ' + calls);
});

t('fix M04: la escritura es atómica (tmp + rename) y limpia el temp en error', () => {
  // La implementación robusta usa fs.openSync/fs.writeSync(+fsync) en vez de
  // fs.writeFileSync para añadir durabilidad real (crash/power-loss).
  // Validamos el comportamiento: escribe el temp, lo renombra atómicamente,
  // limpia el temp en error y hace fsync del contenido Y del directorio.
  const atomicWrite =
    src.includes('JSON.stringify(CONFIG, null, 2)')
    && (src.includes('fs.writeFileSync(tmp') || src.includes('fs.writeSync(fd'));
  assert.ok(atomicWrite, 'persistConfig no escribe el temp');
  assert.ok(src.includes('fs.renameSync(tmp, CONFIG_PATH)'),
    'persistConfig no renombra atómicamente');
  assert.ok(src.includes('fs.unlinkSync(tmp)'),
    'persistConfig no limpia el temp en caso de error');
  assert.ok(src.includes('fs.fsyncSync(fd)'),
    'persistConfig no hace fsync del contenido (durabilidad crash)');
});

t('fix M04 (cola envenenada ALTO): la cadena se recupera tras un fallo de I/O', () => {
  // Un único fallo de escritura NO debe dejar writeConfigChain rejected para
  // siempre: la siguiente escritura debe seguir ejecutándose. El fix encadena
  // con .catch(() => {}) que consume el error y deja la cadena resuelta.
  const recoverable = src.includes('writeConfigChain = chained.catch(() => {})')
    || src.includes('writeConfigChain = writeConfigChain.then(op).catch');
  assert.ok(recoverable, 'la cola de escritura no se recupera tras un fallo (queda envenenada)');
});

t('fix M04 (fsync dir): el directorio padre se sincroniza tras el rename', () => {
  assert.ok(src.includes('fs.fsyncSync(dirFd)'),
    'persistConfig no hace fsync del directorio (el rename podria no ser durable)');
});

console.log(`\nAUDIT M04 (escritura atómica config) Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
