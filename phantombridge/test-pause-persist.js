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
// [portable] Relative path to the test itself (not an absolute path from one
// machine): this way the test runs idempotently in CI/another checkout.
const BRIDGE = path.join(__dirname, 'bridge.js');
const RELAY = 'wss://relay-pause-persist.test';
const NSEC = nip19.nsecEncode(generateSecretKey());
// Separate relay identity: the bridge in jitsi mode signs the room content
// with THIS key (never the main one) so the phantombot receiver can classify
// it as untrusted/relay_npubs. readSecret requires it in JITSI_MODE, so the
// jitsi probes must inject it.
const RELAY_NSEC = nip19.nsecEncode(generateSecretKey());
// Secrets are REFERENCES (env:VAR) — never plaintext values in config.json.
// The bridge resolves them at require() time; subprocess probes inherit these.
process.env.PHANTOMBRIDGE_TEST_NSEC = NSEC;
process.env.PHANTOMBRIDGE_TEST_RELAY_NSEC = RELAY_NSEC;
process.env.PHANTOMBRIDGE_TEST_XMPP_PASSWORD = 'x';
process.env.PHANTOMBRIDGE_TEST_ADMIN_TOKEN = 'test-admin-token-123456';

function writeCfg(stateFile, pauseFile, mode) {
  const cfg = {
    mode: mode || 'nostr', nick: 't', httpPort: 18099, httpAdminToken: 'env:PHANTOMBRIDGE_TEST_ADMIN_TOKEN',
    // Jitsi mode requires CONFIG.xmpp to initialize (LOW-10); provide a
    // minimal dummy value so the XMPP client require does not crash.
    xmpp: {service: 'xmpps://127.0.0.1:5223', domain: 'auth.test', username: 'bridge', password: 'env:PHANTOMBRIDGE_TEST_XMPP_PASSWORD', focus: 'focus.test'},
    nostr: {relay: RELAY, nsec: 'env:PHANTOMBRIDGE_TEST_NSEC', relayNsec: 'env:PHANTOMBRIDGE_TEST_RELAY_NSEC'},
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

console.log('KILL-SWITCH DURABILITY (any mode): the pause persists and survives restart in jitsi/nostr/both:');

// --- JITSI mode (the [ALTO] audit target) ---
t('JITSI: pause persisted in PAUSE_FILE survives restart (bridgeState is null)', () => {
  const pauseFile = path.join(tmpDir, 'jitsi-pause.json');
  const stateFile = path.join(tmpDir, 'jitsi-state.json');
  // No nostr state (bridgeState null in jitsi). Only PAUSE_FILE with jitsi paused.
  fs.writeFileSync(pauseFile, JSON.stringify({jitsi: true, nostr: false}, null, 2));
  fs.writeFileSync(stateFile, JSON.stringify({relay: RELAY, lastSeen: 500, seenIds: [],
    antiloop: null, pendingSince: null, dropped: [], droppedOverflow: false,
    recoveryWatermark: 400, delivery: {}}, null, 2));
  const cfg = writeCfg(stateFile, pauseFile, 'jitsi');
  const out = runProbe(cfg, 'jitsi-restore',
    "console.log('JITSI nostr='+b.isPaused('nostr')+' jitsi='+b.isPaused('jitsi'));");
  assert.ok(out.includes('jitsi=true'), 'jitsi pause must be restored in jitsi mode, got: ' + out.match(/JITSI.*/));
  assert.ok(out.includes('nostr=false'), 'nostr stays unpaused, got: ' + out.match(/JITSI.*/));
});

// --- NOSTR mode (regression: still works) ---
t('NOSTR: pause restored from PAUSE_FILE in nostr mode', () => {
  const pauseFile = path.join(tmpDir, 'nostr-pause.json');
  const stateFile = path.join(tmpDir, 'nostr-state.json');
  fs.writeFileSync(pauseFile, JSON.stringify({jitsi: false, nostr: true}, null, 2));
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'nostr-restore');
  assert.ok(out.includes('nostr=true'), 'nostr pause must be restored, got: ' + out.match(/PROBE.*/));
  assert.ok(out.includes('jitsi=false'), 'jitsi unpaused, got: ' + out.match(/PROBE.*/));
});

// --- Legacy migration: v1 wrote paused inside .bridge-state.json (without PAUSE_FILE) ---
t('MIGRATION: missing PAUSE_FILE reads the legacy paused from the .bridge-state.json', () => {
  const pauseFile = path.join(tmpDir, 'mig-pause.json'); // no existe
  const stateFile = path.join(tmpDir, 'mig-state.json');
  writeBridgeState(stateFile, {paused: {jitsi: true, nostr: false}});
  const cfg = writeCfg(stateFile, pauseFile, 'jitsi'); // jitsi mode: bridgeState null
  const out = runProbe(cfg, 'mig',
    "console.log('MIG nostr='+b.isPaused('nostr')+' jitsi='+b.isPaused('jitsi'));");
  assert.ok(out.includes('jitsi=true'), 'jitsi legacy must be migrated, got: ' + out.match(/MIG.*/));
  // After migrating, persistPause() must have written PAUSE_FILE.
  assert.ok(fs.existsSync(pauseFile), 'after migrating, PAUSE_FILE must be created');
  const written = JSON.parse(fs.readFileSync(pauseFile, 'utf8'));
  assert.strictEqual(written.jitsi, true, 'migrated PAUSE_FILE must have jitsi=true');
});

// --- Compatibility: without PAUSE_FILE nor legacy paused -> both unpaused ---
t('without PAUSE_FILE nor legacy paused -> both sides unpaused (default)', () => {
  const pauseFile = path.join(tmpDir, 'none-pause.json');
  const stateFile = path.join(tmpDir, 'none-state.json');
  writeBridgeState(stateFile); // sin campo paused
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'none');
  assert.ok(out.includes('nostr=false jitsi=false'), 'both unpaused, got: ' + out.match(/PROBE.*/));
});

