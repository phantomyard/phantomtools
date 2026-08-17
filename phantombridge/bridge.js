#!/usr/bin/env node
// PhantomBridge — generic messaging bridge for the phantombot ecosystem.
//
// Modes (config.mode):
//   jitsi  — Jitsi (XMPP MUC) ↔ Nostr. Historical behavior: joins meet
//            rooms as "secretario" and mirrors the chat to the Nostr relay
//            via NIP-17 gift-wraps to authorized agents. Agents reply
//            through PhantomChat (DM to the bridge) and the bridge injects
//            the message into the room. (default — 100% compatible with v1.0.0)
//   nostr  — Nostr ↔ Nostr: DM↔DM routing between agents (inter-department
//            bot↔bot coordination). No Jitsi. Format: "@agent text".
//   both   — Both: Jitsi rooms + DM↔DM routing.
//
// The bridge is an OPTIONAL extra of the ecosystem: without it there is no
// communication between bots nor between bot and humans inside Jitsi rooms,
// but users who don't want it don't have to deploy it.
//
// Usage: node bridge.js [config.json]
// Local HTTP API: POST /join {room, nick?, password?, timeout?}, POST /leave {room},
//                 POST /promote {room, nick}, POST /register {room, agents, timeout?},
//                 POST /pause {side: jitsi|nostr|both, paused: bool},
//                 GET /status, GET /recordings, GET /recordings/:name

const fs = require('fs');
const path = require('path');
const http = require('http');
const crypto = require('crypto');
const {nip19, finalizeEvent, generateSecretKey, getPublicKey, getEventHash} = require('nostr-tools');
const nip44 = require('nostr-tools/nip44');
const {unwrapEvent} = require('nostr-tools/nip17');
const {makeAuthEvent} = require('nostr-tools/nip42');
const {loadOrgRouting} = require('./org-routing.js');

const CONFIG_PATH = process.argv[2] || (process.env.PHANTOMBRIDGE_CONFIG || './config.json');
const CONFIG = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));

const MODE = (CONFIG.mode || 'jitsi').toLowerCase();
const JITSI_MODE = MODE === 'jitsi' || MODE === 'both';
const NOSTR_MODE = MODE === 'nostr' || MODE === 'both';

// ---------------------------------------------------------------------------
// Per-side pause (kill-switch) — runtime, via HTTP POST /pause
// ---------------------------------------------------------------------------
// CONFIG.paused (opcional): estado inicial al arrancar.
//   { "jitsi": false, "nostr": false }
// Each side pauses INDEPENDENTLY: nostr paused = agent DMs are silently
// ignored (bots get no replies that would make them burn tokens); jitsi
// paused = rooms are left and room commands answer "paused". The other
// side keeps operating.
const PAUSED = {
  jitsi: !!(CONFIG.paused && CONFIG.paused.jitsi),
  nostr: !!(CONFIG.paused && CONFIG.paused.nostr),
};

function isPaused(side) {
  if (side === 'both') return PAUSED.jitsi || PAUSED.nostr;
  return !!PAUSED[side];
}

function setPaused(side, val) {
  const v = !!val;
  if (side === 'both') { PAUSED.jitsi = v; PAUSED.nostr = v; return {jitsi: PAUSED.jitsi, nostr: PAUSED.nostr}; }
  if (side !== 'jitsi' && side !== 'nostr') throw new Error('invalid side: ' + side + ' (jitsi|nostr|both)');
  PAUSED[side] = v;
  return {jitsi: PAUSED.jitsi, nostr: PAUSED.nostr};
}

// ---------------------------------------------------------------------------
// Nostr subscription state (anti-backlog on restarts)
// ---------------------------------------------------------------------------
// Stores the created_at of the last processed gift-wrap (kind 1059) so that
// on reconnect/restart the subscription starts with `since` instead of
// reprocessing the whole relay history (noisy backlog: commands that get
// re-executed, test REQUESTs that get re-routed, DMs that get re-answered).
// CONFIG.stateFile (opcional) customiza el path; default junto al config.
const STATE_FILE = CONFIG.stateFile || path.join(path.dirname(CONFIG_PATH), '.bridge-state.json');
const STATE_OVERLAP_SECS = 120; // margin to avoid losing boundary events
const STATE_FLUSH_MS = 5000;    // escritura debounced
const SEEN_IDS_MAX = 200;       // buffer de IDs de gift-wraps ya procesados
let bridgeState = null;         // {relay, lastSeen, seenIds[]} | null
let stateDirty = false;
let stateTimer = null;

function loadState() {
  try {
    const raw = fs.readFileSync(STATE_FILE, 'utf8');
    const s = JSON.parse(raw);
    if (s && typeof s.relay === 'string' && typeof s.lastSeen === 'number' && s.lastSeen > 0) {
      bridgeState = {relay: s.relay, lastSeen: s.lastSeen, seenIds: Array.isArray(s.seenIds) ? s.seenIds : []};
      console.log('[nostr] previous state:', bridgeState.relay, 'last event', new Date(bridgeState.lastSeen * 1000).toISOString(), '(' + bridgeState.seenIds.length + ' ids seen)');
    }
  } catch (e) { /* sin estado previo o corrupto -> backlog completo */ }
}

function markSeen(id) {
  if (!bridgeState || !id) return;
  if (!bridgeState.seenIds) bridgeState.seenIds = [];
  // Avoid duplicates in the buffer and trim to the maximum
  if (bridgeState.seenIds.includes(id)) return;
  bridgeState.seenIds.unshift(id);
  if (bridgeState.seenIds.length > SEEN_IDS_MAX) bridgeState.seenIds.length = SEEN_IDS_MAX;
  stateDirty = true;
  if (!stateTimer) {
    stateTimer = setTimeout(() => { stateTimer = null; flushState(); }, STATE_FLUSH_MS);
  }
}

function isSeen(id) {
  return !!bridgeState && !!id && !!(bridgeState.seenIds && bridgeState.seenIds.includes(id));
}

function flushState() {
  if (!stateDirty || !bridgeState) return;
  stateDirty = false;
  try {
    const tmp = STATE_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(bridgeState, null, 2) + '\n');
    fs.fsyncSync(fs.openSync(tmp, 'r+'));
    fs.renameSync(tmp, STATE_FILE);
  } catch (e) {
    console.error('[nostr] error writing state:', e.message);
  }
}

function updateLastSeen(ts) {
  if (!bridgeState) return;
  if (ts > bridgeState.lastSeen) {
    bridgeState.lastSeen = ts;
    stateDirty = true;
    if (!stateTimer) {
      stateTimer = setTimeout(() => { stateTimer = null; flushState(); }, STATE_FLUSH_MS);
    }
  }
}

// Flush al salir (SIGTERM/SIGINT/exit normal)
function flushStateOnExit() { flushState(); }
process.on('exit', flushStateOnExit);
process.on('SIGTERM', () => { flushState(); process.exit(0); });
process.on('SIGINT', () => { flushState(); process.exit(0); });

// Initialize state only in nostr/both mode (where the subscription exists)
if (NOSTR_MODE) {
  loadState();
  // If there was no previous state for this relay, initialize it so that
  // updateLastSeen()/markSeen() can persist from the very first event.
  if (!bridgeState || bridgeState.relay !== CONFIG.nostr.relay) {
    bridgeState = {relay: CONFIG.nostr.relay, lastSeen: 0, seenIds: []};
  }
}

const NICK = CONFIG.nick || 'secretario';
const ROOM_SUFFIX = CONFIG.roomSuffix || '@conference.meet.example.com';

// ---------------------------------------------------------------------------
// Recordings (jitsi mode only; shared by the HTTP API if applicable)
// ---------------------------------------------------------------------------
const RECORDINGS_DIR = CONFIG.recordingsDir || '/tmp/phantommeet-recordings';
const DL_BASE = CONFIG.downloadBase || 'https://meet.example.com';
const DL_SECRET_FILE = CONFIG.downloadSecretFile || '/opt/recordings-serve/.secret';
const DL_EXPIRY_HOURS = CONFIG.downloadExpiryHours || 24;
function readDlSecret() {
  try { return fs.readFileSync(DL_SECRET_FILE, 'utf8').trim(); } catch (e) { return null; }
}
function mintDownloadUrl(name) {
  const secret = readDlSecret();
  if (!secret) return null;
  const expiry = Math.floor(Date.now() / 1000) + Math.floor(DL_EXPIRY_HOURS * 3600);
  const payload = expiry + ':' + name;
  const sig = crypto.createHmac('sha256', secret).update(payload).digest('hex');
  const token = expiry + '.' + sig;
  return DL_BASE + '/dl/' + token + '/' + encodeURIComponent(name);
}
function listRecordings() {
  try {
    const files = fs.readdirSync(RECORDINGS_DIR).filter(f => f.endsWith('.mp4'));
    return files.map(f => {
      const st = fs.statSync(RECORDINGS_DIR + '/' + f);
      return {name: f, size: st.size, mtime: st.mtime.toISOString()};
    }).sort((a, b) => b.mtime.localeCompare(a.mtime));
  } catch (err) {
    return {error: err.message};
  }
}
function formatRecordingsList(recs) {
  if (Array.isArray(recs) && recs.length === 0) return 'No hay grabaciones en ' + RECORDINGS_DIR;
  if (Array.isArray(recs)) {
    const lines = recs.map(r => {
      const mb = (r.size / 1048576).toFixed(1) + ' MB';
      const date = r.mtime.slice(0, 19).replace('T', ' ');
      const url = mintDownloadUrl(r.name);
      return '- ' + r.name + ' (' + mb + ', ' + date + ')' + (url ? '\n  ' + url : '');
    });
    return 'Grabaciones (' + recs.length + '):\n' + lines.join('\n');
  }
  return 'Error leyendo grabaciones: ' + recs.error;
}

