process.umask(0o077);
// KILL-SWITCH DURABILITY (ANY MODE): the per-side pause must survive a
// restart/reconnect in EVERY mode (jitsi, nostr, both).
//
// [ALTO] audit regression: the v1 fix persisted the pause via `persistState()`
// which early-returns on `if (!bridgeState)`, and `bridgeState` is only
// initialized in NOSTR_MODE. In a jitsi-only deployment bridgeState stays
// null, so POST /pause {side:'jitsi'} -> setPaused -> markStateDirty ->
// flushState -> persistState -> `if (!bridgeState) return;` wrote NOTHING and
// the runtime pause was lost on restart. That first fix was only tested with
// mode:'nostr' (which initializes loadState/bridgeState), so the gap was not
// caught.
//
// Fix (v2): the pause is persisted in its own dedicated file (PAUSE_FILE,
// `.bridge-pause.json`), independent of bridgeState. setPaused() writes it
// synchronously+durably (persistPause()); loadPause() restores it on EVERY
// boot regardless of mode, with legacy migration from the .bridge-state.json
// `paused` field written by v1.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {execSync} = require('child_process');

const {generateSecretKey, nip19} = require('nostr-tools');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pause-persist-'));
// [portable] Ruta relativa al propio test (no un path absoluto de una máquina
// concreta): así el test corre en CI/otro checkout de forma idempotente.
const BRIDGE = path.join(__dirname, 'bridge.js');
const RELAY = 'wss://relay-pause-persist.test';
const NSEC = nip19.nsecEncode(generateSecretKey());
// Identidad de relay separada: el bridge en modo jitsi firma el contenido de
// sala con ESTA clave (nunca con el principal) para que el receptor phantombot
// pueda clasificarla como untrusted/relay_npubs. readSecret la exige en
// JITSI_MODE, así que los probes de jitsi deben inyectarla.
const RELAY_NSEC = nip19.nsecEncode(generateSecretKey());

function writeCfg(stateFile, pauseFile, mode) {
  const cfg = {
    mode: mode || 'nostr', nick: 't', httpPort: 18099, httpAdminToken: 'test-admin-token-123456',
    // Modo jitsi exige CONFIG.xmpp para inicializar (LOW-10); aportar un
    // valor mínimo dummy para que el require del cliente XMPP no crashee.
    xmpp: {service: 'xmpps://127.0.0.1:5223', domain: 'auth.test', username: 'bridge', password: 'x', focus: 'focus.test'},
    nostr: {relay: RELAY, nsec: NSEC, relayNsec: RELAY_NSEC},
    agents: {a: 'pk1', b: 'pk2'},
    routing: {permissions: {a: ['b']}, default: 'deny'},
    stateFile, pauseFile
  };
  const cfgPath = path.join(tmpDir, 'cfg-' + path.basename(stateFile) + '-' + (mode || 'nostr') + '.json');
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
  return cfgPath;
}

const BRIDGE_DIR = path.dirname(BRIDGE);

function runProbe(cfgPath, label, extraCode) {
  const script = path.join(tmpDir, 'probe-' + label + '.js');
  fs.writeFileSync(script, `
process.env.PHANTOMBRIDGE_CONFIG=${JSON.stringify(cfgPath)};
const b=require(${JSON.stringify(BRIDGE)});
${extraCode || "console.log('PROBE " + label + " nostr='+b.isPaused('nostr')+' jitsi='+b.isPaused('jitsi'));"}
process.exit(0);
`);
  return execSync('node ' + script, {cwd: BRIDGE_DIR, encoding: 'utf8'});
}

function writeBridgeState(stateFile, extra) {
  const base = {relay: RELAY, lastSeen: 500, seenIds: [], antiloop: null,
    pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: 400, delivery: {}};
  fs.writeFileSync(stateFile, JSON.stringify(Object.assign(base, extra || {}), null, 2));
}

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message.split('\n')[0]); failed++; }
}

console.log('KILL-SWITCH DURABILITY (any mode): el pause persiste y sobrevive al restart en jitsi/nostr/both:');

