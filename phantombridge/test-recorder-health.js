#!/usr/bin/env node
process.umask(0o077);
// Recorder readiness (v0): a Jitsi/Jibri recorder can be DEAD (systemd covers
// it) or ALIVE BUT DUMB (running yet unable to record) — then meetings still
// work but the Record button just fails. The bridge surfaces a READ-ONLY
// recorder verdict in GET /status (HEALTHY + IDLE = ready) so a persona can
// check it BEFORE convening. /status must never block on the probe: it returns
// the cached verdict the background monitor refreshed.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const {generateSecretKey, nip19} = require('nostr-tools');

let passed = 0, failed = 0;
async function t(name, fn) {
  try { await fn(); console.log('  ok:', name); passed++; }
  catch (e) { console.error('  FAIL:', name, '-', e.message); failed++; }
}

// Mutable fake recorder health endpoint.
let payload = '{"busyStatus":"IDLE","health":{"healthStatus":"HEALTHY"}}';
const health = http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(payload);
});

function request(port, pathName, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.get({host: '127.0.0.1', port, path: pathName, headers}, res => {
      let body = '';
      res.on('data', c => (body += c));
      res.on('end', () => resolve({status: res.statusCode, body}));
    });
    req.on('error', reject);
  });
}

(async () => {
  await new Promise(resolve => health.listen(0, '127.0.0.1', resolve));
  const healthUrl = 'http://127.0.0.1:' + health.address().port + '/jibri/api/v1.0/health';

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'phantombridge-recorder-'));
  const cfgPath = path.join(dir, 'config.json');
  process.env.PHANTOMBRIDGE_TEST_NSEC = nip19.nsecEncode(generateSecretKey());
  process.env.PHANTOMBRIDGE_TEST_ADMIN_TOKEN = 'test-admin-token-123456';
  process.env.PHANTOMBRIDGE_TEST_XMPP_PW = 'test-xmpp-password';
  const cfg = {
    mode: 'both',
    httpPort: 0,
    httpAdminToken: 'env:PHANTOMBRIDGE_TEST_ADMIN_TOKEN',
    recorder: {healthUrl, timeoutMs: 2000, refreshSecs: 15},
    nostr: {
      relay: 'ws://127.0.0.1:19996',
      nsec: 'env:PHANTOMBRIDGE_TEST_NSEC',
      relayNsec: 'env:PHANTOMBRIDGE_TEST_NSEC',
    },
    xmpp: {
      service: 'xmpps://127.0.0.1:5223',
      domain: 'auth.example.test',
      username: 'bridge',
      password: 'env:PHANTOMBRIDGE_TEST_XMPP_PW',
    },
    agents: {},
    permissions: {},
    routing: {permissions: {}, default: 'deny'},
  };
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
  fs.chmodSync(cfgPath, 0o600);
  process.env.PHANTOMBRIDGE_CONFIG = cfgPath;

  const bridge = require('./bridge.js');

  // 1. Pure classifier — the readiness rule on its own.
  await t('classify: HEALTHY + IDLE (nested under status) is ready', () => {
    const v = bridge.classifyRecorderHealth({status: {busyStatus: 'IDLE', health: {healthStatus: 'HEALTHY', details: {}}}});
    assert.strictEqual(v.ready, true);
  });
  await t('classify: HEALTHY + IDLE (top-level) is ready', () => {
    const v = bridge.classifyRecorderHealth({busyStatus: 'IDLE', health: {healthStatus: 'HEALTHY'}});
    assert.strictEqual(v.ready, true);
  });
  await t('classify: BUSY is not ready (a recording is in progress)', () => {
    const v = bridge.classifyRecorderHealth({status: {busyStatus: 'BUSY', health: {healthStatus: 'HEALTHY'}}});
    assert.strictEqual(v.ready, false);
    assert.match(v.reason, /busy/);
  });
  await t('classify: UNHEALTHY is not ready', () => {
    const v = bridge.classifyRecorderHealth({status: {busyStatus: 'IDLE', health: {healthStatus: 'UNHEALTHY'}}});
    assert.strictEqual(v.ready, false);
    assert.match(v.reason, /not healthy/);
  });
  await t('classify: a missing health key is not ready (fail-safe for the gate)', () => {
    assert.strictEqual(bridge.classifyRecorderHealth({status: {busyStatus: 'IDLE'}}).ready, false);
  });
  await t('classify: a non-object payload is not ready', () => {
    assert.strictEqual(bridge.classifyRecorderHealth('nope').ready, false);
    assert.strictEqual(bridge.classifyRecorderHealth(null).ready, false);
  });

  // 2. Integration — probe against the fake health server.
  await t('probe: HEALTHY + IDLE -> ready (nested)', async () => {
    payload = '{"status":{"busyStatus":"IDLE","health":{"healthStatus":"HEALTHY","details":{}}}}';
    const v = await bridge.probeRecorderHealth(healthUrl);
    assert.strictEqual(v.ready, true);
  });
  await t('probe: BUSY -> not ready', async () => {
    payload = '{"status":{"busyStatus":"BUSY","health":{"healthStatus":"HEALTHY"}}}';
    const v = await bridge.probeRecorderHealth(healthUrl);
    assert.strictEqual(v.ready, false);
    assert.match(v.reason, /busy/);
  });
  await t('probe: no answer -> not ready, never throws', async () => {
    const v = await bridge.probeRecorderHealth('http://127.0.0.1:1/jibri/api/v1.0/health', 500);
    assert.strictEqual(v.ready, false);
    assert.match(v.reason, /unreachable/);
  });
  await t('probe: non-JSON body -> not ready', async () => {
    payload = 'not json';
    const v = await bridge.probeRecorderHealth(healthUrl);
    assert.strictEqual(v.ready, false);
    assert.match(v.reason, /unrecognized/);
  });

  // 3. Cached verdict + GET /status integration.
  await t('refresh caches a ready verdict', async () => {
    payload = '{"status":{"busyStatus":"IDLE","health":{"healthStatus":"HEALTHY"}}}';
    const st = await bridge.refreshRecorderHealth();
    assert.strictEqual(st.ready, true);
    assert.ok(st.checkedAt, 'checkedAt must be set after a refresh');
  });

  await new Promise(resolve => bridge.server.listen(0, '127.0.0.1', resolve));
  const port = bridge.server.address().port;

  await t('/status requires the admin token', async () => {
    const r = await request(port, '/status');
    assert.strictEqual(r.status, 401);
  });
  await t('/status exposes recorder.ready from the cache', async () => {
    const r = await request(port, '/status', {authorization: 'Bearer ' + bridge.getAdminToken()});
    assert.strictEqual(r.status, 200);
    const body = JSON.parse(r.body);
    assert.strictEqual(body.recorder.ready, true);
    assert.strictEqual(typeof body.recorder.reason, 'string');
    assert.strictEqual(typeof body.recorder.ageSecs, 'number');
  });
  await t('/status never blocks on the probe (returns the cached verdict)', async () => {
    // The source turns BUSY, but no refresh has run: /status must answer from
    // the cache (still ready) instead of waiting for a fresh probe.
    payload = '{"status":{"busyStatus":"BUSY","health":{"healthStatus":"HEALTHY"}}}';
    const t0 = Date.now();
    const r = await request(port, '/status', {authorization: 'Bearer ' + bridge.getAdminToken()});
    const elapsed = Date.now() - t0;
    const body = JSON.parse(r.body);
    assert.strictEqual(body.recorder.ready, true);
    assert.ok(elapsed < 1000, 'status must not block on the recorder probe (' + elapsed + 'ms)');
  });
  await t('refresh picks up the new BUSY verdict', async () => {
    const st = await bridge.refreshRecorderHealth();
    assert.strictEqual(st.ready, false);
    assert.match(st.reason, /busy/);
    const r = await request(port, '/status', {authorization: 'Bearer ' + bridge.getAdminToken()});
    assert.strictEqual(JSON.parse(r.body).recorder.ready, false);
  });

  health.close();
  console.log(`\nRecorder readiness Result: ${passed} ok, ${failed} fail`);
  process.exit(failed ? 1 : 0);
})();