// ---------------------------------------------------------------------------
// org.yaml source of truth (norma v1.6) — hierarchy → routing
// ---------------------------------------------------------------------------
// If an org.yaml (PhantomOrg compiled) is available, the bridge derives
// its agents and DM↔DM routing from roles.reports_to + escalation_matrix
// (see org-routing.js). The manual config.json routing remains the fallback
// when no org.yaml is present. Config: CONFIG.orgFile (default: org.yaml
// next to the config file).
const ORG_FILE = CONFIG.orgFile || path.join(path.dirname(CONFIG_PATH), 'org.yaml');
const DERIVED = loadOrgRouting(ORG_FILE);
if (DERIVED) {
  const manualAgents = CONFIG.agents && Object.keys(CONFIG.agents).length;
  const manualRouting = CONFIG.routing && CONFIG.routing.permissions && Object.keys(CONFIG.routing.permissions).length;
  CONFIG.agents = Object.assign({}, DERIVED.agents, CONFIG.agents || {});
  if (manualAgents) {
    console.log('[bridge] agents del config.json complementan org.yaml (' + ORG_FILE + '); org.yaml manda.');
  }
  if (manualRouting) {
    console.warn('[bridge] WARNING: routing manual en config.json IGNORADO — org.yaml (' + ORG_FILE + ') es la fuente de verdad (norma v1.6).');
  }
  CONFIG.routing = DERIVED.routing;
}

// ---------------------------------------------------------------------------
// Estado compartido
// ---------------------------------------------------------------------------
const agentByName = new Map();    // nombre -> pubkey hex
const agentByPubkey = new Map();  // pubkey hex -> nombre
const lastRoomByAgent = new Map(); // nombre -> sala (respuestas sin sala)

for (const [name, pk] of Object.entries(CONFIG.agents || {})) {
  agentByName.set(name, pk);
  agentByPubkey.set(pk, name);
}

// ---------------------------------------------------------------------------
// DM↔DM routing (nostr/both mode) — inter-department coordination
// ---------------------------------------------------------------------------
// CONFIG.routing: {
//   "permissions": { "roberto": ["alma", "paco", "pepa"], "alma": ["roberto"], ... },
//   "default": "deny"  // what happens to a pair without an explicit rule
// }
// DM format: "@agent text" → forwards to that agent (prefixed "[from] text").
// The bridge presents its own identity (without bot:true) so that
// phantombot's bot-gate does not discard the DM at the receiver.
const routingPerms = CONFIG.routing && CONFIG.routing.permissions || {};
const routingDefault = (CONFIG.routing && CONFIG.routing.default) || 'deny';

function routingAllowed(from, to, perms, def) {
  if (from === to) return false;
  const rule = (perms || {})[from];
  if (!rule) return (def || 'deny') === 'allow';
  if (rule.includes('*')) return true;
  return rule.includes(to);
}

// Parses a coordination DM: returns {to, text} if it is "@agent text", or null.
function parseRouteTarget(content) {
  const m = content.match(/^@([A-Za-z0-9_.-]+)\s+([\s\S]+)$/);
  if (!m) return null;
  return {to: m[1].trim().toLowerCase(), text: m[2].trim()};
}

// ---------------------------------------------------------------------------
// Anti-loop (Phase 0+1): telemetry + mechanical enforcement of DM↔DM traffic.
// The bridge is the choke point of all bot↔bot traffic, so a loop can be
// cut here without touching the personas.
//
// Mechanisms (configurable via config.antiloop):
//   1. Protocol envelope (norma v1.3): if the message carries a
//      `[env] {json}` line, expires/hops/trace are validated (already-
//      traversed edge = oscillation). The bridge SEALS the envelope on
//      every forward (hops++, trace+from+to, expires if missing) — even if
//      the bots don't cooperate, the first forward already creates it.
//   2. Request_id short-circuit: same request_id (norma format
//      {org_id}-{yyyymmdd}-{seq4}) seen >N times in window -> drop + warn.
//      Additionally, EDGES (from->to) are tracked per rid: repeating an
//      already-traversed edge in the same thread = ping-pong -> drop
//      (works without envelope: the state belongs to the bridge).
//   3. Content-fingerprint dedup (F2-05): same
//      (from, to, content) in window -> drop. The fingerprint is robust
//      to reformatting (canonical: spaces/uppercase/punctuation/
//      accents) and near-identical paraphrase (unigrams + Jaccard >=
//      fuzzyThreshold), and request_ids are ignored (metadata, not
//      content) — closes the gap of the bot that strips the envelope and
//      re-publishes with new rid/text.
//   4. Pair rate: max N from->to messages per window -> drop.
//
// Persistence: in-memory (loops are short-lived; a bridge restart resets
// the state). The drop count is exposed in /status.
// ---------------------------------------------------------------------------
const ALO = CONFIG.antiloop || {};
// Normalization/validation of config.antiloop (Copilot finding 6): non-
// numeric values -> default; out of range -> clamp. Avoids silent
// degradation (e.g. maxHops:"abc" -> NaN -> never drops by hops).
function num(v, dflt, min, max) {
  const n = Number(v);
  if (!Number.isFinite(n)) return dflt;
  if (min !== undefined && n < min) return min;
  if (max !== undefined && n > max) return max;
  return n;
}
const ENVELOPE_MARKER = '[env]'; // protocol constant: also fixed in PhantomOrg (org.yaml envelope.marker)
const ANTILOOP = {
  // 1) Envelope de protocolo (norma v1.3)
  maxHops: num(ALO.maxHops, 3, 1, 10),        // coincide con communication.max_hops del org
  expireMs: num(ALO.expireMs, 6 * 3600000, 60000, 7 * 24 * 3600000),  // TTL del envelope si no trae expires

  // 2) Cortocircuito por request_id
  reqWindowMs: num(ALO.reqWindowMs, 600000, 1000, 24 * 3600000),
  reqMax: num(ALO.reqMax, 8, 1, 100),
  requestMax: num(ALO.requestMax, 500, 10, 10000),  // hard cap of entries (LRU by last) — F2-08
  requests: new Map(), // request_id -> {count, first, last, agents:Set, edges:Map<edge,count>, tripped:bool}

  // 3) Logical dedup — 1h window (F3-01): a slow loop repeats content
  //    every 15-30 min; with 10 min the repeat fell outside the window and
  //    the loop continued. hashMax 200 remains trivial for 5 bots.
  hashWindowMs: num(ALO.hashWindowMs, 3600000, 1000, 24 * 3600000),
  hashMax: num(ALO.hashMax, 200, 10, 10000),
  hashes: new Map(),   // hash(from|to|canonicalText) -> {id, ts, pair, canon, shingles} (F2-R04 + F2-05)
  evictedHashes: 0,    // eviction counter by hashMax (observable degradation) — F2-09
  fuzzyThreshold: num(ALO.fuzzyThreshold, 0.85, 0.5, 1),  // min Jaccard to consider two messages the same content (F2-05)

  // 4) Pair rate
  pairWindowMs: num(ALO.pairWindowMs, 60000, 1000, 3600000),
  pairMax: num(ALO.pairMax, 10, 1, 1000),
  pairs: new Map(),    // "from|to" -> [{id, ts}] (F2-R03: identity by id, not by ts)

  // 4b) HOURLY pair rate (F3-01): defends against SLOW loops that evade
  //     pairWindowMs (1 msg/15min = 4/h never exceeds 10/min). An hourly
  //     limit cuts the loop after 2.5h for 4/h; legitimate traffic between
  //     two bots is one-off requests (tens/day).
  pairHourWindowMs: num(ALO.pairHourWindowMs, 3600000, 60000, 24 * 3600000),
  pairHourMax: num(ALO.pairHourMax, 10, 1, 1000),
  pairHours: new Map(), // "from|to" -> [{id, ts}] (same monotonic identity)

  // Stats
  routed: 0,
  dropped: {hash: 0, fuzzy: 0, pair: 0, request: 0, cycle: 0, hops: 0, expired: 0},
  lastSweep: 0,

  // MONOTONIC admission identity (F2-R03/R04): Date.now() is not unique —
  // two admissions in the same ms would share the same ts and rollback
  // could not tell them apart. Each admission gets a unique incremental id.
  nextAdmissionId: 1,
};

// --- Envelope de protocolo (norma v1.3) ------------------------------------
// Formato: PRIMERA LÍNEA `[env] {json}` seguida del mensaje (F2-01: el
// bridge delivers the envelope as the real first line; the [from] goes after).
// The bridge seals/validates it; the personas keep it when replying (norma).
//
// F2-06: the JSON is parsed from the COMPLETE first line (not with a non-
// greedy regex over the object) — supports `}` inside JSON strings.
// F2-02: strict type/range validation. An invalid envelope is treated
// as nonexistent (null) and the message falls through to the remaining defenses.
function parseEnvelope(text) {
  const str = String(text);
  const nl = str.indexOf('\n');
  const firstLine = (nl === -1 ? str : str.slice(0, nl)).trim();
  const mm = firstLine.match(/^\[env\]\s+/);
  if (!mm) return null;
  const jsonPart = firstLine.slice(mm[0].length);
  let env;
  try {
    env = JSON.parse(jsonPart);
  } catch (e) {
    return null;
  }
  if (!env || typeof env !== 'object' || Array.isArray(env)) return null;
  // Strict validation (F2-02): no silent type coercion.
  if (!Number.isSafeInteger(env.hops) || env.hops < 0) return null;
  if (env.trace !== undefined && !Array.isArray(env.trace)) return null;
  if (env.trace && !env.trace.every(x => typeof x === 'string')) return null;
  if (env.expires !== undefined && (!Number.isSafeInteger(env.expires) || env.expires <= 0)) return null;
  if (env.rid !== undefined && typeof env.rid !== 'string') return null;
  if (env.trace === undefined) env.trace = []; // legitimate default (absence, not invalid type)
  const rest = (nl === -1 ? '' : str.slice(nl + 1)).trim();
  return {env, rest};
}

function renderEnvelope(env) {
  return ENVELOPE_MARKER + ' ' + JSON.stringify(env) + '\n';
}

