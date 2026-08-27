#!/usr/bin/env node
process.umask(0o077);
// Secret resolution regression (PR #24): config.json carries REFERENCES
// (vault:NAME / env:VAR), never plaintext values or tool-owned files.
// A plaintext inline value, a legacy *File key, or an unresolvable
// reference must all fail closed at require() time.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {execSync} = require('child_process');
const {generateSecretKey, nip19} = require('nostr-tools');

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pb-secrets-'));
const BRIDGE = path.join(__dirname, 'bridge.js');
const NSEC = nip19.nsecEncode(generateSecretKey());

function writeCfg(name, nostrSection, adminToken) {
  const cfg = {
    mode: 'nostr',
    nick: 't',
    httpPort: 0,
    httpAdminToken: adminToken,
    nostr: nostrSection,
    agents: {a: 'pk1'},
    routing: {permissions: {}, default: 'deny'},
  };
  const cfgPath = path.join(tmpDir, name + '.json');
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
  fs.chmodSync(cfgPath, 0o600);
  return cfgPath;
}

// Require bridge.js in a subprocess pointing at cfgPath. Returns {code, stderr}.
function probe(cfgPath, extraEnv) {
  const script = `process.env.PHANTOMBRIDGE_CONFIG=${JSON.stringify(cfgPath)};require(${JSON.stringify(BRIDGE)});`;
  const env = Object.assign({}, process.env, extraEnv || {});
  try {
    execSync('node -e ' + JSON.stringify(script), {cwd: __dirname, env, stdio: ['ignore', 'ignore', 'pipe']});
    return {code: 0, stderr: ''};
  } catch (e) {
    return {code: e.status || 1, stderr: String(e.stderr || '')};
  }
}

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

t('env: reference resolves (bridge loads)', () => {
  const cfg = writeCfg('env-ok', {relay: 'ws://127.0.0.1:19996', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'}, 'env:PHANTOMBRIDGE_TEST_ADMIN_TOKEN');
  const r = probe(cfg, {PHANTOMBRIDGE_TEST_NSEC: NSEC, PHANTOMBRIDGE_TEST_ADMIN_TOKEN: 'test-admin-token-123456'});
  assert.strictEqual(r.code, 0, r.stderr);
});

t('env: reference with unset variable fails closed', () => {
  const cfg = writeCfg('env-missing', {relay: 'ws://127.0.0.1:19996', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'}, 'env:PHANTOMBRIDGE_TEST_ADMIN_TOKEN');
  const r = probe(cfg, {PHANTOMBRIDGE_TEST_ADMIN_TOKEN: 'test-admin-token-123456'});
  assert.notStrictEqual(r.code, 0);
  assert.ok(r.stderr.includes('is not set'), r.stderr);
});

t('plaintext inline nsec is rejected', () => {
  const cfg = writeCfg('plaintext', {relay: 'ws://127.0.0.1:19996', nsec: NSEC}, 'env:PHANTOMBRIDGE_TEST_ADMIN_TOKEN');
  const r = probe(cfg, {PHANTOMBRIDGE_TEST_ADMIN_TOKEN: 'test-admin-token-123456'});
  assert.notStrictEqual(r.code, 0);
  assert.ok(r.stderr.includes('plaintext secret not allowed'), r.stderr);
});

t('legacy nsecFile key is rejected', () => {
  const cfg = writeCfg('legacy-file', {relay: 'ws://127.0.0.1:19996', nsecFile: './secrets/bridge.nsec'}, 'env:PHANTOMBRIDGE_TEST_ADMIN_TOKEN');
  const r = probe(cfg, {PHANTOMBRIDGE_TEST_ADMIN_TOKEN: 'test-admin-token-123456'});
  assert.notStrictEqual(r.code, 0);
  assert.ok(r.stderr.includes('no longer supported'), r.stderr);
});

t('unresolvable vault: reference fails closed', () => {
  // phantombot is not installed in this environment, so `phantombot vault get`
  // cannot resolve; the bridge must refuse rather than fall back.
  const cfg = writeCfg('vault-missing', {relay: 'ws://127.0.0.1:19996', nsec: 'vault:phantombridge-test-nonexistent'}, 'env:PHANTOMBRIDGE_TEST_ADMIN_TOKEN');
  const r = probe(cfg, {PHANTOMBRIDGE_TEST_ADMIN_TOKEN: 'test-admin-token-123456'});
  assert.notStrictEqual(r.code, 0);
  assert.ok(r.stderr.includes('cannot resolve vault:'), r.stderr);
});

t('plaintext inline admin token is rejected', () => {
  const cfg = writeCfg('plaintext-admin', {relay: 'ws://127.0.0.1:19996', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'}, 'test-admin-token-123456');
  const r = probe(cfg, {PHANTOMBRIDGE_TEST_NSEC: NSEC});
  assert.notStrictEqual(r.code, 0);
  assert.ok(r.stderr.includes('plaintext secret not allowed'), r.stderr);
});

fs.rmSync(tmpDir, {recursive: true, force: true});
console.log(`\nSecrets regression: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