// --- [BAJO] relay-mismatch: the legacy paused from the .bridge-state.json must NOT
// be applied if the persisted relay does not match CONFIG.nostr.relay.
// (v1 wrote paused inside the nostr state; when switching relay A->B with the
// same state file, A's pause must not leak to B.) ---
t('[BAJO] relay mismatch: the legacy paused is ignored if the relay changes', () => {
  const pauseFile = path.join(tmpDir, 'mismatch-pause.json'); // missing -> falls back to legacy
  const stateFile = path.join(tmpDir, 'mismatch-state.json');
  // State from ANOTHER relay (A) with legacy paused nostr=true, but the config
  // points to RELAY (B). It must not be applied.
  writeBridgeState(stateFile, {paused: {jitsi: false, nostr: true}});
  const st = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  st.relay = 'wss://relay-OTRO.test'; // relay A different from the config (RELAY = B)
  fs.writeFileSync(stateFile, JSON.stringify(st, null, 2));
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'mismatch');
  assert.ok(out.includes('nostr=false'),
    'the pause from another relay must NOT leak (nostr must stay false), got: ' + out.match(/PROBE.*/));
  assert.ok(out.includes('jitsi=false'), 'jitsi unpaused, got: ' + out.match(/PROBE.*/));
});

// --- [ALTO/MEDIO] Durability: persistPause() fsync on the descriptor that
// writes (writeSync) + fsync of the parent directory after the rename. Verifies
// that PAUSE_FILE is durable and has no leftover .tmp after a normal write. ---
t('durability: persistPause() leaves a correct PAUSE_FILE with no orphaned tmp', () => {
  const pauseFile = path.join(tmpDir, 'dur-pause.json');
  const stateFile = path.join(tmpDir, 'dur-state.json');
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'dur-write', `
    b.setPaused('jitsi', true);
    console.log('DUR file='+require('fs').existsSync(${JSON.stringify(pauseFile)})+' content='+require('fs').readFileSync(${JSON.stringify(pauseFile)},'utf8').replace(/\\s+/g,''));
  `);
  assert.ok(out.includes('file=true'), 'persistPause must create PAUSE_FILE, got: ' + out.match(/DUR.*/));
  assert.ok(out.includes('content={\"jitsi\":true,\"nostr\":false}'),
    'correct durable content, got: ' + out.match(/DUR.*/));
  // No leftovers of persistPause temporaries should remain (pattern .pause.json.tmp.<pid>.<time>)
  const leftovers = fs.readdirSync(tmpDir).filter(f => /dur-pause\.json\.tmp\./.test(f));
  assert.strictEqual(leftovers.length, 0, 'there must be no orphaned .tmp from persistPause, got: ' + leftovers.join(','));
});