// Extrae el request_id del texto (fallback best-effort para mensajes SIN
// envelope; el rid del envelope tiene prioridad en antiLoopCheck — F2-04).
// Misma regex que el cortocircuito. Solo para crear/rastrear, nunca para
// autorizar: un rid de texto libre nunca debe poder contaminar el contador
// of a legitimate rid (that is why, if there is an envelope, ONLY env.rid is used).
function extractRid(text) {
  const m = String(text).match(/([a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*-\d{8}-\d{4})/);
  return m ? m[1] : null;
}

// Seals the envelope in a message about to be forwarded A->B.
// - If the message already carries an envelope, it updates it (hops++, trace push).
// - If not, it creates one (rid from text if present, hops=1).
// - expires is set if missing (now + expireMs).
function stampEnvelope(text, from, to) {
  const parsed = parseEnvelope(text);
  const env = parsed ? parsed.env : {rid: extractRid(text) || undefined, hops: 0, trace: []};
  env.hops = (env.hops || 0) + 1;
  if (env.trace[env.trace.length - 1] !== from) env.trace.push(from);
  env.trace.push(to);
  if (!env.expires) env.expires = Date.now() + ANTILOOP.expireMs;
  const rest = parsed ? parsed.rest : String(text).trim();
  return renderEnvelope(env) + rest;
}

// Aristas (emisor->destino) recorridas por un envelope: pares consecutivos
// of the trace. If the new edge (from->to) is already there -> oscillation.
function traceHasEdge(trace, from, to) {
  for (let i = 0; i + 1 < trace.length; i++) {
    if (trace[i] === from && trace[i + 1] === to) return true;
  }
  return false;
}

// --- Huella de contenido (F2-05) --------------------------------------------
// The classic dedup (djb2 of exact text) dies against a non-cooperative bot
// that STRIPS the envelope and re-publishes with a new rid and slightly
// reformatted/paraphrased text: the hash changes even though the content is
// the same.
//
// Two levels (config.antiloop):
//   1. CANONICAL: NFKC -> lowercase -> no diacritics -> only [a-z0-9] +
//      spaces -> collapsed whitespace. Kills trivial reformatting
//      (spaces, uppercase, punctuation, accents, emoji).
//   2. FUZZY (unigrams + Jaccard): word set of the canonical text.
//      Two messages with overlap >= fuzzyThreshold are considered the
//      same content (near-identical paraphrase — the real pattern of an
//      LLM loop rewriting the message). Unigrams: robust to reordering
//      and light rewording in short messages (bigrams dropped to
//      Jaccard < 0.5 by just reordering two words). Conservative default
//      threshold (0.85): "confirma la reunion manana" vs "confirma la
//      reunion hoy" give 0.6 -> not dropped.
//
// IMPORTANT: the fingerprint is computed over the CONTENT (envelope rest if
// present) with request_ids REMOVED (they are protocol metadata, not
// content — a bot re-publishing with a new rid but the same body must fall
// into dedup). This is the piece that closes F2-05.

// Norma rid format: {org}-{yyyymmdd}-{seq4} (also fixed in
// extractRid). Removed from content BEFORE the fingerprint.
const RID_PATTERN = /[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*-\d{8}-\d{4}/g;

function stripRids(text) {
  return String(text).replace(RID_PATTERN, ' ');
}

function canonicalize(text) {
  return String(text)
    .normalize('NFKC')            // unifies forms (fullwidth, ligatures, compat)
    .toLowerCase()                // case-insensitive
    .normalize('NFD')             // splits diacritics
    .replace(/[\u0300-\u036f]/g, '')  // removes accents/ñ->n (fingerprint, not crypto)
    .replace(/[^a-z0-9\s]/g, ' ')     // non-alphanumeric -> space (punctuation, emoji)
    .replace(/\s+/g, ' ')         // collapses whitespace
    .trim();
}

// Word set (unigrams) of the canonical text. Empty if text is empty.
function shingleSet(text) {
  const words = text.split(' ').filter(Boolean);
  return new Set(words);
}

function jaccard(a, b) {
  if (!a.size && !b.size) return 1;   // both empty: identical
  if (!a.size || !b.size) return 0;   // one empty: nothing in common
  let inter = 0;
  for (const x of a) if (b.has(x)) inter++;
  return inter / (a.size + b.size - inter);
}

// Hash simple (djb2) de emisor|receptor|texto CANONICO — solo para dedup, no cripto.
function hashMsg(from, to, canonText) {
  let h = 5381;
  const s = from + '|' + to + '|' + canonText;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(36);
}

function sweepLoopState(now) {
  for (const [k, arr] of ANTILOOP.pairs) {
    const fresh = arr.filter(e => now - e.ts < ANTILOOP.pairWindowMs);
    if (fresh.length) ANTILOOP.pairs.set(k, fresh);
    else ANTILOOP.pairs.delete(k);
  }
  for (const [k, arr] of ANTILOOP.pairHours) {
    const fresh = arr.filter(e => now - e.ts < ANTILOOP.pairHourWindowMs);
    if (fresh.length) ANTILOOP.pairHours.set(k, fresh);
    else ANTILOOP.pairHours.delete(k);
  }
  for (const [k, e] of ANTILOOP.hashes) {
    if (now - e.ts > ANTILOOP.hashWindowMs) ANTILOOP.hashes.delete(k);
  }
  for (const [k, r] of ANTILOOP.requests) {
    if (now - r.last > ANTILOOP.reqWindowMs) ANTILOOP.requests.delete(k);
  }
}

// Resolves the rid to track for a message (used by check and rollback).
// If there is a valid envelope -> ONLY env.rid (authoritative, does not scan text).
// Without envelope -> best-effort textual fallback (F2-04).
function resolveRid(parsed, textStr) {
  if (parsed && parsed.env.rid) return parsed.env.rid;
  if (!parsed) return extractRid(textStr);
  return null;
}

// Returns {ok:true, admission} if the message passes, or {ok:false, reason, detail}
// if it must be dropped. The drop is SILENT (log only): replying to the sender
// would feed the loop back and burn more tokens (kill-switch philosophy).
//
// TRANSACTIONAL (F2-R01): first CHECKS all defenses WITHOUT mutating state
// (envelope, request, dedup, rate); only if the message passes ALL of them
// does it COMMIT the admission record (hashes/pairs/requests). Thus a
// message dropped by dedup or rate does NOT consume request_id quota nor
// register edges — before, state was mutated before those defenses and the
// counter/edges could reach reqMax/cycle with messages never admitted.
//
// The COMMIT returns an ADMISSION TOKEN (F2-R02): it describes exactly each
// mutation made (hash+ts, pairKey+ts, rid+instance+edge). The rollback
// after a failed publishDM receives that token and undoes ONLY that
// admission, even if another concurrent admission touched the same
// structures while the publish await released the event loop.
function antiLoopCheck(fromName, toName, text) {
  const now = Date.now();
  const admissionId = ANTILOOP.nextAdmissionId++;  // identidad ÚNICA (F2-R03/R04)
  if (now - ANTILOOP.lastSweep > 30000) { ANTILOOP.lastSweep = now; sweepLoopState(now); }
  const textStr = String(text);

  // 0) Protocol envelope (norma v1.3) — if the message carries one.
  //    Pure checks (no mutation): expired/hops/edge are the most
  //    precise signal of a creative loop (new rids, different text).
  const parsed = parseEnvelope(textStr);
  if (parsed) {
    const env = parsed.env;
    if (env.expires && now > env.expires) {
      ANTILOOP.dropped.expired++;
      return {ok: false, reason: 'expired', detail: 'envelope caducado (' + new Date(env.expires).toISOString() + ')'};
    }
    if (env.hops >= ANTILOOP.maxHops) {
      ANTILOOP.dropped.hops++;
      return {ok: false, reason: 'hops', detail: 'envelope supera max_hops=' + ANTILOOP.maxHops + ' (hops=' + env.hops + ')'};
    }
    if (traceHasEdge(env.trace, fromName, toName)) {
      ANTILOOP.dropped.cycle++;
      return {ok: false, reason: 'cycle', detail: 'arista ' + fromName + '->' + toName + ' ya recorrida (trace ' + JSON.stringify(env.trace) + ')'};
    }
  }

  // 1) Cortocircuito por request_id (formato norma: {org}-{yyyymmdd}-{seq4})
  //    — goes BEFORE the pair rate: it is the most specific loop signal.
  //    CHECK read-only: does not mutate count/edges; COMMIT happens in step 4.
  const rid = resolveRid(parsed, textStr);
  let reqEntry = null;   // existing entry of the rid (if already tracked)
  if (rid) {
    reqEntry = ANTILOOP.requests.get(rid) || null;
    if (reqEntry) {
      // Arista emisor->destino ya recorrida en este hilo = ping-pong
      // (funciona incluso si los bots no cooperan con el envelope).
      const edgeKey = fromName + '|' + toName;
      if (reqEntry.edges.has(edgeKey)) {
        ANTILOOP.dropped.cycle++;
        return {ok: false, reason: 'cycle', detail: 'arista ' + fromName + '->' + toName + ' repetida en el hilo ' + rid};
      }
      // count = apariciones ADMITIDAS (solo sube en COMMIT): el cortocircuito
      // salta cuando se alcanza el tope, no cuando se supera por descartados.
      if (reqEntry.count >= ANTILOOP.reqMax) {
        if (!reqEntry.tripped) {
          reqEntry.tripped = true;  // one-time instrumentation (does not consume quota)
          ANTILOOP.dropped.request++;
          console.warn('[antiloop] ⚠ CORTOCIRCUITO request_id', rid, '-', reqEntry.count, 'apariciones admitidas en', Math.round((now - reqEntry.first) / 1000) + 's por', [...reqEntry.agents].join(', '));
        }
        return {ok: false, reason: 'request', detail: 'request_id ' + rid + ' repetido (' + reqEntry.count + 'x admitidos)'};
      }
    }
  }

  // 2) Content-fingerprint dedup (F2-05) — CHECK read-only
  //    (the record is only written in COMMIT).
  //    - The content compared is the envelope REST if present (not the
  //      full text): so a different rid/trace/hops with the SAME content
  //      falls into dedup. This is the piece that closes F2-05 (bot that
  //      strips the envelope and re-publishes with a new rid).
  //    - Level 1 (canonical): djb2 hash of from|to|canonicalText.
  //      Kills trivial reformatting (spaces, uppercase, punctuation,
  //      accents, emoji).
  //    - Level 2 (fuzzy): if the canonical does not match exactly, the
  //      unigram set (shingles) is compared against recent messages of
  //      the SAME pair with Jaccard >= fuzzyThreshold → near-identical
  //      paraphrase → drop. Only against the same pair (from->to) so a
  //      legitimate broadcast is not dropped.
  const contentStr = parsed ? parsed.rest : textStr;
  const canon = canonicalize(stripRids(contentStr));
  const shingles = shingleSet(canon);
  const h = hashMsg(fromName, toName, canon);
  if (ANTILOOP.hashes.has(h)) {
    ANTILOOP.dropped.hash++;
    return {ok: false, reason: 'dedup', detail: 'identical message repeated by ' + fromName + ' -> ' + toName};
  }
  if (shingles.size && ANTILOOP.hashes.size) {
    const pk = fromName + '|' + toName;
    for (const [hk, hv] of ANTILOOP.hashes) {
      if (hk === h) continue;
      if (hv.pair !== pk || !hv.shingles || !hv.shingles.size) continue;
      if (jaccard(shingles, hv.shingles) >= ANTILOOP.fuzzyThreshold) {
        ANTILOOP.dropped.fuzzy++;
        return {ok: false, reason: 'fuzzy', detail: 'near-identical content (' + jaccard(shingles, hv.shingles).toFixed(2) + ') repeated by ' + fromName + ' -> ' + toName};
      }
    }
  }

  // 3) Pair rate from->to — CHECK read-only (the mark is added in COMMIT).
  const pk = fromName + '|' + toName;
  const arr = ANTILOOP.pairs.get(pk) || [];
  const fresh = arr.filter(e => now - e.ts < ANTILOOP.pairWindowMs);
  if (fresh.length >= ANTILOOP.pairMax) {
    ANTILOOP.dropped.pair++;
    return {ok: false, reason: 'rate', detail: fromName + '->' + toName + ' (' + fresh.length + ' msgs en ' + Math.round(ANTILOOP.pairWindowMs / 1000) + 's)'};
  }

  // 3b) HOURLY pair rate (F3-01) — same read-only philosophy:
  //     cuts slow loops that evade the per-minute limit.
  const arrH = ANTILOOP.pairHours.get(pk) || [];
  const freshH = arrH.filter(e => now - e.ts < ANTILOOP.pairHourWindowMs);
  if (freshH.length >= ANTILOOP.pairHourMax) {
    ANTILOOP.dropped.pair++;
    return {ok: false, reason: 'rate', detail: fromName + '->' + toName + ' (' + freshH.length + ' msgs en ' + Math.round(ANTILOOP.pairHourWindowMs / 3600000) + 'h, hourly limit)'};
  }

  // 4) COMMIT — all defenses passed: record the admission and
  //    build the ADMISSION TOKEN (F2-R02) that identifies each mutation.
  //    The identity is admissionId (monotonic counter, F2-R03/R04): the
  //    timestamp is NOT unique (two admissions can land in the same ms) and
  //    therefore cannot be used to tell marks apart.
  const admission = {
    admissionId: admissionId,
    hash: h,
    pairKey: pk,
    rid: null,
    requestEntry: null,
    edgeKey: null
  };
  if (rid) {
    // edges es Map<arista, ocurrencias>: dos admisiones concurrentes del
    // mismo RID y la misma arista se cuentan por separado, y el rollback de
    // una decrementa sin borrar la arista de la otra (F2-R02).
    const r = reqEntry || {count: 0, first: now, last: now, agents: new Set(), edges: new Map(), tripped: false};
    r.count++;
    r.last = now;
    r.agents.add(fromName);
    r.agents.add(toName);
    const edgeKey = fromName + '|' + toName;
    r.edges.set(edgeKey, (r.edges.get(edgeKey) || 0) + 1);
    ANTILOOP.requests.set(rid, r);
    admission.rid = rid;
    admission.requestEntry = r;
    admission.edgeKey = edgeKey;
    // Hard cap of entries (LRU by last) — F2-08: even though the time
    // window already evicts, a bot generating many new rids in the window
    // must not be able to grow the map without limit.
    if (ANTILOOP.requests.size > ANTILOOP.requestMax) {
      let oldestK = null, oldestT = Infinity;
      for (const [k, rr] of ANTILOOP.requests) if (rr.last < oldestT) { oldestT = rr.last; oldestK = k; }
      if (oldestK) ANTILOOP.requests.delete(oldestK);
    }
  }
  ANTILOOP.hashes.set(h, {id: admissionId, ts: now, pair: pk, canon, shingles});
  if (ANTILOOP.hashes.size > ANTILOOP.hashMax) {
    const oldest = ANTILOOP.hashes.keys().next().value;
    ANTILOOP.hashes.delete(oldest);
    ANTILOOP.evictedHashes++; // observable degradation (F2-09)
  }
  fresh.push({id: admissionId, ts: now});
  ANTILOOP.pairs.set(pk, fresh);
  // Hourly mark (F3-01): same monotonic identity for rollback.
  const freshH2 = arrH.filter(e => now - e.ts < ANTILOOP.pairHourWindowMs);
  freshH2.push({id: admissionId, ts: now});
  ANTILOOP.pairHours.set(pk, freshH2);

  return {ok: true, admission};
}

// Compensates the mutations of ONE concrete admission when the subsequent
// publication fails (F2-10 + F2-R02): receives the ADMISSION TOKEN returned
// by antiLoopCheck() and undoes exactly that admission.
//
// F2-R02 (concurrency): between this COMMIT and rollback, the `await
// publishDM()` releases the event loop and another concurrent admission may
// have touched the same structures. That is why it does not look up by
// (from,to,text):
//   - hash: deleted only if the current entry is still OURS
//     (same admissionId). Protects against re-registration of the same
//     hash after hashMax eviction — F2-R04: the id matters, not the ts
//     (re-registration can land in the same millisecond as the original).
//   - pair: OUR exact mark is removed (findIndex by admissionId +
//     splice), never pop(). F2-R03: two admissions can share the SAME
//     timestamp (Date.now() is not unique); looking up by ts would remove
//     the first match, which could be ANOTHER admission's mark.
//   - request: validates the current entry is the SAME instance we
//     admitted (protects against requestMax eviction + re-creation of the
//     same RID); and the edge is decremented (Map with counts), not
//     blindly deleted (two concurrent admissions can share the edge).
function antiLoopRollback(admission) {
  if (!admission) return;
  // Dedup: only if our mark is still the current one (same admission).
  const hEntry = ANTILOOP.hashes.get(admission.hash);
  if (hEntry && hEntry.id === admission.admissionId) {
    ANTILOOP.hashes.delete(admission.hash);
  }
  // Pair rate: remove OUR exact mark.
  const arr = ANTILOOP.pairs.get(admission.pairKey);
  if (arr) {
    const idx = arr.findIndex(e => e.id === admission.admissionId);
    if (idx >= 0) arr.splice(idx, 1);
    if (!arr.length) ANTILOOP.pairs.delete(admission.pairKey);
  }
  // Tasa HORARIA (F3-01): eliminar NUESTRA marca exacta igual que la de minuto.
  const arrH2 = ANTILOOP.pairHours.get(admission.pairKey);
  if (arrH2) {
    const idxH = arrH2.findIndex(e => e.id === admission.admissionId);
    if (idxH >= 0) arrH2.splice(idxH, 1);
    if (!arrH2.length) ANTILOOP.pairHours.delete(admission.pairKey);
  }
  // Request_id: solo si la entrada actual es la misma que admitimos.
  if (admission.rid && admission.requestEntry) {
    const current = ANTILOOP.requests.get(admission.rid);
    if (current === admission.requestEntry) {
      current.count = Math.max(0, current.count - 1);
      const c = current.edges.get(admission.edgeKey);
      if (c && c > 1) current.edges.set(admission.edgeKey, c - 1);
      else current.edges.delete(admission.edgeKey);
      if (current.count === 0 && !current.tripped) ANTILOOP.requests.delete(admission.rid);
    }
  }
}


// ---------------------------------------------------------------------------
// Nostr — common layer (publishDM, subscribe, handling)
// ---------------------------------------------------------------------------
if (!CONFIG.nostr.nsec || CONFIG.nostr.nsec.startsWith('CHANGE_ME_')) {
  throw new Error(
    'PhantomBridge: nostr.nsec no configurada. Copia config.example.json a config.json y establece una nsec real (npub1.../nsec1...) en nostr.nsec.'
  );
}
const {data: bridgeSk} = nip19.decode(CONFIG.nostr.nsec);
const bridgePk = getPublicKey(bridgeSk);

async function publishDM(recipientPk, content, title) {
  // NIP-17 gift wrap con created_at = now() (NO randomNow):
  // phantombot consulta con ventana since=now-120s; los wraps con
  // fecha backdated 0-48h (randomNow) quedan fuera de la ventana y
  // nunca llegan. Fijamos created_at real en rumor, seal y wrap.
  const nowTs = Math.floor(Date.now() / 1000);
  const SEAL_KIND = 13, GIFT_WRAP_KIND = 1059;
  const rumor = {
    kind: 14,
    created_at: nowTs,
    content,
    tags: [["p", recipientPk]],
    pubkey: bridgePk
  };
  rumor.id = getEventHash(rumor); // mandatory canonical id
  const seal = finalizeEvent({
    kind: SEAL_KIND,
    content: nip44.encrypt(JSON.stringify(rumor), nip44.getConversationKey(bridgeSk, recipientPk)),
    created_at: nowTs,
    tags: []
  }, bridgeSk);
  const randomKey = generateSecretKey();
  const wrapped = finalizeEvent({
    kind: GIFT_WRAP_KIND,
    content: nip44.encrypt(JSON.stringify(seal), nip44.getConversationKey(randomKey, recipientPk)),
    created_at: nowTs,
    tags: [["p", recipientPk]]
  }, randomKey);
  const res = await new Promise((resolve, reject) => {
    const ws = new WebSocket(CONFIG.nostr.relay);
    let sent = false;
    const timer = setTimeout(() => { ws.close(); reject(new Error('timeout publicando DM')); }, 10000);
    const sendEvent = () => {
      if (sent) return;
      sent = true;
      ws.send(JSON.stringify(['EVENT', wrapped]));
    };
    ws.onopen = () => {
      // Do not send the EVENT here: wait to see if the relay asks for AUTH.
      // Si en 800ms no llega AUTH, publicar directo (relay abierto).
      setTimeout(() => { if (!sent) sendEvent(); }, 800);
    };
    ws.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data.toString()); }
      catch (err) { console.error('[nostr] publishDM: frame no-JSON ignorado:', e.data.toString().slice(0, 120)); return; }
      if (m[0] === 'AUTH') {
        const ev = finalizeEvent(makeAuthEvent(CONFIG.nostr.relay, m[1]), bridgeSk);
        ws.send(JSON.stringify(['AUTH', ev]));
        setTimeout(sendEvent, 300);
      } else if (m[0] === 'OK') {
        if (m[1] === wrapped.id) { clearTimeout(timer); ws.close(); resolve(m[2]); }
      } else if (m[0] === 'NOTICE') {
        clearTimeout(timer); ws.close(); reject(new Error(m[1]));
      }
    };
    ws.onerror = () => { clearTimeout(timer); reject(new Error('ws error')); };
  });
  return res;
}

