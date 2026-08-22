// AUDIT kaieriksen M05 (🔴 BLOCKING): direct download of recordings
// was UNAUTHENTICATED — binding to 127.0.0.1 is not an auth barrier on a
// shared host (any local process that reaches the port read each MP4).
// Fix: require requireAdmin on GET /recordings/:name before serving the file.
//
// Verifies that the code applied the gate on the direct-download path.
const assert = require('assert');
const fs = require('fs');
const src = fs.readFileSync('./bridge.js', 'utf8');
let passed = 0, failed = 0;
function t(n, fn){ try { fn(); console.log('  ok:', n); passed++; }
  catch(e){ console.error('  FAIL:', n, '-', e.message); failed++; } }

t('route /recordings/:name requires requireAdmin before serving the file', () => {
  const idx = src.indexOf("req.url.startsWith('/recordings/')");
  assert.ok(idx > 0, 'route /recordings/:name not found');
  // Window from route start to the createReadStream (with margin).
  const createIdx = src.indexOf('createReadStream', idx);
  assert.ok(createIdx > idx, 'createReadStream not found after the route');
  const after = src.slice(idx, createIdx + 200);
  assert.ok(after.includes('requireAdmin'),
    'requireAdmin must be between the route start and the createReadStream');
  // The gate must precede the createReadStream (deny before opening the file).
  const adminIdx = after.indexOf('requireAdmin');
  const streamIdx = after.indexOf('createReadStream');
  assert.ok(adminIdx >= 0 && adminIdx < streamIdx,
    'requireAdmin must precede the createReadStream');
});
t('AUDIT M05 annotation present', () => {
  assert.ok(src.includes('AUDIT kaieriksen M05'), 'M05 annotation missing');
});
t('the /recordings listing ALSO requires admin (closes signed-URL bypass)', () => {
  // AUDIT M05 BLOCKING 1 (kaieriksen): the /recordings listing delivered
  // the signed URLs (mintDownloadUrl -> 24h bearer) in a PUBLIC way, so
  // any client bypassed the requireAdmin of /recordings/:name
  // by downloading via /dl/... . Fail-closed: the listing must also require the
  // admin token. Listing for authenticated Nostr agents remains covered
  // by the `recordings` DM (M01 gate agentCanOperateRoom).
  const listIdx = src.indexOf("req.url === '/recordings'");
  const dlIdx = src.indexOf("req.url.startsWith('/recordings/')");
  assert.ok(listIdx > 0 && dlIdx > listIdx,
    'the listing matcher must come before the download one (if/else order)');
  // Between the listing matcher and the download matcher there must be a requireAdmin
  // (the listing must NOT remain public).
  const window_ = src.slice(listIdx, dlIdx);
  assert.ok(window_.includes('requireAdmin'),
    'the /recordings listing must require requireAdmin (fail-closed, no public signed URLs)');
});
console.log(`\nAUDIT M05 (recordings auth) Result: ${passed} ok, ${failed} fail`);
process.exit(failed ? 1 : 0);