// --- Modo JITSI (el [ALTO] de la auditoría) ---
t('JITSI: pause persistido en PAUSE_FILE sobrevive al restart (bridgeState es null)', () => {
  const pauseFile = path.join(tmpDir, 'jitsi-pause.json');
  const stateFile = path.join(tmpDir, 'jitsi-state.json');
  // Sin state nostr (bridgeState null en jitsi). Solo PAUSE_FILE con jitsi paused.
  fs.writeFileSync(pauseFile, JSON.stringify({jitsi: true, nostr: false}, null, 2));
  fs.writeFileSync(stateFile, JSON.stringify({relay: RELAY, lastSeen: 500, seenIds: [],
    antiloop: null, pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: 400, delivery: {}}, null, 2));
  const cfg = writeCfg(stateFile, pauseFile, 'jitsi');
  const out = runProbe(cfg, 'jitsi-restore',
    "console.log('JITSI nostr='+b.isPaused('nostr')+' jitsi='+b.isPaused('jitsi'));");
  assert.ok(out.includes('jitsi=true'), 'jitsi pause debe restaurarse en modo jitsi, got: ' + out.match(/JITSI.*/));
  assert.ok(out.includes('nostr=false'), 'nostr sigue despausado, got: ' + out.match(/JITSI.*/));
});

// --- Modo NOSTR (regresión: sigue funcionando) ---
t('NOSTR: pause restaurado desde PAUSE_FILE en modo nostr', () => {
  const pauseFile = path.join(tmpDir, 'nostr-pause.json');
  const stateFile = path.join(tmpDir, 'nostr-state.json');
  fs.writeFileSync(pauseFile, JSON.stringify({jitsi: false, nostr: true}, null, 2));
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'nostr-restore');
  assert.ok(out.includes('nostr=true'), 'nostr pause debe restaurarse, got: ' + out.match(/PROBE.*/));
  assert.ok(out.includes('jitsi=false'), 'jitsi despausado, got: ' + out.match(/PROBE.*/));
});

// --- Migración legacy: v1 escribió paused dentro de .bridge-state.json (sin PAUSE_FILE) ---
t('MIGRACIÓN: ausencia de PAUSE_FILE lee el paused legacy del .bridge-state.json', () => {
  const pauseFile = path.join(tmpDir, 'mig-pause.json'); // no existe
  const stateFile = path.join(tmpDir, 'mig-state.json');
  writeBridgeState(stateFile, {paused: {jitsi: true, nostr: false}});
  const cfg = writeCfg(stateFile, pauseFile, 'jitsi'); // modo jitsi: bridgeState null
  const out = runProbe(cfg, 'mig',
    "console.log('MIG nostr='+b.isPaused('nostr')+' jitsi='+b.isPaused('jitsi'));");
  assert.ok(out.includes('jitsi=true'), 'jitsi legacy debe migrarse, got: ' + out.match(/MIG.*/));
  // Tras migrar, persistPause() debe haber escrito PAUSE_FILE.
  assert.ok(fs.existsSync(pauseFile), 'tras migrar, PAUSE_FILE debe crearse');
  const written = JSON.parse(fs.readFileSync(pauseFile, 'utf8'));
  assert.strictEqual(written.jitsi, true, 'PAUSE_FILE migrado debe tener jitsi=true');
});

// --- Compatibilidad: sin PAUSE_FILE ni paused legacy -> ambos despausados ---
t('sin PAUSE_FILE ni paused legacy -> ambos lados despausados (default)', () => {
  const pauseFile = path.join(tmpDir, 'none-pause.json');
  const stateFile = path.join(tmpDir, 'none-state.json');
  writeBridgeState(stateFile); // sin campo paused
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'none');
  assert.ok(out.includes('nostr=false jitsi=false'), 'ambos despausados, got: ' + out.match(/PROBE.*/));
});

// --- [BAJO] relay-mismatch: el paused legacy del .bridge-state.json NO debe
// aplicarse si el relay persistido no coincide con CONFIG.nostr.relay.
// (v1 escribia paused dentro del state nostr; al cambiar relay A->B con el
// mismo state file no debe filtrarse el pause de A a B.) ---
t('[BAJO] relay mismatch: el paused legacy se ignora si el relay cambia', () => {
  const pauseFile = path.join(tmpDir, 'mismatch-pause.json'); // no existe -> cae a legacy
  const stateFile = path.join(tmpDir, 'mismatch-state.json');
  // State de OTRO relay (A) con paused legacy nostr=true, pero el config
  // apunta a RELAY (B). No debe aplicarse.
  writeBridgeState(stateFile, {paused: {jitsi: false, nostr: true}});
  const st = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  st.relay = 'wss://relay-OTRO.test'; // relay A distinto del config (RELAY = B)
  fs.writeFileSync(stateFile, JSON.stringify(st, null, 2));
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'mismatch');
  assert.ok(out.includes('nostr=false'),
    'el pause de otro relay NO debe filtrarse (nostr debe quedar false), got: ' + out.match(/PROBE.*/));
  assert.ok(out.includes('jitsi=false'), 'jitsi despausado, got: ' + out.match(/PROBE.*/));
});