// Subscription: listen to gift-wraps addressed to the bridge
function subscribeIncoming() {
  const ws = new WebSocket(CONFIG.nostr.relay);
  // `since`: start from the last processed event (with an overlap
  // margin) so the historical backlog is not reprocessed on every reconnect.
  // No previous state -> full backlog (original behavior).
  let since = null;
  if (bridgeState && bridgeState.relay === CONFIG.nostr.relay && bridgeState.lastSeen > 0) {
    since = bridgeState.lastSeen - STATE_OVERLAP_SECS;
    console.log('[nostr] subscription since', new Date(since * 1000).toISOString(), '(last processed ' + new Date(bridgeState.lastSeen * 1000).toISOString() + ')');
  } else {
    console.log('[nostr] no previous state for this relay: full backlog subscription');
  }
  const REQ_FILTER = {kinds: [1059], '#p': [bridgePk]};
  if (since !== null) REQ_FILTER.since = since;
  const sendReq = () => ws.send(JSON.stringify(['REQ', 'bridge-in', REQ_FILTER]));
  let authSent = false;
  let reqSent = false;
  ws.onopen = () => {
    // Do NOT send REQ here: with nip42_auth the relay serves only the
    // historical backlog to an unauthenticated subscription, and live
    // streaming stays dead. Wait for the AUTH challenge (or the 800ms
    // fallback if the relay is open) and send REQ ONLY after authenticating.
    setTimeout(() => {
      if (!authSent && !reqSent) { reqSent = true; sendReq(); }
    }, 800);
  };
  ws.onmessage = async (e) => {
    let m;
    try { m = JSON.parse(e.data.toString()); }
    catch (err) { console.error('[nostr] subscribeIncoming: frame no-JSON ignorado:', e.data.toString().slice(0, 120)); return; }
    if (m[0] === 'AUTH') {
      const ev = finalizeEvent(makeAuthEvent(CONFIG.nostr.relay, m[1]), bridgeSk);
      ws.send(JSON.stringify(['AUTH', ev]));
      authSent = true;
      // Wait for the relay to validate the AUTH signature (asynchronous
      // verification) before registering the subscription; otherwise the REQ
      // is processed as unauthenticated and live streaming never arrives.
      setTimeout(() => { if (!reqSent) { reqSent = true; sendReq(); } }, 300);
    } else if (m[0] === 'EVENT') {
      handleIncomingGiftWrap(m[2]);
    }
  };
  ws.onclose = () => { console.error('[nostr] connection closed, reconnecting in 5s'); setTimeout(subscribeIncoming, 5000); };
  // NOTE: do not call ws.close() here. With Node's global WebSocket (undici),
  // onclose fires automatically after an error; close() inside onerror
  // re-triggers the error cycle -> infinite recursion (stack overflow).
  ws.onerror = () => {};
}