// --- [MEDIO/ALTO] Persistence error NOT hidden: if the pause cannot be
// written, setPaused() throws -> the HTTP handler responds ok:false instead of
// lying with ok:true when the kill-switch only lives in RAM. ---
t('failed persistence: setPaused() throws if it cannot persist (does not hide the error)', () => {
  // pauseFile inside a non-existent directory -> the tmp openSync fails.
  const pauseFile = path.join(tmpDir, 'no-such-dir', 'pause.json');
  const stateFile = path.join(tmpDir, 'fail-state.json');
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'fail-persist', `
    let threw = null;
    try { b.setPaused('jitsi', true); } catch (e) { threw = e.message; }
    console.log('FAIL threw='+!!threw+' msg='+(threw||''));
  `);
  assert.ok(out.includes('threw=true'), 'setPaused must throw when persistence fails, got: ' + out.match(/FAIL.*/));
  assert.ok(out.includes('msg='), 'must carry an error message, got: ' + out.match(/FAIL.*/));
  // The RAM state did change (kill-switch operative), but the failure is reported.
  assert.ok(out.includes('msg=') , 'msg not empty');
});

// --- [7] Directory fsync fault injection (exact regression of 8153728). The
// previous case (non-existent directory) fails BEFORE reaching the dir fsync;
// this injects the fault in fs.fsyncSync ONLY for the directory FD (open 'r'),
// proving that persistPause() returns false and that setPaused() throws — the
// kill-switch does not claim durability that does not exist. ---
t('fsync(dir) injection: if the directory fsync fails, persistPause()=false and setPaused() throws', () => {
  const pauseFile = path.join(tmpDir, 'dirsync-pause.json');
  const stateFile = path.join(tmpDir, 'dirsync-state.json');
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  const out = runProbe(cfg, 'dirsync', `
    const fs = require('fs');
    const origFsync = fs.fsyncSync;
    // Fails ONLY when the fd is for a directory (opened with O_RDONLY and
    // is a dir). The data file fsync (fd 'w') must keep working.
    fs.fsyncSync = function(fd) {
      try {
        const st = fs.fstatSync(fd);
        if (st.isDirectory()) { throw new Error('dir-fsync-injected'); }
      } catch (e) {
        if (e.message === 'dir-fsync-injected') throw e;
        // fstat may fail on some FS; do not block here: keep following the original.
      }
      return origFsync(fd);
    };
    let threw = null;
    let ret = undefined;
    try { b.setPaused('nostr', true); } catch (e) { threw = e.message; }
    console.log('DIRSYNC threw='+!!threw+' msg='+(threw||'')+' ramNostr='+b.isPaused('nostr'));
  `);
  // persistPause must have returned false (broken dir sync) -> setPaused throws.
  assert.ok(out.includes('threw=true'), 'setPaused must throw if the directory fsync fails, got: ' + out.match(/DIRSYNC.*/));
  assert.ok(out.includes('msg=') && !out.includes('msg=undefined'), 'must carry an error message, got: ' + out.match(/DIRSYNC.*/));
  // Since the operation is jitsi=true-style (activating), RAM stays paused (fail-closed).
  assert.ok(out.includes('ramNostr=true'), 'the side stays paused in RAM (fail-closed), got: ' + out.match(/DIRSYNC.*/));
});

