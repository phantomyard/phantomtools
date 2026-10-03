#!/usr/bin/env node
process.umask(0o077);
// Startup relay-reachability diagnostic: the bridge warns when its own relay is
// absent from the canonical list its personas resolve relays from. Diagnostic
// only — it must never change the relay set, throw, or block startup, and it
// must stay quiet whenever the list cannot be fetched.
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const M = require('./canonical-relays.js');

function okFetch(relays) {
  return async () => ({ok: true, json: async () => ({relays})});
}

function capture() {
  const lines = [];
  return {lines, warn: (m) => lines.push(String(m))};
}

(async () => {
  // 1. Default URL, and the per-deployment mirror phantombot may be pointed at.
  assert.strictEqual(M.canonicalRelaysUrl({}), M.DEFAULT_CANONICAL_RELAYS_URL);
  assert.strictEqual(
    M.canonicalRelaysUrl({PHANTOMCHAT_RELAYS_URL: '  https://mirror.example/relays.json '}),
    'https://mirror.example/relays.json');

  // 2. Acceptance rule: ws:// and wss:// survive; anything else is dropped.
  assert.deepStrictEqual(
    M.validRelays(['wss://a.example', 'ws://b.example', 'http://c.example', 42, null]),
    ['wss://a.example', 'ws://b.example']);

  // 3. Relay present in the list → no warning.
  const c1 = capture();
  const r1 = await M.warnIfRelayNotCanonical('ws://127.0.0.1:19999', {
    fetchImpl: okFetch(['wss://a.example', 'ws://127.0.0.1:19999']),
    warn: c1.warn,
  });
  assert.deepStrictEqual(r1, {checked: true, present: true});
  assert.strictEqual(c1.lines.length, 0, 'a reachable relay must not warn');

  // 4. Relay missing → warnings that name the relay, the silent mode and the fix.
  const c2 = capture();
  const r2 = await M.warnIfRelayNotCanonical('ws://127.0.0.1:19999', {
    fetchImpl: okFetch(['wss://a.example']),
    warn: c2.warn,
  });
  assert.deepStrictEqual(r2, {checked: true, present: false});
  assert.ok(c2.lines.length > 0, 'a missing relay must warn');
  const text = c2.lines.join('\n');
  assert.ok(text.includes('ws://127.0.0.1:19999'), 'warning names the configured relay');
  assert.ok(text.includes('silently'), 'warning names the silent failure mode');
  assert.ok(text.includes('PHANTOMCHAT_RELAYS_URL'), 'warning names the deployment knob');

  // 5. Any fetch failure → quiet, never throws, never guesses.
  const failures = [
    async () => { throw new Error('offline'); },
    async () => ({ok: false, json: async () => ({})}),
    async () => ({ok: true, json: async () => ({nope: true})}),
    async () => ({ok: true, json: async () => ({relays: ['http://x', 1]})}),
  ];
  for (const impl of failures) {
    const c = capture();
    const r = await M.warnIfRelayNotCanonical('ws://127.0.0.1:19999', {fetchImpl: impl, warn: c.warn});
    assert.deepStrictEqual(r, {checked: false}, 'an unverified list is not a check');
    assert.strictEqual(c.lines.length, 0, 'no warning without a verified list');
  }

  // 6. Wiring: bridge.js runs the diagnostic against its own relay at startup.
  const source = fs.readFileSync(path.join(__dirname, 'bridge.js'), 'utf8');
  assert.ok(source.includes("require('./canonical-relays.js')"), 'bridge.js requires the diagnostic');
  assert.ok(source.includes('warnIfRelayNotCanonical(CONFIG.nostr.relay)'), 'bridge.js checks its own relay');

  // 7. The requirement is written down where the deployer looks.
  const readme = fs.readFileSync(path.join(__dirname, 'README.md'), 'utf8');
  assert.ok(readme.includes('PHANTOMCHAT_RELAYS_URL'), 'README documents the relay-source override');

  console.log('OK test-canonical-relays.js (relay reachability diagnostic: 7 checks)');
})().catch((e) => {
  console.error('FAIL', e && e.message ? e.message : e);
  process.exit(1);
});