async function handleIncomingGiftWrap(giftWrap) {
  try {
    // Deduplication by ID: if this gift-wrap was already processed (e.g.
    // the relay re-sent it within the overlap after a restart), ignore it.
    if (isSeen(giftWrap.id)) {
      console.log('[nostr] duplicate gift-wrap ignored:', giftWrap.id.slice(0, 8));
      return;
    }
    // Record the last seen event (even if paused or from an
    // pubkey no autorizado): el estado marca lo ya RECIBIDO, no lo
    // procesado, para no re-entregar DMs viejos tras un reinicio.
    if (giftWrap && giftWrap.created_at) updateLastSeen(giftWrap.created_at);
    markSeen(giftWrap.id);
    // Kill-switch: nostr paused -> DMs are ignored SILENTLY (no
    // response, so bots process nothing and burn no tokens).
    if (PAUSED.nostr) {
      console.log('[nostr] paused: DM ignored');
      return;
    }
    const unwrapped = unwrapEvent(giftWrap, bridgeSk);
    const senderPk = unwrapped.pubkey;
    const senderName = agentByPubkey.get(senderPk);
    if (!senderName) {
      console.log('[nostr] DM from unauthorized pubkey, ignored:', senderPk.slice(0, 8));
      return;
    }
    const content = unwrapped.content;
    console.log('[nostr] DM from', senderName, ':', content.slice(0, 80));
    const cmd = content.trim().toLowerCase();

    // --- Comandos comunes ---
    if (cmd === 'status' || cmd === 'help' || cmd === 'routes') {
      await publishDM(senderPk, buildHelp(senderName), 'Bridge').catch(err => console.error('[bridge] DM failed:', err.message));
      return;
    }
    if (cmd === 'grabaciones' || cmd === 'recordings' || cmd === 'grabaciones?') {
      if (!JITSI_MODE) {
        await publishDM(senderPk, 'Este bridge no gestiona salas Jitsi (modo ' + MODE + ').', 'Bridge').catch(() => {});
        return;
      }
      const recs = listRecordings();
      const reply = formatRecordingsList(recs);
      console.log('[recordings] listado pedido por', senderName, '->', reply.slice(0, 60));
      await publishDM(senderPk, reply, 'Grabaciones').catch(err => console.error('[recordings] DM failed:', err.message));
      return;
    }

    // --- Routing DM↔DM (nostr/both mode): "@agent text" ---
    if (NOSTR_MODE) {
      const route = parseRouteTarget(content);
      if (route) {
        await handleRoute(senderName, senderPk, route);
        return;
      }
    }

    // --- Modo jitsi: comandos de salas y mensajes a sala ---
    if (JITSI_MODE) {
      if (PAUSED.jitsi) {
        console.log('[nostr] jitsi paused: DM from', senderName, 'ignored (room command)');
        await publishDM(senderPk, '⏸ The Jitsi bridge is paused. Resume with POST /pause {side:jitsi, paused:false}.', 'Bridge').catch(() => {});
        return;
      }
      const joinM = content.match(/^join(?:\s+(\[[^\]]+\]|\S+))?/i);
      const leaveM = content.match(/^leave(?:\s+(\[[^\]]+\]|\S+))?/i);
      if (joinM || leaveM) {
        if (!handleJoinLeaveFn) {
          console.log('[nostr] handleJoinLeave no disponible (jitsi no iniciado)');
          return;
        }
        await handleJoinLeaveFn(senderName, senderPk, content, joinM, leaveM);
        return;
      }
      let room = null, text = content;
      const m = content.match(/^\[([^\]]+)\]\s*(.*)$/s);
      if (m) { room = m[1].trim(); text = m[2].trim(); }
      if (!room) room = lastRoomByAgent.get(senderName);
      if (!room || !text) {
        console.log('[nostr] DM sin sala ni texto, ignorado');
        return;
      }
      if (!rooms.has(room)) {
        console.log('[nostr] sala', room, 'no activa en el puente, ignorado');
        return;
      }
      await sendToRoom(room, `[${senderName}] ${text}`);
      return;
    }

    console.log('[nostr] DM no procesado (modo ' + MODE + '):', content.slice(0, 60));
  } catch (err) {
    console.error('[nostr] error procesando gift-wrap:', err.message);
  }
}

function buildHelp(senderName) {
  const lines = ['Bridge modo ' + MODE + '. Comandos:'];
  if (NOSTR_MODE) {
    lines.push('  @agente texto        — enviar mensaje a otro agente');
    const perms = routingPerms[senderName];
    lines.push('  Puedes hablar con: ' + (perms ? perms.join(', ') : (routingDefault === 'allow' ? 'todos' : 'nadie (default deny)')));
  }
  if (JITSI_MODE) {
    lines.push('  join [sala]          — activar sala');
    lines.push('  leave [sala]         — desactivar sala');
    lines.push('  [sala] texto         — enviar texto a sala');
    lines.push('  grabaciones          — listar grabaciones');
  }
  lines.push('  status                — este mensaje');
  return lines.join('\n');
}

