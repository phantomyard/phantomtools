#!/usr/bin/env node
// LOW-9 regression test (audit 462e62b): loadState() must DISTINGUISH the
// failure cases. The old bug: ENOENT (no state file = first run, full backlog
// is legitimate), JSON corruption and EACCES all fell into the same silent
// "full backlog", which could cause silent REPLAY (re-delivering DMs) when the
// state file existed but was damaged/unreadable.
//
// We spawn bridge.js as a child per scenario because loadState() aborts with
// process.exit(1) on corruption (fail-closed) — that cannot be caught with
// try/catch in this same process. The child requires the bridge module (the
// `require.main === module` server guard does NOT run, so only loadState +
// module init execute) and prints LOADED on success.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {execFileSync} = require('child_process');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '—', e.message); failed++; }
}

// Build a temp config snapshot like test-persist-audit-alto3, with an
// isolated stateFile, so STATE_FILE lands on the temp path.
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'low9-'));
const baseConfig = require('./testlib.js').baseConfig();
// loadState() solo corre en modo nostr/both; forzamos nostr para ejercitarlo.
baseConfig.mode = 'nostr';
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));

function runChild(stateFile, writeStateBytes) {
  // Point the temp config at the given state file path.
  const cfg = JSON.parse(fs.readFileSync(tmpConfigPath, 'utf8'));
  cfg.stateFile = stateFile;
  fs.writeFileSync(tmpConfigPath, JSON.stringify(cfg, null, 2));
  if (writeStateBytes !== null) fs.writeFileSync(stateFile, writeStateBytes);
  // El módulo bridge deja handles activos (server/timers) que impiden a node
  // salir solo, así que usamos timeout+killSignal para no colgar el test.
  // Para los casos fail-closed, process.exit(1) termina ANTES del timeout.
  try {
    const out = execFileSync(process.execPath, ['-e',
      "require('./bridge.js'); console.log('LOADED_OK');"],
      {cwd: __dirname, encoding: 'utf8', timeout: 4000, killSignal: 'SIGTERM', env: {...process.env, PHANTOMBRIDGE_CONFIG: tmpConfigPath}});
    return {code: 0, out};
  } catch (e) {
    // e.status: null si fue matado por el timeout (killSignal), o el exit code
    // real si el proceso salió solo (p.ej. process.exit(1) de fail-closed).
    const code = e.status === null ? 'timeout-kill' : e.status;
    return {code, out: (e.stdout || '') + (e.stderr || '')};
  }
}

// 1. ENOENT: no state file → legitimate full backlog, loadState does NOT abort.
t('ENOENT (sin archivo) -> carga normal, sin aborto', () => {
  const noState = path.join(tmpDir, 'missing.json');
  const r = runChild(noState, null);
  assert.ok(/LOADED_OK/.test(r.out), 'child no cargó: ' + r.out.slice(0, 200));
  assert.ok(/sin archivo de estado previo/.test(r.out), 'debe loguear init limpio: ' + r.out.slice(0, 200));
});

// 2. JSON corrupto → fail-closed: aborta (exit 1) con motivo logueado.
t('JSON corrupto -> ERROR FATAL + aborto (fail-closed, no backlog silencioso)', () => {
  const badState = path.join(tmpDir, 'corrupt.json');
  const r = runChild(badState, '{no es json valido');
  assert.ok(/ERROR FATAL loading state/.test(r.out), 'debe loguear ERROR FATAL: ' + r.out.slice(0, 300));
  assert.ok(/Estado corrupto/.test(r.out), 'debe mencionar corrupción: ' + r.out.slice(0, 300));
  assert.ok(!/LOADED_OK/.test(r.out), 'NO debe cargar tras estado corrupto (abortó antes): ' + r.out.slice(0, 200));
});

// 3. JSON válido pero forma inválida (no relay) -> también fail-closed (no
//    hay forma de saber qué se procesó). El objeto no tiene relay string.
t('estado JSON válido pero forma inválida -> aborto fail-closed', () => {
  const weirdState = path.join(tmpDir, 'weird.json');
  const r = runChild(weirdState, JSON.stringify({foo: 1, bar: 'x'}));
  assert.ok(/ERROR FATAL loading state/.test(r.out), ':: ' + r.out.slice(0, 300));
  assert.ok(!/LOADED_OK/.test(r.out), 'NO debe cargar: ' + r.out.slice(0, 200));
});

// 4. EACCES (permiso denegado) -> fail-closed también (no sabemos qué se
//    procesó). Simulamos con un path que apunta a un directorio ilegible como
//    archivo o a un archivo sin permiso de lectura.
t('EACCES (estado ilegible) -> aborto fail-closed', () => {
  const blockedState = path.join(tmpDir, 'no-read.json');
  fs.writeFileSync(blockedState, JSON.stringify({relay: 'n', lastSeen: 1}));
  fs.chmodSync(blockedState, 0o000); // sin permiso de lectura
  try {
    const r = runChild(blockedState, null);
    assert.ok(/ERROR FATAL loading state/.test(r.out), ':: ' + r.out.slice(0, 300));
    assert.ok(!/LOADED_OK/.test(r.out), 'NO debe cargar: ' + r.out.slice(0, 200));
  } finally {
    fs.chmodSync(blockedState, 0o600); // restaurar para poder limpiar
  }
});

// 5. Estado VÁLIDO y completo → carga normal (sin regresión del happy path),
//    y restaura el relay lastSeen.
t('estado válido y completo -> carga normal sin aborto', () => {
  const okState = path.join(tmpDir, 'ok.json');
  const r = runChild(okState, JSON.stringify({
    relay: 'ws://mirelay', lastSeen: Math.floor(Date.now() / 1000) - 10,
    seenIds: [{id: 'a', ts: Math.floor(Date.now() / 1000) - 5}], pendingSince: null, dropped: [],
  }));
  assert.ok(/LOADED_OK/.test(r.out), ':: ' + r.out.slice(0, 200));
});

fs.rmSync(tmpDir, {recursive: true, force: true});
console.log('\nResult: ' + passed + ' ok, ' + failed + ' fail');
process.exit(failed ? 1 : 0);