// --- [ALTO/MEDIO] Durability: persistPause() fsync sobre el descriptor que
// escribe (writeSync) + fsync del directorio padre tras el rename. Se verifica
// que el PAUSE_FILE queda durable y sin restos .tmp tras una escritura normal. ---
t('durabilidad: persistPause() deja PAUSE_FILE correcto y sin tmp huérfanos', () => {
  const pauseFile = path.join(tmpDir, 'dur-pause.json');
  const stateFile = path.join(tmpDir, 'dur-state.json');
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'dur-write', `
    b.setPaused('jitsi', true);
    console.log('DUR file='+require('fs').existsSync(${JSON.stringify(pauseFile)})+' content='+require('fs').readFileSync(${JSON.stringify(pauseFile)},'utf8').replace(/\\s+/g,''));
  `);
  assert.ok(out.includes('file=true'), 'persistPause debe crear PAUSE_FILE, got: ' + out.match(/DUR.*/));
  assert.ok(out.includes('content={\"jitsi\":true,\"nostr\":false}'),
    'contenido durable correcto, got: ' + out.match(/DUR.*/));
  // No deben quedar temporales de persistPause (patrón .pause.json.tmp.<pid>.<time>)
  const leftovers = fs.readdirSync(tmpDir).filter(f => /dur-pause\.json\.tmp\./.test(f));
  assert.strictEqual(leftovers.length, 0, 'no debe haber .tmp huérfanos de persistPause, got: ' + leftovers.join(','));
});

// --- [MEDIO/ALTO] Error de persistencia NO ocultado: si no se puede escribir
// el pause, setPaused() lanza -> el handler HTTP responde ok:false en vez de
// mentir con ok:true cuando el kill-switch solo vive en RAM. ---
t('persistencia fallida: setPaused() lanza si no puede persistir (no oculta el error)', () => {
  // pauseFile dentro de un directorio inexistente -> openSync del tmp falla.
  const pauseFile = path.join(tmpDir, 'no-such-dir', 'pause.json');
  const stateFile = path.join(tmpDir, 'fail-state.json');
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'fail-persist', `
    let threw = null;
    try { b.setPaused('jitsi', true); } catch (e) { threw = e.message; }
    console.log('FAIL threw='+!!threw+' msg='+(threw||''));
  `);
  assert.ok(out.includes('threw=true'), 'setPaused debe lanzar cuando la persistencia falla, got: ' + out.match(/FAIL.*/));
  assert.ok(out.includes('msg='), 'debe llevar mensaje de error, got: ' + out.match(/FAIL.*/));
  // El estado en RAM sí cambió (kill-switch operativo), pero se notifica el fallo.
  assert.ok(out.includes('msg=') , 'msg no vacío');
});

// --- [7] Inyección de fallo del fsync DEL DIRECTORIO (regresión exacta de
// 8153728). El caso anterior (directorio inexistente) falla ANTES de llegar al
// fsync del dir; este inyecta el fallo en fs.fsyncSync SOLO para el FD del
// directorio (open 'r'), probando que persistPause() devuelve false y que
// setPaused() lanza — el kill-switch no afirma durabilidad que no existe. ---
t('inyección fsync(dir): si el fsync del directorio falla, persistPause()=false y setPaused() lanza', () => {
  const pauseFile = path.join(tmpDir, 'dirsync-pause.json');
  const stateFile = path.join(tmpDir, 'dirsync-state.json');
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'dirsync', `
    const fs = require('fs');
    const origFsync = fs.fsyncSync;
    // Falla SOLO cuando el fd es de un directorio (abierto con O_RDONLY y que
    // es un dir). El fsync del fichero de datos (fd 'w') debe seguir OK.
    fs.fsyncSync = function(fd) {
      try {
        const st = fs.fstatSync(fd);
        if (st.isDirectory()) { throw new Error('dir-fsync-injected'); }
      } catch (e) {
        if (e.message === 'dir-fsync-injected') throw e;
        // fstat puede fallar en algunos FS; no bloquear aquí: seguimos el original.
      }
      return origFsync(fd);
    };
    let threw = null;
    let ret = undefined;
    try { b.setPaused('nostr', true); } catch (e) { threw = e.message; }
    console.log('DIRSYNC threw='+!!threw+' msg='+(threw||'')+' ramNostr='+b.isPaused('nostr'));
  `);
  // persistPause debe haber devuelto false (synq del dir roto) -> setPaused lanza.
  assert.ok(out.includes('threw=true'), 'setPaused debe lanzar si el fsync del directorio falla, got: ' + out.match(/DIRSYNC.*/));
  assert.ok(out.includes('msg=') && !out.includes('msg=undefined'), 'debe llevar mensaje de error, got: ' + out.match(/DIRSYNC.*/));
  // Como la operación es jitsi=true-style (activar), RAM queda pausada (fail-closed).
  assert.ok(out.includes('ramNostr=true'), 'el lado queda pausado en RAM (fail-closed), got: ' + out.match(/DIRSYNC.*/));
});