// Enruta un DM "@agente texto" al destinatario si los permisos lo permiten.
async function handleRoute(fromName, fromPk, route) {
  const toPk = agentByName.get(route.to);
  if (!toPk) {
    await publishDM(fromPk, 'Agente desconocido: @' + route.to + '. Agentes: ' + [...agentByName.keys()].join(', '), 'Bridge').catch(() => {});
    return;
  }
  if (!routingAllowed(fromName, route.to, routingPerms, routingDefault)) {
    console.log('[routing] bloqueado:', fromName, '->', route.to);
    await publishDM(fromPk, 'No tienes permiso para escribir a @' + route.to + '.', 'Bridge').catch(() => {});
    return;
  }

  // Anti-loop: content dedup + pair rate + request_id short-circuit.
  // If it trips, the message is dropped IN SILENCE (log + counter, no reply to sender).
  const loop = antiLoopCheck(fromName, route.to, route.text);
  if (!loop.ok) {
    console.warn('[antiloop] message blocked (' + loop.reason + '):', loop.detail);
    return;
  }
  const admission = loop.admission;
  ANTILOOP.routed++;
  console.log('[routing]', fromName, '->', route.to, ':', route.text.slice(0, 60));
  // Seal the protocol envelope (norma v1.3): the bridge is the choke point and
  // keeps the chain alive even if the bots don't cooperate (creates it if missing).
  // F2-01: the envelope is ALWAYS the real first line; the [from] (sender)
  // goes as metadata AFTER the envelope line, so that the norma
  // "copy the [env] line as-is" is unambiguous for the bot.
  const stamped = stampEnvelope(route.text, fromName, route.to);
  const delivered = stamped.replace(/\n/, '\n[' + fromName + '] ');
  const ok = await publishDM(toPk, delivered, `Bridge ${fromName}`).catch(err => {
    console.error('[routing] DM failed:', err.message);
    return false;
  });
  if (ok) {
    await publishDM(fromPk, `Mensaje entregado a @${route.to}.`, 'Bridge').catch(() => {});
  } else {
    // F2-10 + F2-R02: the publication failed — compensate ONLY the
    // anti-loop state consumed by THIS admission (hash/pair/rid), using the
    // COMMIT admission token. So the sender's retry does not fall into a
    // false loop positive due to a network failure, and concurrent
    // admissions that touched the same structures are not undone.
    antiLoopRollback(admission);
    ANTILOOP.routed--;
    await publishDM(fromPk, `No pude entregar el mensaje a @${route.to}.`, 'Bridge').catch(() => {});
  }
}


// ---------------------------------------------------------------------------
// Jitsi (XMPP MUC) — solo si JITSI_MODE
// ---------------------------------------------------------------------------
const rooms = new Map();         // sala -> {joinedAt, nick, agents: [nombres]}
const pendingIQs = new Map();    // id -> {resolve, reject, timer}
const discoPings = new Map();    // id -> {room, timer}
const DEFAULT_TIMEOUT_MIN = 15;
const ALONE_GRACE_MS = 30000;
const roomTimeouts = new Map();
const roomOccupants = new Map();
const aloneTimers = new Map();
const lastActivity = new Map();

const roomAgents = new Map();
for (const [room, agents] of Object.entries(CONFIG.roomAgents || {})) {
  if (Array.isArray(agents)) roomAgents.set(room, agents);
}
for (const [room, t] of Object.entries(CONFIG.roomTimeouts || {})) {
  const n = parseInt(t, 10);
  if (!isNaN(n) && n > 0) roomTimeouts.set(room, n);
}
function getTimeoutMin(room) { return roomTimeouts.get(room) || DEFAULT_TIMEOUT_MIN; }

