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
// loadState() only runs in nostr/both mode; we force nostr to exercise it.
baseConfig.mode = 'nostr';
const tmpConfigPath = path.join(tmpDir, 'config.json');
fs.writeFileSync(tmpConfigPath, JSON.stringify(baseConfig, null, 2));

function runChild(stateFile, writeStateBytes) {
  // Point the temp config at the given state file path.
  const cfg = JSON.parse(fs.readFileSync(tmpConfigPath, 'utf8'));
  cfg.stateFile = stateFile;
  fs.writeFileSync(tmpConfigPath, JSON.stringify(cfg, null, 2));
  if (writeStateBytes !== null) fs.writeFileSync(stateFile, writeStateBytes);
  // The bridge module leaves active handles (server/timers) that keep node
  // from exiting on its own, so we use timeout+killSignal to avoid hanging.
  // For the fail-closed cases, process.exit(1) terminates BEFORE the timeout.
  try {
    const out = execFileSync(process.execPath, ['-e',
      "require('./bridge.js'); console.log('LOADED_OK');"],
      {cwd: __dirname, encoding: 'utf8', timeout: 4000, killSignal: 'SIGTERM', env: {...process.env, PHANTOMBRIDGE_CONFIG: tmpConfigPath}});
    return {code: 0, out};
  } catch (e) {
    // e.status: null if it was killed by the timeout (killSignal), or the real
    // exit code if the process exited on its own (e.g. process.exit(1) from
    // fail-closed).
    const code = e.status === null ? 'timeout-kill' : e.status;
    return {code, out: (e.stdout || '') + (e.stderr || '')};
  }
}

// 1. ENOENT: no state file → legitimate full backlog, loadState does NOT abort.
t('ENOENT (missing file) -> normal load, no abort', () => {
  const noState = path.join(tmpDir, 'missing.json');
  const r = runChild(noState, null);
  assert.ok(/LOADED_OK/.test(r.out), 'child did not load: ' + r.out.slice(0, 200));
  assert.ok(/no previous state file/.test(r.out), 'must log clean init: ' + r.out.slice(0, 200));
});

// 2. Corrupt JSON → fail-closed: aborts (exit 1) with a logged reason.
t('corrupt JSON -> FATAL ERROR + abort (fail-closed, no silent backlog)', () => {
  const badState = path.join(tmpDir, 'corrupt.json');
  const r = runChild(badState, '{no es json valido');
  assert.ok(/ERROR FATAL loading state/.test(r.out), 'must log ERROR FATAL: ' + r.out.slice(0, 300));
  assert.ok(/Corrupt state/.test(r.out), 'must mention corruption: ' + r.out.slice(0, 300));
  assert.ok(!/LOADED_OK/.test(r.out), 'must NOT load after (aborted before): ' + r.out.slice(0, 200));
});

// 3. Valid JSON but invalid shape (no relay) -> also fail-closed (no way to
//    know what was processed). The object has no relay string.
t('valid JSON but invalid shape -> fail-closed abort', () => {
  const weirdState = path.join(tmpDir, 'weird.json');
  const r = runChild(weirdState, JSON.stringify({foo: 1, bar: 'x'}));
  assert.ok(/ERROR FATAL loading state/.test(r.out), ':: ' + r.out.slice(0, 300));
  assert.ok(!/LOADED_OK/.test(r.out), 'must NOT load: ' + r.out.slice(0, 200));
});

// 4. EACCES (permission denied) -> fail-closed too (we do not know what was
//    processed). We simulate it with a path that points to an unreadable
//    directory as a file, or to a file with no read permission.
t('EACCES (unreadable state) -> fail-closed abort', () => {
  const blockedState = path.join(tmpDir, 'no-read.json');
  fs.writeFileSync(blockedState, JSON.stringify({relay: 'n', lastSeen: 1}));
  fs.chmodSync(blockedState, 0o000); // no read permission
  try {
    const r = runChild(blockedState, null);
    assert.ok(/ERROR FATAL loading state/.test(r.out), ':: ' + r.out.slice(0, 300));
    assert.ok(!/LOADED_OK/.test(r.out), 'must NOT load: ' + r.out.slice(0, 200));
  } finally {
    fs.chmodSync(blockedState, 0o600); // restore so we can clean up
  }
});

// 5. Valid and complete state → normal load (no happy-path regression), and it
//    restores the relay lastSeen.
t('valid and complete state -> normal load without abort', () => {
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