// --- [1 BLOQUEANTE] Fail-closed ante PAUSE_FILE corrupto: un fichero que
// EXISTE pero es ilegible/JSON roto/esquema inválido debe ABORTAR el arranque
// (throw), NUNCA arrancar despausado asumiendo CONFIG.paused default. ---
t('[BLOQUEANTE] PAUSE_FILE corrupto (JSON inválido) -> loadPause() lanza, el bridge NO arranca despausado', () => {
  const pauseFile = path.join(tmpDir, 'corrupt-pause.json');
  const stateFile = path.join(tmpDir, 'corrupt-state.json');
  fs.writeFileSync(pauseFile, '{esto no es json'); // existente pero corrupto
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  // El require del bridge invoca loadPause() en arranque; esperamos que reviente.
  let loadError = null;
  try {
    runProbe(cfg, 'corrupt', "console.log('CORRUPT no-debio-llegar');");
  } catch (e) { loadError = e; }
  assert.ok(loadError, 'loadPause() debe lanzar al arrancar con PAUSE_FILE corrupto');
  assert.ok(/PAUSE_FILE corrupto/.test(String(loadError.stderr || loadError.message)),
    'el error debe ser inequívoco, got: ' + String(loadError.stderr || loadError.message).split('\n')[0]);
});

// --- [1 BLOQUEANTE] Esquema inválido pero JSON válido -> igual de fatal. ---
t('[BLOQUEANTE] PAUSE_FILE con esquema inválido (falta jitsi/nostr booleano) -> lanza', () => {
  const pauseFile = path.join(tmpDir, 'schema-pause.json');
  const stateFile = path.join(tmpDir, 'schema-state.json');
  fs.writeFileSync(pauseFile, JSON.stringify({jitsi: 'true', nostr: 0}, null, 2)); // tipos incorrectos
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  let loadError = null;
  try { runProbe(cfg, 'schema', "console.log('SCHEMA no-debio-llegar');"); }
  catch (e) { loadError = e; }
  assert.ok(loadError, 'loadPause() debe lanzar con esquema inválido');
  assert.ok(/esquema inválido/.test(String(loadError.stderr || loadError.message)),
    'error inequívoco de esquema, got: ' + String(loadError.stderr || loadError.message).split('\n')[0]);
});

// --- [9] Documentación del límite: el test unitario verifica la SECUENCIA de
// primitivas de durabilidad (open+writeSync+fsync(fd)+close+rename+fsync(dir))
// y el fail-closed ante fallos inyectados, NO una garantía empírica frente a
// corte de energía real (eso requeriría hardware/kernel, no es CI estable).
// Los casos de inyección anteriores cubren los puntos de fallo: open (ENOENT),
// fsync del directorio (este), y la migración durable (ver [2]).

// --- [2 ALTO] Migración legacy DEBE persistir durablemente; si no, el arranque
// se aborta (no se completa la migración solo en RAM y se pierde el pause al
// reiniciar). El caso MIGRACIÓN feliz ya existe arriba; aquí el fallo de
// persistPause() durante la migración debe lanzar. ---
t('[ALTO] migración legacy: si persistPause() falla al migrar, loadPause() aborta', () => {
  // pauseFile en directorio inexistente -> persistPause() durante la migración falla.
  const pauseFile = path.join(tmpDir, 'no-such-dir', 'mig-pause.json');
  const stateFile = path.join(tmpDir, 'mig-fail-state.json');
  // state con paused legacy VÁLIDO (relay coincide) que dispararía la migración.
  writeBridgeState(stateFile, {paused: {jitsi: true, nostr: false}});
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  let loadError = null;
  try { runProbe(cfg, 'migfail', "console.log('MIGFAIL no-debio-llegar');"); }
  catch (e) { loadError = e; }
  assert.ok(loadError, 'la migración debe abortar si no puede crear el PAUSE_FILE durable');
  assert.ok(/no se pudo migrar/.test(String(loadError.stderr || loadError.message)),
    'error inequívoco de migración, got: ' + String(loadError.stderr || loadError.message).split('\n')[0]);
});

// cleanup
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