let xmpp = null;
// handleJoinLeave vive dentro del bloque JITSI_MODE (necesita joinRoom/
// leaveRoom/persistRoomTimeout); exposed here so the nostr flow can use it
// only when jitsi mode is active.
let handleJoinLeaveFn = null;
if (JITSI_MODE) {
  // STARTTLS upgrade: @xmpp/starttls calls tls.connect({socket, host}) and does
  // NOT propagate rejectUnauthorized from config (bug/limitation of xmpp.js:
  // the option only reaches direct TLS xmpps://). Therefore, with xmpp:// +
  // Prosody's self-signed cert, verification fails.
  //
  // SCOPED fix (not global): the STARTTLS upgrade is the ONLY case that
  // passes the `socket` option to tls.connect (reuses the existing TCP socket). If
  // present -> trust the cert (local XMPP connection only
  // 127.0.0.1); if not -> normal verification. Any other TLS connection
  // of the process (wss://, https://) stays intact.
  const tls = require('tls');
  const origTlsConnect = tls.connect;
  tls.connect = function (...args) {
    if (args[0] && typeof args[0] === 'object' && args[0].socket && CONFIG.xmpp.rejectUnauthorized === false) {
      args[0] = { ...args[0], rejectUnauthorized: false };
    }
    return origTlsConnect.apply(this, args);
  };
  const {client, xml, jid} = require('@xmpp/client');

  xmpp = client({
    service: CONFIG.xmpp.service || 'xmpp://127.0.0.1:5222',
    domain: CONFIG.xmpp.domain || 'auth.meet.example.com',
    username: CONFIG.xmpp.username || 'bridge',
    password: CONFIG.xmpp.password,
    ...(CONFIG.xmpp.rejectUnauthorized !== undefined ? {rejectUnauthorized: CONFIG.xmpp.rejectUnauthorized} : {}),
  });

  xmpp.on('error', (err) => console.error('[xmpp] error:', err.message));
  xmpp.on('offline', () => console.log('[xmpp] offline'));
  xmpp.on('online', async (address) => {
    console.log('[xmpp] online como', address.toString());
    const prev = [...rooms.entries()];
    rooms.clear();
    for (const [room, info] of prev) await joinRoom(room, {nick: info.nick});
    for (const room of CONFIG.startRooms || []) await joinRoom(room);
  });

  xmpp.on('stanza', async (stanza) => {
    if (stanza.is('iq') && stanza.attrs.id && discoPings.has(stanza.attrs.id)) {
      const p = discoPings.get(stanza.attrs.id);
      discoPings.delete(stanza.attrs.id);
      clearTimeout(p.timer);
      if (stanza.attrs.type === 'error') {
        const errEl = stanza.getChild('error');
        let cond = 'desconocido';
        if (errEl) {
          for (const c of ['item-not-found', 'service-unavailable', 'gone', 'not-allowed', 'forbidden']) {
            if (errEl.getChild(c)) { cond = c; break; }
          }
        }
        console.log(`[xmpp] sonda sala ${p.room}: error ${cond} -> sala destruida, limpiando`);
        leaveRoom(p.room);
      }
      return;
    }
    if (stanza.is('iq') && stanza.attrs.id && pendingIQs.has(stanza.attrs.id)) {
      const p = pendingIQs.get(stanza.attrs.id);
      pendingIQs.delete(stanza.attrs.id);
      clearTimeout(p.timer);
      if (stanza.attrs.type === 'result') {
        const conf = stanza.getChild('conference', 'http://jitsi.org/protocol/focus');
        const ready = conf && conf.attrs.ready === 'true';
        console.log('[focus] IQ result:', stanza.attrs.from, 'ready=' + ready, conf ? `room=${conf.attrs.room}` : '');
        p.resolve(ready);
      } else if (stanza.attrs.type === 'error') {
        const errEl = stanza.getChild('error');
        let cond = '';
        if (errEl) {
          for (const c of ['service-unavailable', 'not-allowed', 'item-not-found', 'internal-server-error', 'feature-not-implemented']) {
            if (errEl.getChild(c)) { cond = c; break; }
          }
        }
        console.error('[focus] IQ error:', cond || 'desconocido');
        p.reject(new Error('IQ error de focus: ' + (cond || stanza.toString().slice(0, 200))));
      }
      return;
    }
    if (stanza.is('presence')) {
      const from = stanza.attrs.from || '';
      const [roomJid, nick] = from.split('/');
      const room = roomJid ? roomJid.replace(ROOM_SUFFIX, '') : '';
      const selfNick = (rooms.get(room) || {}).nick || NICK;
      if (rooms.has(room) && nick && nick.toLowerCase() !== selfNick.toLowerCase() && nick.toLowerCase() !== 'focus') {
        const occ = roomOccupants.get(room) || new Set();
        if (stanza.attrs.type === 'unavailable') {
          occ.delete(nick);
          if (occ.size === 0) scheduleAloneLeave(room);
        } else {
          occ.add(nick);
          cancelAloneLeave(room);
        }
        roomOccupants.set(room, occ);
      }
      if (stanza.attrs.type === 'error') {
        console.error('[xmpp] ERROR presence en', from, '->', (stanza.getChild('error')||{}).getChildText ? '' : '');
        const errEl = stanza.getChild('error');
        if (errEl) console.error('[xmpp]   condition:', errEl.getChildText && (errEl.getChildText('item-not-found') || errEl.getChildText('not-allowed') || errEl.getChildText('forbidden') || errEl.getChildText('conflict')) || '(see stanza)');
        if (rooms.has(room)) {
          console.error('[xmpp] presencia error en sala, limpiando estado completo:', room);
          await leaveRoom(room);
        }
      }
      return;
    }
    if (stanza.is('message')) {
      const type = stanza.attrs.type;
      const body = stanza.getChild('body');
      if (type === 'groupchat' && body) {
        const from = stanza.attrs.from;
        const roomJid = from.split('/')[0];
        const nick = from.split('/')[1] || '';
        const text = body.getText();
        const room = roomJid.replace(ROOM_SUFFIX, '');
        const roomInfo = rooms.get(room);
        if (nick.toLowerCase() === (roomInfo ? roomInfo.nick : NICK).toLowerCase()) return;
        handleRoomMessage(room, nick, text);
      } else if (type === 'chat' && body) {
        const from = stanza.attrs.from;
        console.log('[xmpp] chat directo de', from, ':', body.getText());
      }
    }
  });

  function handleRoomMessage(room, nick, text) {
    lastActivity.set(room, Date.now());
    console.log(`[xmpp] <${room}> ${nick}: ${text.slice(0, 80)}`);
    if (!rooms.has(room)) {
      console.log('[xmpp] room not managed, joining:', room);
      joinRoom(room);
      return;
    }
    const allowed = roomAgents.get(room);
    const targets = allowed && allowed.length ? allowed : [...agentByName.keys()];
    const occupants = roomOccupants.get(room);
    const roster = targets.slice();
    if (occupants && occupants.size) roster.push(occupants.size + ' humano' + (occupants.size > 1 ? 's' : ''));
    const rosterStr = roster.join(', ');
    for (const name of targets) {
      lastRoomByAgent.set(name, room);
      const pk = agentByName.get(name);
      if (!pk) continue;
      const content = `[${room}] (participantes: ${rosterStr}) ${nick}: ${text}`;
      publishDM(pk, content, `Jitsi ${room}`).catch(err => console.error('[nostr] DM failed:', err.message));
    }
  }

  function allocateConference(roomJid) {
    return new Promise((resolve, reject) => {
      const id = `alloc-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const timer = setTimeout(() => {
        pendingIQs.delete(id);
        reject(new Error(`allocation timeout for ${roomJid} (jicofo did not respond in 15s)`));
      }, 15000);
      pendingIQs.set(id, { resolve, reject, timer });
      const iq = xml('iq', { to: CONFIG.xmpp.focus || 'focus.' + (CONFIG.xmpp.focusDomain || 'meet.example.com'), type: 'set', id },
        xml('conference', { xmlns: 'http://jitsi.org/protocol/focus', room: roomJid }));
      console.log('[focus] IQ allocation ->', roomJid, `(id=${id})`);
      xmpp.send(iq).catch(err => {
        clearTimeout(timer);
        pendingIQs.delete(id);
        reject(err);
      });
    });
  }

  async function joinRoom(room, opts = {}) {
    if (PAUSED.jitsi) { console.log('[xmpp] join', room, 'rejected: jitsi paused'); return {ok: false, error: 'jitsi paused'}; }
    if (rooms.has(room)) { console.log('[xmpp] already in room', room); return {ok: true, already: true}; }
    const roomJid = room + ROOM_SUFFIX;
    const nick = opts.nick || NICK;
    console.log('[xmpp] joining', roomJid, 'as', nick);
    let allocError = null;
    try {
      try {
        await allocateConference(roomJid);
        console.log('[focus] conferencia lista para', room);
      } catch (allocErr) {
        allocError = allocErr;
        console.error('[focus] allocation failed (continuing with join):', allocErr.message);
      }
      const muc = xml('x', {xmlns: 'http://jabber.org/protocol/muc'});
      if (opts.password) muc.append(xml('password', {}, opts.password));
      await xmpp.send(xml('presence', {to: `${roomJid}/${nick}`}, muc));
      rooms.set(room, {joinedAt: Date.now(), nick});
      lastActivity.set(room, Date.now());
      roomOccupants.set(room, new Set());
      if (opts.timeout) {
        const n = parseInt(opts.timeout, 10);
        if (!isNaN(n) && n > 0) roomTimeouts.set(room, n);
      }
      console.log('[xmpp] unido a', room, 'como', nick, '(timeout inactividad ' + getTimeoutMin(room) + ' min)');
      return {ok: true, allocError: allocError ? allocError.message : null};
    } catch (err) {
      console.error('[xmpp] error join', room, err.message);
      return {ok: false, error: err.message};
    }
  }

  async function leaveRoom(room) {
    if (!rooms.has(room)) return;
    const roomJid = room + ROOM_SUFFIX;
    const nick = rooms.get(room).nick || NICK;
    try {
      await xmpp.send(xml('presence', {to: `${roomJid}/${nick}`, type: 'unavailable'}));
      rooms.delete(room);
      lastActivity.delete(room);
      roomOccupants.delete(room);
      cancelAloneLeave(room);
      console.log('[xmpp] salido de', room);
    } catch (err) {
      console.error('[xmpp] error leave', room, err.message);
    }
  }

  function scheduleAloneLeave(room) {
    if (aloneTimers.has(room)) return;
    console.log('[xmpp] sala', room, 'sin ocupantes, cerrando en 30s');
    const t = setTimeout(async () => {
      aloneTimers.delete(room);
      if (rooms.has(room) && (roomOccupants.get(room) || new Set()).size === 0) {
        console.log('[xmpp] auto close (empty room):', room);
        await leaveRoom(room);
      }
    }, ALONE_GRACE_MS);
    aloneTimers.set(room, t);
  }

  function cancelAloneLeave(room) {
    const t = aloneTimers.get(room);
    if (t) { clearTimeout(t); aloneTimers.delete(room); }
  }

  function persistRoomTimeout(room, timeout) {
    const n = parseInt(timeout, 10);
    if (isNaN(n) || n <= 0) return;
    roomTimeouts.set(room, n);
    CONFIG.roomTimeouts = CONFIG.roomTimeouts || {};
    CONFIG.roomTimeouts[room] = n;
    fs.writeFileSync(CONFIG_PATH + '.tmp', JSON.stringify(CONFIG, null, 2));
    fs.renameSync(CONFIG_PATH + '.tmp', CONFIG_PATH);
  }

  function probeRoom(room) {
    if (PAUSED.jitsi) return;
    if (!rooms.has(room)) return;
    const roomJid = room + ROOM_SUFFIX;
    const id = 'probe-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
    const timer = setTimeout(() => {
      discoPings.delete(id);
      console.log('[xmpp] probe timeout for', room, '(no response, will retry)');
    }, 10000);
    discoPings.set(id, {room, timer});
    const iq = xml('iq', {to: roomJid, type: 'get', id},
      xml('query', {xmlns: 'http://jabber.org/protocol/disco#info'}));
    xmpp.send(iq).catch(err => {
      clearTimeout(timer);
      discoPings.delete(id);
      console.error('[xmpp] sonda error send', room, err.message);
    });
  }

  setInterval(() => {
    if (PAUSED.jitsi) return;
    for (const room of [...rooms.keys()]) probeRoom(room);
  }, 60000);

  setInterval(async () => {
    if (PAUSED.jitsi) return;
    const now = Date.now();
    for (const [room, info] of [...rooms.entries()]) {
      const last = lastActivity.get(room) || info.joinedAt || now;
      const humans = (roomOccupants.get(room) || new Set()).size;
      if (humans === 0 && now - last > getTimeoutMin(room) * 60000) {
        console.log('[xmpp] inactivity close (' + getTimeoutMin(room) + ' min, empty room):', room);
        await leaveRoom(room);
      }
    }
  }, 30000);

  async function sendToRoom(room, text) {
    if (PAUSED.jitsi) { console.log('[xmpp] send to', room, 'rejected: jitsi paused'); return; }
    const roomJid = room + ROOM_SUFFIX;
    try {
      await xmpp.send(xml('message', {to: roomJid, type: 'groupchat'}, xml('body', {}, text)));
      console.log('[xmpp] ->', room, ':', text.slice(0, 80));
    } catch (err) {
      console.error('[xmpp] error send:', err.message);
    }
  }

  async function handleJoinLeave(senderName, senderPk, content, joinM, leaveM) {
    const isJoin = !!joinM;
    if (isJoin && PAUSED.jitsi) {
      await publishDM(senderPk, '⏸ The Jitsi bridge is paused. Resume with POST /pause {side:jitsi, paused:false}.', 'Bridge').catch(() => {});
      return;
    }
    const rawRoom = (isJoin ? joinM[1] : leaveM[1] || '').trim();
    if (!rawRoom) {
      await publishDM(senderPk, 'Falta la sala. Uso: join [sala] | join [https://meet.../sala] [--nick X] [--password Y] [--timeout N] | leave [sala]', 'Bridge').catch(() => {});
      return;
    }
    let roomName = rawRoom;
    if (roomName.startsWith('[') && roomName.endsWith(']')) roomName = roomName.slice(1, -1);
    const urlM = roomName.match(/^https?:\/\/[^/]+\/([a-zA-Z0-9_-]+)$/);
    if (urlM) roomName = urlM[1];
    const opts = {nick: null, password: null, timeout: null};
    for (const f of content.matchAll(/--(nick|password|timeout)\s+(\S+)/gi)) opts[f[1].toLowerCase()] = f[2];
    if (isJoin) {
      await joinRoom(roomName, opts);
      if (opts.timeout) persistRoomTimeout(roomName, opts.timeout);
      const ok = rooms.has(roomName);
      const nick = ok ? rooms.get(roomName).nick : opts.nick || NICK;
      const reply = ok
        ? `Room ${roomName} activated in the bridge (joining as ${nick}${opts.password ? ', with password' : ''}).`
        : `Could not activate room ${roomName}. Check the bridge logs.`;
      console.log('[bridge] join via DM from', senderName, '->', roomName, 'nick=' + nick, ok ? 'ok' : 'failed');
      await publishDM(senderPk, reply, 'Bridge').catch(err => console.error('[bridge] DM failed:', err.message));
    } else {
      await leaveRoom(roomName);
      const reply = rooms.has(roomName)
        ? `Could not leave room ${roomName}.`
        : `Room ${roomName} deactivated from the bridge.`;
      console.log('[bridge] leave via DM from', senderName, '->', roomName);
      await publishDM(senderPk, reply, 'Bridge').catch(err => console.error('[bridge] DM failed:', err.message));
    }
  }
  handleJoinLeaveFn = handleJoinLeave;

  async function promoteUser(room, nick) {
    const roomJid = room + ROOM_SUFFIX;
    const presence = xml('presence', { to: roomJid },
      xml('x', { xmlns: 'http://jabber.org/protocol/muc#user' },
        xml('item', { nick, role: 'moderator' })));
    await xmpp.send(presence);
    return true;
  }
}

// ---------------------------------------------------------------------------
// API HTTP local
// ---------------------------------------------------------------------------
const server = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  const MAX_BODY = 64 * 1024; // 64KB: payloads diminutos ({room,nick,...}) — evita DoS por cuerpo grande (hallazgo Copilot 5)
  const readBody = () => new Promise((resolve, reject) => {
    let body = '';
    let size = 0;
    req.on('data', c => {
      size += c.length;
      if (size > MAX_BODY) {
        const err = new Error('body too large (max ' + MAX_BODY + ' bytes)');
        err.statusCode = 413;
        reject(err);
        req.pause(); // no destruir: la respuesta 413 debe llegar al cliente
        return;
      }
      body += c;
    });
    req.on('error', (err) => { err.statusCode = 400; reject(err); });
    req.on('end', () => resolve(body));
  });
  // Cierra la promesa de readBody ante error de stream/oversize: sin esto, un
  // reject quedaba como unhandled rejection y mataba el proceso.
  const handleBody = (fn) => {
    readBody().then(fn).catch(err => {
      res.statusCode = err.statusCode || 400;
      res.end(JSON.stringify({ok: false, error: err.message}));
    });
  };
  const parseBody = (body) => {
    try { return JSON.parse(body); }
    catch (err) { const e = new Error('invalid JSON: ' + err.message); e.statusCode = 400; throw e; }
  };

  if (req.method === 'POST' && req.url === '/join') {
    handleBody(async (body) => {
      try {
        if (!JITSI_MODE) return res.end(JSON.stringify({ok: false, error: 'mode ' + MODE + ': no Jitsi rooms'}));
        if (PAUSED.jitsi) return res.end(JSON.stringify({ok: false, error: 'jitsi paused'}));
        const {room, nick, password, timeout} = parseBody(body);
        if (!room) return res.end(JSON.stringify({ok: false, error: 'room required'}));
        const result = await joinRoom(room, {nick, password, timeout});
        if (timeout) persistRoomTimeout(room, timeout);
        if (!result || result.ok === false) {
          res.statusCode = 502;
          return res.end(JSON.stringify({ok: false, error: (result && result.error) || 'error joining the room'}));
        }
        res.end(JSON.stringify({ok: true, room, nick: nick || NICK, password: !!password, timeout: getTimeoutMin(room), allocError: result.allocError || null, already: !!result.already}));
      } catch (e) { res.statusCode = e.statusCode || 500; res.end(JSON.stringify({ok: false, error: e.message})); }
    });
  } else if (req.method === 'POST' && req.url === '/leave') {
    handleBody(async (body) => {
      try {
        if (!JITSI_MODE) return res.end(JSON.stringify({ok: false, error: 'mode ' + MODE + ': no Jitsi rooms'}));
        const {room} = parseBody(body);
        await leaveRoom(room);
        res.end(JSON.stringify({ok: true, room}));
      } catch (e) { res.statusCode = e.statusCode || 500; res.end(JSON.stringify({ok: false, error: e.message})); }
    });
  } else if (req.method === 'POST' && req.url === '/promote') {
    handleBody(async (body) => {
      try {
        if (!JITSI_MODE) return res.end(JSON.stringify({ok: false, error: 'mode ' + MODE + ': no Jitsi rooms'}));
        if (PAUSED.jitsi) return res.end(JSON.stringify({ok: false, error: 'jitsi paused'}));
        const {room, nick} = parseBody(body);
        if (!room || !nick) return res.end(JSON.stringify({ok: false, error: 'room and nick required'}));
        await promoteUser(room, nick);
        res.end(JSON.stringify({ok: true, room, nick}));
      } catch (e) { res.statusCode = e.statusCode || 500; res.end(JSON.stringify({ok: false, error: e.message})); }
    });
  } else if (req.method === 'GET' && req.url === '/recordings') {
    const recs = listRecordings();
    if (!Array.isArray(recs)) {
      res.statusCode = 500;
      return res.end(JSON.stringify({ok: false, error: (recs && recs.error) || 'error listando grabaciones'}));
    }
    recs.forEach(r => { r.url = mintDownloadUrl(r.name); });
    res.end(JSON.stringify({ok: true, recordings: recs}));
  } else if (req.method === 'GET' && req.url.startsWith('/recordings/')) {
    const name = decodeURIComponent(req.url.slice('/recordings/'.length));
    if (name.includes('/') || name.includes('\\')) {
      res.statusCode = 400;
      return res.end(JSON.stringify({ok: false, error: 'invalid name'}));
    }
    const full = RECORDINGS_DIR + '/' + name;
    if (!fs.existsSync(full)) {
      res.statusCode = 404;
      return res.end(JSON.stringify({ok: false, error: 'no existe'}));
    }
    res.setHeader('Content-Type', 'video/mp4');
    res.setHeader('Content-Disposition', 'attachment; filename="' + name + '"');
    const stream = fs.createReadStream(full);
    stream.on('error', () => { res.statusCode = 500; res.end('error leyendo archivo'); });
    stream.pipe(res);
  } else if (req.method === 'POST' && req.url === '/register') {
    handleBody(async (body) => {
      try {
        if (!JITSI_MODE) return res.end(JSON.stringify({ok: false, error: 'modo ' + MODE + ': sin salas Jitsi'}));
        if (PAUSED.jitsi) return res.end(JSON.stringify({ok: false, error: 'jitsi pausado'}));
        const {room, agents, timeout} = parseBody(body);
        if (!room) return res.end(JSON.stringify({ok: false, error: 'room requerido'}));
        if (!Array.isArray(agents)) return res.end(JSON.stringify({ok: false, error: 'agents debe ser array'}));
        const unknown = agents.filter(a => !agentByName.has(a));
        if (unknown.length) return res.end(JSON.stringify({ok: false, error: 'agentes desconocidos: ' + unknown.join(', ')}));
        // agents=[] = modo broadcast (responder a todos): se borra del Map en
        // runtime, but the empty entry IS PERSISTED in CONFIG so the
        // broadcast sobreviva reinicios (comportamiento intencional).
        if (agents.length === 0) roomAgents.delete(room);
        else roomAgents.set(room, agents);
        CONFIG.roomAgents = CONFIG.roomAgents || {};
        CONFIG.roomAgents[room] = agents;
        if (timeout) persistRoomTimeout(room, timeout);
        fs.writeFileSync(CONFIG_PATH + '.tmp', JSON.stringify(CONFIG, null, 2));
        fs.renameSync(CONFIG_PATH + '.tmp', CONFIG_PATH);
        console.log('[bridge] /register', room, agents.length ? agents.join(',') : '(broadcast)', timeout ? 'timeout=' + timeout + 'min' : '');
        res.end(JSON.stringify({ok: true, room, agents, broadcast: agents.length === 0, timeout: getTimeoutMin(room)}));
      } catch (e) { res.statusCode = e.statusCode || 500; res.end(JSON.stringify({ok: false, error: e.message})); }
    });
  } else if (req.method === 'POST' && req.url === '/pause') {
    handleBody(async (body) => {
      try {
        const {side, paused} = parseBody(body);
        if (typeof paused !== 'boolean') { res.statusCode = 400; return res.end(JSON.stringify({ok: false, error: 'paused must be boolean'})); }
        const state = setPaused(side, paused);
        console.log('[bridge] pause', side, '=', paused, '(state:', JSON.stringify(state) + ')');
        if (side === 'jitsi' || side === 'both') {
          if (paused && JITSI_MODE) {
            for (const room of [...rooms.keys()]) {
              await leaveRoom(room);
              console.log('[bridge] jitsi pause: room', room, 'left');
            }
          }
        }
        res.end(JSON.stringify({ok: true, side, paused, state}));
      } catch (e) { res.statusCode = e.statusCode || 400; res.end(JSON.stringify({ok: false, error: e.message})); }
    });
  } else if (req.method === 'GET' && req.url === '/status') {
    res.end(JSON.stringify({
      ok: true,
      mode: MODE,
      paused: {...PAUSED},
      nick: NICK,
      rooms: JITSI_MODE ? [...rooms.keys()] : [],
      roomNicks: JITSI_MODE ? Object.fromEntries([...rooms.entries()].map(([r, i]) => [r, i.nick])) : {},
      roomAgents: JITSI_MODE ? Object.fromEntries(roomAgents) : {},
      roomTimeouts: JITSI_MODE ? Object.fromEntries(roomTimeouts) : {},
      roomOccupants: JITSI_MODE ? Object.fromEntries([...roomOccupants.entries()].map(([r, s]) => [r, [...s]])) : {},
      roomIdleSecs: JITSI_MODE ? Object.fromEntries([...rooms.keys()].map(r => [r, Math.round((Date.now() - (lastActivity.get(r) || Date.now())) / 1000)])) : {},
      agents: Object.fromEntries(agentByName),
      routing: NOSTR_MODE ? routingPerms : undefined,
      antiloop: {
        routed: ANTILOOP.routed,
        dropped: {...ANTILOOP.dropped},
        activePairs: [...ANTILOOP.pairs.keys()],
        activeRequests: [...ANTILOOP.requests.entries()].map(([id, r]) => ({id, count: r.count, agents: [...r.agents], edges: [...r.edges.keys()]})),
        config: {maxHops: ANTILOOP.maxHops, expireMs: ANTILOOP.expireMs, reqMax: ANTILOOP.reqMax, requestMax: ANTILOOP.requestMax, hashMax: ANTILOOP.hashMax, fuzzyThreshold: ANTILOOP.fuzzyThreshold, pairMax: ANTILOOP.pairMax, pairHourMax: ANTILOOP.pairHourMax, marker: ENVELOPE_MARKER},
        evictedHashes: ANTILOOP.evictedHashes,
      },
      xmpp: JITSI_MODE ? (xmpp.status ? xmpp.status : 'connected?') : 'n/a',
    }));
  } else {
    res.statusCode = 404;
    res.end(JSON.stringify({ok: false, error: 'not found'}));
  }
});

// Start only when executed directly (not on require() for tests)
if (require.main === module) {
  console.log('[bridge] starting in mode ' + MODE + '...');
  if (!CONFIG.nostr || !CONFIG.nostr.relay || !CONFIG.nostr.nsec) {
    console.error('[bridge] incomplete config: missing nostr.relay / nostr.nsec');
    process.exit(1);
  }
  if (JITSI_MODE && (!CONFIG.xmpp || !CONFIG.xmpp.password)) {
    console.error('[bridge] mode ' + MODE + ' requires config.xmpp (service/domain/username/password)');
    process.exit(1);
  }

  server.listen(CONFIG.httpPort || 8090, '127.0.0.1', () => {
    console.log('[http] local API on :' + (CONFIG.httpPort || 8090));
  });

  // In pure nostr mode XMPP is not needed: the bridge is only a DM↔DM router.
  const start = async () => {
    subscribeIncoming();
    if (JITSI_MODE) {
      try {
        await xmpp.start();
        console.log('[bridge] XMPP online, nostr relay connected.');
      } catch (err) {
        console.error('[bridge] XMPP startup error:', err.message);
        process.exit(1);
      }
    } else {
      console.log('[bridge] mode ' + MODE + ': no XMPP, nostr routing only.');
    }
  };
  start();
}

// Exports para tests unitarios (routing/parseo) — solo si se require()a este archivo
module.exports = {
  MODE, JITSI_MODE, NOSTR_MODE,
  parseRouteTarget,
  routingAllowed,
  buildHelp,
  PAUSED,
  isPaused,
  setPaused,
  antiLoopCheck,
  antiLoopRollback,
  resolveRid,
  ANTILOOP,
  parseEnvelope,
  stampEnvelope,
  extractRid,
  server,   // HTTP API (para tests: server.listen(0) y fetch)
  CONFIG,
};
