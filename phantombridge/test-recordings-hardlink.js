// Regression test for #81: recordings hardlink escape.
// A hardlink inside RECORDINGS_DIR pointing at a file outside the dir is a
// regular file to lstat, so the handler must also refuse nlink > 1 (lstat
// alone only distinguishes symlinks).
'use strict';
process.umask(0o077);
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');

const TEST_NSEC = nip19.nsecEncode(generateSecretKey());
process.env.PHANTOMBRIDGE_TEST_NSEC = TEST_NSEC;

let passed = 0, failed = 0;
function count(name, ok, extra) {
  if (ok) { passed++; console.log('  ok:', name); }
  else { failed++; console.log('  FAIL:', name, '—', extra || ''); }
}

const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'pb-reg-hardlink-'));
const REC_DIR = path.join(ROOT, 'recordings');
const OUT_DIR = path.join(ROOT, 'outside');
fs.mkdirSync(REC_DIR, {recursive: true});
fs.mkdirSync(OUT_DIR, {recursive: true});

// A sensitive file OUTSIDE the recordings dir.
const SECRET_OUT = path.join(OUT_DIR, 'top-secret.txt');
fs.writeFileSync(SECRET_OUT, 'FLAG{sensitive-outside-data}');
fs.writeFileSync(path.join(REC_DIR, 'legit.mp4'), 'LEGIT-MP4-CONTENT');
// Hardlink inside the recordings dir pointing at the outside secret.
fs.linkSync(SECRET_OUT, path.join(REC_DIR, 'evil-hardlink.mp4'));

const DL_SECRET = path.join(ROOT, 'dl.secret');
fs.writeFileSync(DL_SECRET, 'dl-secret-token');
fs.chmodSync(DL_SECRET, 0o600);

const CFG_PATH = path.join(ROOT, 'config.json');
fs.writeFileSync(CFG_PATH, JSON.stringify({
  mode: 'nostr', nick: 'bridge-test', httpPort: 0,
  nostr: {relay: 'ws://127.0.0.1:19995', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'},
  agents: {alice: getPublicKey(generateSecretKey()), bob: getPublicKey(generateSecretKey())},
  routing: {permissions: {alice: ['bob'], bob: ['alice']}, default: 'deny'},
  recordingsDir: REC_DIR,
  downloadSecretFile: DL_SECRET,
  httpAdminToken: 'env:PHANTOMBRIDGE_TEST_NSEC',
}, null, 2));
fs.chmodSync(CFG_PATH, 0o600);
process.env.PHANTOMBRIDGE_CONFIG = CFG_PATH;
fs.rmSync(path.join(ROOT, '.bridge-state.json'), {force: true});

const bridge = require('./bridge.js');
const ADMIN = bridge.getAdminToken();

function request(method, url, token) {
  return new Promise((resolve, reject) => {
    const headers = {};
    if (token) headers['authorization'] = 'Bearer ' + token;
    const req = http.request({host: '127.0.0.1', port: PORT, path: url, method, headers}, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', c => body += c);
      res.on('end', () => resolve({status: res.statusCode, body}));
    });
    req.on('error', reject);
    req.end();
  });
}
let PORT;

(async () => {
  await new Promise(resolve => bridge.server.listen(0, '127.0.0.1', resolve));
  PORT = bridge.server.address().port;

  let r = await request('GET', '/recordings/evil-hardlink.mp4', ADMIN);
  count('hardlink to an outside file is refused (404)', r.status === 404,
    'got ' + r.status + ' ' + r.body.slice(0, 60));

  r = await request('GET', '/recordings/legit.mp4', ADMIN);
  count('legit recording still serves 200', r.status === 200 && r.body === 'LEGIT-MP4-CONTENT',
    'got ' + r.status);

  console.log(`\nRecordings hardlink regression: ${passed} passed, ${failed} failed`);
  await new Promise(resolve => bridge.server.close(resolve));
  fs.rmSync(ROOT, {recursive: true, force: true});
  process.exit(failed ? 1 : 0);
})().catch(e => {
  console.error('FATAL:', e);
  try { fs.rmSync(ROOT, {recursive: true, force: true}); } catch (_) {}
  process.exit(1);
});