// --- [1 BLOQUEANTE] Fail-closed on a corrupt PAUSE_FILE: a file that EXISTS
// but is unreadable/broken JSON/invalid schema must ABORT startup (throw),
// NEVER start unpaused assuming CONFIG.paused default. ---
t('[BLOQUEANTE] corrupt PAUSE_FILE (invalid JSON) -> loadPause() throws, the bridge does NOT start unpaused', () => {
  const pauseFile = path.join(tmpDir, 'corrupt-pause.json');
  const stateFile = path.join(tmpDir, 'corrupt-state.json');
  fs.writeFileSync(pauseFile, '{esto no es json'); // existente pero corrupto
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  // The bridge require invokes loadPause() at startup; we expect it to blow up.
  let loadError = null;
  try {
    runProbe(cfg, 'corrupt', "console.log('CORRUPT no-debio-llegar');");
  } catch (e) { loadError = e; }
  assert.ok(loadError, 'loadPause() must throw at startup with a corrupt PAUSE_FILE');
  assert.ok(/PAUSE_FILE corrupt/.test(String(loadError.stderr || loadError.message)),
    'the error must be unambiguous, got: ' + String(loadError.stderr || loadError.message).split('\n')[0]);
});

// --- [1 BLOQUEANTE] Invalid schema but valid JSON -> equally fatal. ---
t('[BLOQUEANTE] PAUSE_FILE with invalid schema (missing jitsi/nostr boolean) -> throws', () => {
  const pauseFile = path.join(tmpDir, 'schema-pause.json');
  const stateFile = path.join(tmpDir, 'schema-state.json');
  fs.writeFileSync(pauseFile, JSON.stringify({jitsi: 'true', nostr: 0}, null, 2)); // tipos incorrectos
  writeBridgeState(stateFile);
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  let loadError = null;
  try { runProbe(cfg, 'schema', "console.log('SCHEMA no-debio-llegar');"); }
  catch (e) { loadError = e; }
  assert.ok(loadError, 'loadPause() must throw with an invalid schema');
  assert.ok(/invalid schema/.test(String(loadError.stderr || loadError.message)),
    'unambiguous schema error, got: ' + String(loadError.stderr || loadError.message).split('\n')[0]);
});

// --- [9] Limit documentation: the unit test verifies the SEQUENCE of
// durability primitives (open+writeSync+fsync(fd)+close+rename+fsync(dir))
// and fail-closed against injected faults, NOT an empirical guarantee against
// a real power cut (that would require hardware/kernel, not stable CI).
// The previous injection cases cover the failure points: open (ENOENT),
// directory fsync (this one), and durable migration (see [2]).

// --- [2 ALTO] Legacy migration MUST persist durably; otherwise startup is
// aborted (migration is not completed only in RAM and the pause is lost on
// restart). The happy MIGRATION case already exists above; here the failure of
// persistPause() during migration must throw. ---
t('[ALTO] legacy migration: if persistPause() fails during migration, loadPause() aborts', () => {
  // pauseFile in a non-existent directory -> persistPause() fails during migration.
  const pauseFile = path.join(tmpDir, 'no-such-dir', 'mig-pause.json');
  const stateFile = path.join(tmpDir, 'mig-fail-state.json');
  // state with VALID legacy paused (relay matches) that would trigger the migration.
  writeBridgeState(stateFile, {paused: {jitsi: true, nostr: false}});
  const cfg = writeCfg(stateFile, pauseFile, 'nostr');
  let loadError = null;
  try { runProbe(cfg, 'migfail', "console.log('MIGFAIL no-debio-llegar');"); }
  catch (e) { loadError = e; }
  assert.ok(loadError, 'the migration must abort if it cannot create the durable PAUSE_FILE');
  assert.ok(/failed to migrate/.test(String(loadError.stderr || loadError.message)),
    'unambiguous migration error, got: ' + String(loadError.stderr || loadError.message).split('\n')[0]);
});

// cleanup
try { fs.rmSync(tmpDir, {recursive: true, force: true}); } catch (_) {}
delete process.env.PHANTOMBRIDGE_CONFIG;

console.log('');
console.log(`Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
