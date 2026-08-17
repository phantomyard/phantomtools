#!/usr/bin/env node
// test-http-api.js — HTTP API tests of the bridge (Copilot findings 4/5/7)
// Covers: /status, /pause, /join, /leave, /promote, /register, /recordings,
// invalid JSON -> 400, large body -> 413, 404, and the real state of /join.
//
// Usage: node test-http-api.js
// Requires: config.json with nostr mode (no XMPP) — the HTTP server is exported
// from bridge.js and listens on an ephemeral port.

const {server, CONFIG, PAUSED, setPaused, JITSI_MODE, MODE} = require('./bridge.js');

let passed = 0, failed = 0;
const t = (name, fn) => {
  try { fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.error('  FAIL:', name, '—', e.message); }
};
const tAsync = async (name, fn) => {
  try { await fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.error('  FAIL:', name, '—', e.message); }
};

const PORT = 0; // ephemeral
let base = '';

function listen() {
  return new Promise((resolve) => {
    server.listen(PORT, '127.0.0.1', () => {
      base = 'http://127.0.0.1:' + server.address().port;
      resolve();
    });
  });
}
function close() { return new Promise((r) => server.close(r)); }

async function post(path, body, raw) {
  const res = await fetch(base + path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: raw !== undefined ? raw : JSON.stringify(body),
  });
  let json = null;
  try { json = await res.json(); } catch (e) {}
  return {status: res.status, json};
}
const get = async (path) => {
  const res = await fetch(base + path);
  let json = null;
  try { json = await res.json(); } catch (e) {}
  return {status: res.status, json};
};

(async () => {
  await listen();
  console.log('bridge.js loaded: MODE=' + MODE + ' JITSI_MODE=' + JITSI_MODE);

  // --- /status ---
  await tAsync('GET /status -> 200 with ok:true and antiloop telemetry', async () => {
    const {status, json} = await get('/status');
    if (status !== 200) throw new Error('status ' + status);
    if (!json.ok) throw new Error('ok false');
    if (!json.antiloop || !json.antiloop.config) throw new Error('sin antiloop');
    if (json.antiloop.config.marker !== '[env]') throw new Error('marker incorrecto: ' + json.antiloop.config.marker);
    if (!Number.isInteger(json.antiloop.routed)) throw new Error('routed no entero');
  });

  // --- /pause ---
  const pauseState = PAUSED.nostr;
  await tAsync('POST /pause nostr -> ok:true and reflected state', async () => {
    const {status, json} = await post('/pause', {side: 'nostr', paused: !pauseState});
    if (status !== 200 || !json.ok) throw new Error(JSON.stringify({status, json}));
    if (json.state.nostr !== !pauseState) throw new Error('estado no reflejado');
  });
  await tAsync('POST /pause with non-boolean paused -> 400', async () => {
    const {status, json} = await post('/pause', {side: 'nostr', paused: 'si'});
    if (status !== 400 || json.ok !== false) throw new Error(JSON.stringify({status, json}));
  });
  // restaurar
  setPaused('nostr', pauseState);

  // --- invalid JSON -> 400 (finding 5) ---
  await tAsync('POST /join with invalid JSON -> 400', async () => {
    const {status, json} = await post('/join', null, '{esto no es json');
    if (status !== 400 || json.ok !== false) throw new Error(JSON.stringify({status, json}));
  });
  await tAsync('POST /leave with invalid JSON -> 400', async () => {
    const {status, json} = await post('/leave', null, 'garbage');
    if (status !== 400 || json.ok !== false) throw new Error(JSON.stringify({status, json}));
  });
  await tAsync('POST /register with invalid JSON -> 400', async () => {
    const {status, json} = await post('/register', null, '');
    if (status !== 400 || json.ok !== false) throw new Error(JSON.stringify({status, json}));
  });

  // --- Cuerpo grande -> 413 (hallazgo 5) ---
  await tAsync('POST /join with body >64KB -> 413', async () => {
    const big = 'x'.repeat(70 * 1024);
    const {status, json} = await post('/join', null, big);
    if (status !== 413 || json.ok !== false) throw new Error(JSON.stringify({status, json}));
  });

  // --- /join in nostr mode: clear error (no jitsi rooms) ---
  if (!JITSI_MODE) {
    await tAsync('POST /join in nostr mode -> error "no Jitsi rooms"', async () => {
      const {status, json} = await post('/join', {room: 'sala'});
      if (status !== 200 || json.ok !== false) throw new Error(JSON.stringify({status, json}));
      if (!/no Jitsi rooms/.test(json.error)) throw new Error('unexpected error: ' + json.error);
    });
    await tAsync('POST /leave in nostr mode -> clear error', async () => {
      const {status, json} = await post('/leave', {room: 'sala'});
      if (status !== 200 || json.ok !== false) throw new Error(JSON.stringify({status, json}));
    });
  }

  // --- /register: room required ---
  await tAsync('POST /register without room -> 200 with ok:false (validation error)', async () => {
    const {status, json} = await post('/register', {agents: ['alma']});
    if (json.ok !== false || !json.error) throw new Error(JSON.stringify({status, json}));
  });

  // --- 404 ---
  await tAsync('GET /no-existe -> 404', async () => {
    const {status, json} = await get('/no-existe');
    if (status !== 404 || json.ok !== false) throw new Error(JSON.stringify({status, json}));
  });

  // --- /recordings ---
  await tAsync('GET /recordings -> 200 with list, or 500 with clear error if the dir does not exist', async () => {
    const {status, json} = await get('/recordings');
    if (status === 200) {
      if (!json.ok || !Array.isArray(json.recordings)) throw new Error(JSON.stringify(json));
    } else {
      if (status !== 500 || json.ok !== false || !json.error) throw new Error(JSON.stringify({status, json}));
    }
  });

  await close();
  console.log('\nResult: ' + passed + ' ok, ' + failed + ' fail');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('FATAL:', e); process.exit(1); });
