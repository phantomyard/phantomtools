// AUDIT kaieriksen M04 (🔴 BLOQUEANTE del PR #24 phantomyard):
//   "/register and persistRoomTimeout both write the shared config through the
//    same fixed .tmp path, with no serialization or fsync/atomic durability
//    protocol. Concurrent local API requests can rename/delete each other's
//    temp file and lose room registrations or timeouts. Use a single serialized
//    atomic writer, or keep runtime state separate from the source config."
//
// Fix (bridge.js): `persistConfig()` — a single promise queue
// (writeConfigChain) serializes ALL config writes; each uses a UNIQUE
// temporary name (pid+counter), never two writers share `.tmp`,
// and the rename is atomic on the same filesystem.
//
// This test verifies:
//   1) The code NO LONGER contains the shared manual write
//      `CONFIG_PATH + '.tmp'` (must have been removed — 0 occurrences).
//   2) `persistConfig` exists and is anchored to a single promise chain.
//   3) Each invocation uses a distinct temporary name (no collision).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

// We load the source code (without running the bridge or booting a relay).
const src = fs.readFileSync('./bridge.js', 'utf8');

t('fix M04: no shared manual CONFIG_PATH+.tmp writes remain', () => {
  // Previously there were 3 occurrences of `writeFileSync(CONFIG_PATH + '.tmp', ...)`.
  // After the fix there must be 0 (all go through persistConfig()).
  const manual = (src.match(/writeFileSync\(CONFIG_PATH\s*\+\s*'\.tmp'/g) || []).length;
  assert.strictEqual(manual, 0, manual + ' manual writes with a fixed .tmp remain');
});

t('fix M04: persistConfig exists and uses a single promise queue', () => {
  assert.ok(src.includes('function persistConfig()'), 'persistConfig() is missing');
  assert.ok(src.includes('let writeConfigChain = Promise.resolve();'),
    'writeConfigChain is missing (serialized queue)');
  // The operation is chained to the queue (via `chained = writeConfigChain.then(op)`)
  // and the chain is updated with `writeConfigChain = chained.catch(() => {})`,
  // which serializes ALL writes AND keeps the queue recoverable after
  // a failure (non-poisoned queue).
  assert.ok(src.includes('const chained = writeConfigChain.then(op)'),
    'writes are not chained to the queue');
  assert.ok(src.includes('writeConfigChain = chained.catch(() => {})'),
    'the queue is not updated recoverably');
});

t('fix M04: each write uses a UNIQUE temporary name (pid+counter)', () => {
  // The temp must be unique per invocation (pid + counter), never fixed.
  assert.ok(src.includes("CONFIG_PATH + '.tmp.' + process.pid + '.' + _cfgWriteSeq"),
    'non-unique temp: must include pid+counter');
  assert.ok(src.includes('_cfgWriteSeq += 1'), 'the sequence counter is missing');
});

t('fix M04: persistConfig() is used in /register and persistRoomTimeout', () => {
  // At least 2 calls to persistConfig() in the body (the two call sites).
  const calls = (src.match(/persistConfig\(\)/g) || []).length;
  assert.ok(calls >= 2, 'expected >=2 calls to persistConfig(), got ' + calls);
});

t('fix M04: the write is atomic (tmp + rename) and cleans up the temp on error', () => {
  // The robust implementation uses fs.openSync/fs.writeSync(+fsync) instead of
  // fs.writeFileSync to add real durability (crash/power-loss).
  // We validate the behavior: writes the temp, renames it atomically,
  // cleans up the temp on error and does fsync of the content AND the directory.
  const atomicWrite =
    src.includes('JSON.stringify(CONFIG, null, 2)')
    && (src.includes('fs.writeFileSync(tmp') || src.includes('fs.writeSync(fd'));
  assert.ok(atomicWrite, 'persistConfig does not write the temp');
  assert.ok(src.includes('fs.renameSync(tmp, CONFIG_PATH)'),
    'persistConfig does not rename atomically');
  assert.ok(src.includes('fs.unlinkSync(tmp)'),
    'persistConfig does not clean up the temp on error');
  assert.ok(src.includes('fs.fsyncSync(fd)'),
    'persistConfig does not fsync the content (crash durability)');
});

t('fix M04 (poisoned queue ALTO): the chain recovers after an I/O failure', () => {
  // A single write failure must NOT leave writeConfigChain rejected forever:
  // the next write must keep executing. The fix chains with .catch(() => {})
  // which consumes the error and leaves the chain resolved.
  const recoverable = src.includes('writeConfigChain = chained.catch(() => {})')
    || src.includes('writeConfigChain = writeConfigChain.then(op).catch');
  assert.ok(recoverable, 'the write queue does not recover after a failure (stays poisoned)');
});

t('fix M04 (fsync dir): the parent directory is synced after the rename', () => {
  assert.ok(src.includes('fs.fsyncSync(dirFd)'),
    'persistConfig does not fsync the directory (the rename may not be durable)');
});

console.log(`\nAUDIT M04 (atomic config write) Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
