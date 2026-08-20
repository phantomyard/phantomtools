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
const {execFileSync} = require('child_process');
const {nip19, finalizeEvent, generateSecretKey, getPublicKey, getEventHash, verifyEvent} = require('nostr-tools');
const nip44 = require('nostr-tools/nip44');
const {makeAuthEvent} = require('nostr-tools/nip42');
const {loadOrgRouting} = require('./org-routing.js');

const CONFIG_PATH = process.argv[2] || (process.env.PHANTOMBRIDGE_CONFIG || './config.json');

function assertPrivateFile(file, label) {
  const st = fs.statSync(file);
  if (!st.isFile()) throw new Error(label + ' must be a regular file: ' + file);
  if ((st.mode & 0o077) !== 0) {
    throw new Error(label + ' must have mode 0600 or stricter: ' + file +
      ' (current ' + (st.mode & 0o777).toString(8) + ')');
  }
}

assertPrivateFile(CONFIG_PATH, 'PhantomBridge config');
const CONFIG = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));

// ---------------------------------------------------------------------------
// Secret resolution (CONTRIBUTING.md §4.6). A secret is configured as a
// REFERENCE, never as a plaintext value or a tool-owned plaintext file:
//   "vault:NAME" -> resolved at startup via `phantombot vault get NAME`
//                   (the persona's AES-256-GCM vault; fail-closed on error).
//   "env:VAR"    -> resolved from the operator-injected environment variable.
// Any other non-empty string is a plaintext inline secret and is REJECTED.
// Legacy tool-owned plaintext file keys (*File) are REJECTED with a migration
// hint. The bridge owns no plaintext secret store.
// ---------------------------------------------------------------------------
function resolveSecretRef(ref, label) {
  if (typeof ref !== 'string' || !ref.trim()) {
    throw new Error(label + ': missing secret reference (use "vault:NAME" or "env:VAR")');
  }
  const r = ref.trim();
  if (r.startsWith('vault:')) {
    const name = r.slice('vault:'.length).trim();
    if (!name) throw new Error(label + ': empty vault reference');
    let out;
    try {
      out = execFileSync('phantombot', ['vault', 'get', name], {encoding: 'utf8'});
    } catch (e) {
      throw new Error(label + ': cannot resolve vault:' + name + ' (phantombot vault get failed)');
    }
    const value = (out || '').trim();
    if (!value) throw new Error(label + ': vault:' + name + ' resolved to an empty value');
    return value;
  }
  if (r.startsWith('env:')) {
    const varName = r.slice('env:'.length).trim();
    if (!varName) throw new Error(label + ': empty env reference');
    const value = process.env[varName];
    if (!value || !value.trim()) throw new Error(label + ': env:' + varName + ' is not set');
    return value.trim();
  }
  throw new Error(label + ': plaintext secret not allowed — use "vault:NAME" (phantombot vault) or "env:VAR" (operator-injected environment), never a plaintext value');
}

function readSecret(configSection, inlineKey, fileKey, label) {
  const section = configSection || {};
  if (section[fileKey] !== undefined && section[fileKey] !== null) {
    throw new Error(label + ': ' + fileKey + ' (tool-owned plaintext secret file) is no longer supported — use "' + inlineKey + '": "vault:NAME" (phantombot vault) or "env:VAR" (operator-injected environment)');
  }
  const ref = section[inlineKey];
  if (ref === undefined || ref === null) return null;
  return resolveSecretRef(ref, label);
}

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

// Kill-switch durability (WORKING-ANY-MODE): the per-side pause must survive
// a restart/reconnect in EVERY mode (jitsi, nostr, both). It cannot depend
// on `bridgeState` (which is only initialized in NOSTR_MODE): persist it in
// its own file so a paused side stays paused in a jitsi-only deployment too.
// CONFIG.pauseFile (opcional) customiza el path; default junto al config.
const PAUSE_FILE = CONFIG.pauseFile || path.join(path.dirname(CONFIG_PATH), '.bridge-pause.json');

// persistPause() — durable kill-switch. Same pattern as persistConfig():
// escribir + fsync(fd) + close + rename + fsync del directorio padre.
// DEVUELVE true/false para que setPaused() y el handler HTTP puedan
// distinguir "pause aplicado y durable" de "pause solo en RAM".
//
// Key fix: the content is written with fs.writeSync(fd, ...) ON THE SAME
// fd that was opened, so fs.fsyncSync(fd) syncs the descriptor that
// actually wrote the data. The previous code used fs.writeFileSync(tmp, ...)
// (which opens/closes ANOTHER descriptor internally) and then did fsync on the fd
// original -> fsync on the wrong descriptor, with no durability guarantee.
//
// Additionally, after renameSync the parent directory is synced so the
// rename itself reaches persistent storage (second crash/power-loss window).
// Without this, the content could be durable but the rename remains unconfirmed.
// rename no confirmado.
function persistPause() {
  let fd = null;
  let tmp = null;
  try {
    // Unique temp name per write: pid+Date.now() does not guarantee
    // uniqueness across processes sharing pauseFile. randomBytes(16) makes
    // collision negligible. Precondition: one bridge per pauseFile.
    tmp = PAUSE_FILE + '.tmp.' + crypto.randomBytes(16).toString('hex');
    const data = Buffer.from(JSON.stringify({jitsi: !!PAUSED.jitsi, nostr: !!PAUSED.nostr}, null, 2) + '\n', 'utf8');
    fd = fs.openSync(tmp, 'w', 0o600);
    fs.writeSync(fd, data, 0, data.length, 0);
    fs.fsyncSync(fd);            // durabilidad del contenido (al almacenamiento persistente)
    fs.closeSync(fd);
    fd = null;
    fs.renameSync(tmp, PAUSE_FILE); // atomic on the same filesystem
    tmp = null;
    // fsync el directorio padre para que el rename sea durable.
    try {
      const dirFd = fs.openSync(path.dirname(PAUSE_FILE), 'r');
      try { fs.fsyncSync(dirFd); } finally { fs.closeSync(dirFd); }
    } catch (e) {
      // Fail-closed: el contrato de persistPause() es `true = pause
      // durable`. Si el fsync del directorio no se pudo realizar (FS que no
      // soporta sincronizar directorios: Windows, ciertos overlay), el rename
      // might not be durable on an immediate crash -> we CANNOT claim
      // durability. On Linux/ext4/XFS (normal bridge scenario) this
      // catch never runs.
      if (fd !== null) { try { fs.closeSync(fd); } catch (_) {} fd = null; }
      console.error('[bridge] fsync de directorio no disponible, pause NO durable:', e.message);
      return false;
    }
    return true;
  } catch (e) {
    if (fd !== null) { try { fs.closeSync(fd); } catch (_) {} fd = null; }
    if (tmp !== null) { try { fs.unlinkSync(tmp); } catch (_) {} tmp = null; }
    console.error('[bridge] error persistiendo pause:', e.message);
    return false;
  }
}

// Restore the persisted pause. Runs on every boot regardless of mode; the
// state file (runtime intent) wins over CONFIG.paused.
//
// Fail-closed policy (kill-switch): a *missing* file is a legitimate "never
// paused" signal and falls back to CONFIG.paused / legacy migration. But a
// file that *exists* and is unreadable/corrupt/malformed is an operational
// emergency: we cannot know whether the operator left a side paused, so we
// must NOT silently assume "not paused". Failure here aborts startup (fatal),
// which is the only safe option for a durable emergency switch.
function loadPause() {
  // Prefer the dedicated pause file (any mode). If absent, migrate from the
  // nostr state file (commit 19beb8c wrote `paused` inside .bridge-state.json)
  // so a pause persisted by that version is not lost.
  let exists = false;
  try { fs.accessSync(PAUSE_FILE, fs.constants.F_OK); exists = true; } catch (_) { /* ENOENT */ }
  if (exists) {
    // File present -> must be perfectly parseable, or we fail closed.
    let s;
    try { s = JSON.parse(fs.readFileSync(PAUSE_FILE, 'utf8')); }
    catch (e) {
      throw new Error('PAUSE_FILE corrupto: no se puede determinar el estado del kill-switch. ' +
        'El bridge NO arrancará hasta que se repare/elimine ' + PAUSE_FILE + ' (' + e.message + ')');
    }
    if (!s || typeof s !== 'object' ||
        !(typeof s.jitsi === 'boolean') || !(typeof s.nostr === 'boolean')) {
      throw new Error('PAUSE_FILE con esquema inválido (' + PAUSE_FILE + '): se esperaba {jitsi:boolean,nostr:boolean}. ' +
        'El bridge NO arrancará hasta que se repare/elimine el fichero.');
    }
    PAUSED.jitsi = s.jitsi;
    PAUSED.nostr = s.nostr;
    console.log('[bridge] pause restaurado del estado: jitsi=' + PAUSED.jitsi + ' nostr=' + PAUSED.nostr);
    return;
  }
  // File absent -> fall back to legacy migration from the nostr state file.
  let legacy = null;
  try {
    const raw = fs.readFileSync(STATE_FILE, 'utf8');
    const s = JSON.parse(raw);
    // The nostr state file is RELAY-SPECIFIC — only apply its `paused`
    // migration when the persisted relay matches the configured one, so
    // switching relay A -> B cannot leak A's pause into B. The dedicated
    // PAUSE_FILE is intentionally global (an emergency pause must survive a
    // relay change), so this guard only concerns the v1 legacy path.
    if (s && s.paused && typeof s.paused === 'object' && s.relay === CONFIG.nostr.relay) {
      legacy = s.paused;
    }
  } catch (e) {
    // Legacy absent or unreadable: nothing durable to restore. Only reachable
    // when PAUSE_FILE is absent; keep CONFIG.paused default.
    legacy = null;
  }
  if (legacy) {
    if (typeof legacy.jitsi === 'boolean') PAUSED.jitsi = legacy.jitsi;
    if (typeof legacy.nostr === 'boolean') PAUSED.nostr = legacy.nostr;
    console.log('[bridge] pause migrado del .bridge-state.json (legacy): jitsi=' + PAUSED.jitsi + ' nostr=' + PAUSED.nostr);
    // The migration must be durable: if we cannot create the new file
    // the migration only lived in RAM and the pause would be lost on the
    // siguiente reinicio. Esto es un fallo de arranque (FAIL-CLOSED), NO se
    // traga: el kill-switch no puede quedar solo en RAM.
    if (!persistPause()) {
      throw new Error('no se pudo migrar el kill-switch legacy de forma durable (' + PAUSE_FILE + '): ' +
        'arranque abortado para no perder el estado pausado');
    }
  }
}

function isPaused(side) {
  if (side === 'both') return PAUSED.jitsi || PAUSED.nostr;
  return !!PAUSED[side];
}

// Apply a pause/resume command durably and atomically.
//
// The emergency behavior of `true` and `false` is intentionally NOT symmetric
// (fail-closed in both directions):
//
//   pause=true  (activar kill-switch) -> mutar RAM primero, luego persistir.
//     If persistence fails, we THROW but RAM stayed paused: the bridge
//     sigue efectivamente parado (fail-closed) y la persistencia es un problema
//     which the operator will see fixed after the crash. Never answer ok:true
//     sin durabilidad.
//
//   pause=false (reanudar)           -> persistir PRIMERO, y solo publicar el
//     cambio en RAM si el disco lo confirma. Si la persistencia falla, la RAM
//     permanece pausada (no se reanuda un kill-switch que no es durable): el
//     estado en disco (pausado) y en RAM quedan consistentes.
//
// Con este orden no hay ventana en la que RAM y disco se contradigan:
// * activar fallido -> RAM pausada + disco (posible) viejo; nunca reanuda
// * reanudar fallido -> RAM pausada + disco pausado; consistente
//
// persistPause() is synchronous and atomic (temp + fsync + rename + fsync(dir)),
// por lo que no hay intercalado entre llamadas HTTP del mismo proceso.
// Documented precondition: one bridge per PAUSE_FILE (multi-instance
// sharing the file is not supported: the last writer overwrites the other).
function setPaused(side, val) {
  const v = !!val;
  if (side !== 'jitsi' && side !== 'nostr' && side !== 'both') throw new Error('invalid side: ' + side + ' (jitsi|nostr|both)');

  const target = {};
  if (side === 'both') { target.jitsi = v; target.nostr = v; }
  else { target[side] = v; }

  if (v) {
    // Activating: apply to RAM (fail-closed), then persist.
    if (side === 'both') { PAUSED.jitsi = true; PAUSED.nostr = true; }
    else PAUSED[side] = true;
    if (!persistPause()) {
      throw new Error('no se pudo persistir el pause de forma durable (kill-switch solo en RAM, pero aplicado)');
    }
  } else {
    // Resuming: persist first; only mutate RAM if the disk confirms it.
    const prevJ = PAUSED.jitsi, prevN = PAUSED.nostr;
    if (side === 'both') { PAUSED.jitsi = false; PAUSED.nostr = false; }
    else PAUSED[side] = false;
    if (!persistPause()) {
      // Fallback fail-closed: revert to the original persisted state in RAM.
      PAUSED.jitsi = prevJ; PAUSED.nostr = prevN;
      throw new Error('no se pudo reanudar de forma durable; el kill-switch permanece activo');
    }
  }
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
const REJECTED_IDS_MAX = 200;   // LOW-8: cap del caché de frames rechazados (anti-reintento efímero)
// M-04/M-05: incoming Nostr pipeline limits (before unwrapEvent()).
// Kind 1059 is a tiny gift-wrap by design ({kind,tags,content,pubkey,…});
// an oversized frame indicates abuse/anomaly and must not enter the crypto
// (unwrapEvent) without a cap, so CPU/memory are not burned before discarding.
const NOSTR_MAX_FRAME_BYTES = CONFIG.nostrMaxFrameBytes || (64 * 1024);  // 64KB por frame (mismo tope que el HTTP MAX_BODY)
const NOSTR_MAX_CONCURRENCY = CONFIG.nostrMaxConcurrency || 4;           // simultaneous unwraps
const NOSTR_MAX_QUEUE = CONFIG.nostrMaxQueue || 32;                      // max queued (backpressure)
const DROPPED_MAX = 5000;                                                // persistent dropped-ledger cap
// AUDIT-2 (MEDIUM): the `delivery` ledger must be bounded. Each entry is
// written with a synchronous fsync (markDelivery → flushStateNow), so a unique id
// per incoming event with no TTL/cap turns durability into an I/O DoS
// (state → JSON → ever-growing disk). Explicit policy:
//   - delivered: NOT wall-clock-expired. Kept until the
//     recovery cursor (lastSeen) proves the relay can no longer
//     re-deliver that id within the replay window (watermark). A
//     long downtime freezes lastSeen -> delivered does not expire -> no replay.
//     Ver deliveredSafeToExpire().
//   - pending:   wall-clock-expired (PENDING_TTL_SECS, generous and
//     configurable): a pending that never finishes indicates a crash mid-
//     operation; the relay re-delivers it on resume, so there is no
//     risk of loss or replay (the guarantee differs from delivered).
//   - hard cap:  DELIVERY_MAX bounds state size (I/O). NEVER
//     evict a delivered still inside its watermark window to
//     make room: if the ledger is full of immature delivered, it
//     applies backpressure (admits no more) instead of losing exactly-once.
// Lazy sweep in markDelivery (not on the fsync hot path): the object is swept
// only when the cap is exceeded OR on insert, avoiding costly scans
// on normal events.
const PENDING_TTL_SECS = CONFIG.pendingTtlSecs || (24 * 3600); // wall-clock; pending crash deja retry vía relay
const DELIVERED_WATERMARK_MARGIN_SECS = 120; // extra margin over STATE_OVERLAP_SECS
// AUDIT-M01-OPTION2-FIX (🔴 BLOCKING kaieriksen): MAXIMUM step a
// single processed event may advance recoveryWatermark. The watermark must
// represent the range the relay HAS CONFIRMED as traversed, NOT the local
// clock. A free jump to Date.now() after months of downtime expires delivered
// the relay has not yet proven to have traversed (break exactly-once). This
// step bounds each advance to a short confirmed-backlog window: after a
// downtime, the watermark advances incrementally as the relay
// re-delivers the backlog, never all at once. Pick a multiple of the overlap so
// it stays consistent with the replay overlap (STATE_OVERLAP_SECS).
const RECOVERY_WATERMARK_STEP_SECS = CONFIG.recoveryWatermarkStepSecs || 300; // 5 min por evento procesado
const DELIVERY_MAX = 10000;             // hard cap of the durable delivery ledger
const DELIVERY_SOFT_LIMIT = Math.floor(DELIVERY_MAX * 0.9); // AUDIT-6: umbral de limpieza agresiva + re-scan
let backpressureRejected = 0;           // AUDIT-5: rejected-admission counter (fail-closed)
const nostrQueue = [];          // gift-wraps waiting to be processed
let nostrInflight = 0;          // gift-wraps mid-handleIncomingGiftWrap
// Enqueue + dispatch with a concurrency limit (M-05). If the queue is full,
// the gift-wrap is dropped (hard backpressure): the relay re-delivers it on the
// next since-overlap; it is not lost irrecoverably.
function enqueueGiftWrap(gw) {
  if (nostrQueue.length >= NOSTR_MAX_QUEUE) {
    // H-NEW-01 (ALTO): dropping under backpressure must stay RECOVERABLE.
    // The relay only re-delivers events within the next subscription's
    // `since` window. If we drop this event and let `lastSeen` keep advancing
    // (from events that DID enter the queue), a sustained saturating burst
    // would push the cursor past what the fixed overlap can recover, losing
    // the dropped event PERMANENTLY. We record the oldest point with
    // possible unacknowledged drops so the next `since` never passes it.
    //
    // ALTO-2 (audit 462e62b): dropping must additionally be tracked in a
    // PERSISTENT ledger of dropped ids, and `pendingSince` must stay STICKY
    // until those drops are actually recovered (seen) in a later
    // subscription — not merely until the local queue drains. Draining the
    // queue ≠ the dropped events were processed.
    if (gw && gw.id) recordDropped(gw.id, gw.created_at);
    const nowTs = Math.floor(Date.now() / 1000);
    if (bridgeState && (bridgeState.pendingSince == null || nowTs < bridgeState.pendingSince)) {
      bridgeState.pendingSince = nowTs;
      markStateDirty();
    }
    console.warn('[nostr] cola de procesamiento llena (' + NOSTR_MAX_QUEUE + '): gift-wrap descartado (backpressure, recuperable en reconexión)');
    return false;
  }
  nostrQueue.push(gw);
  pumpNostrQueue();
  return true;
}
function pumpNostrQueue() {
  while (nostrInflight < NOSTR_MAX_CONCURRENCY && nostrQueue.length > 0) {
    const gw = nostrQueue.shift();
    nostrInflight++;
    handleIncomingGiftWrap(gw)
      .catch(err => console.error('[nostr] error procesando gift-wrap:', err && err.message))
      .finally(() => { nostrInflight--; pumpNostrQueue(); });
  }
  // ALTO-2 (audit 462e62b): `pendingSince` is STICKY. It is NOT cleared just
  // because the local queue drained — dropping it here would let `since`
  // advance past dropped events that were never recovered, permanently
  // losing them. It is released only once the dropped-id ledger is empty
  // (i.e. every dropped id has been seen again in a later subscription).
  releasePendingSinceIfRecovered();
}
let bridgeState = null;         // {relay, lastSeen, seenIds[], antiloop?} | null
// 🟡 LOW (AUDIT-13): incremental counter for the delivery-ledger size.
// Avoids Object.keys(bridgeState.delivery).length repeated on hot paths
// (markDelivery, rescan-progress measurement), which is O(n) per call and
// an authorized attacker could amplify by filling the ledger (DELIVERY_MAX=10000).
// It stays in sync with EVERY ledger mutation (insert in
// markDelivery, deletions in evictDeliveryLedger and finishDelivery rejected).
let deliverySize = 0;
let stateDirty = false;
let stateTimer = null;
// LOW-8 (audit 462e62b): cache of REJECTED frame IDs (e.g. too large).
// Separate from seenIds[] (dedup of PROCESSABLE events) so an attacker
// cannot inject arbitrary IDs and poison/degrade the legitimate dedup.
// It is ephemeral (not persisted): it only prevents infinite retries of the
// relay towards the same frame; if lost on restart, the frame will be
// rejected again by size on retry. Entries {id, ts} with their own cap/TTL.
let rejectedIds = [];

// H-04: persist the anti-loop admission identity + rate/ride state so a
// process crash + restart cannot re-deliver a DM that was already
// admitted/committed. Only the CHEAP, delivery-critical pieces are
// persisted: nextAdmissionId (monotonic identity, F2-R03/R04), requests,
// pairs and pairHours (identity by id). The `hashes` maps (canon+shingles)
// are intentionally left volatile: losing them can only re-allow a fuzzy
// dedup, never re-deliver a committed DM.
function serializeAntiloop() {
  const reqs = {};
  for (const [rid, r] of ANTILOOP.requests) {
    reqs[rid] = {
      count: r.count, first: r.first, last: r.last, tripped: !!r.tripped,
      agents: Array.from(r.agents || []),
      edges: Array.from((r.edges || new Map()).entries())
    };
  }
  const pairs = {};
  for (const [k, arr] of ANTILOOP.pairs) pairs[k] = arr;
  const pairHours = {};
  for (const [k, arr] of ANTILOOP.pairHours) pairHours[k] = arr;
  return {
    nextAdmissionId: ANTILOOP.nextAdmissionId,
    requests: reqs, pairs: pairs, pairHours: pairHours
  };
}

function deserializeAntiloop(p) {
  if (!p || typeof p !== 'object') return;
  if (Number.isFinite(p.nextAdmissionId) && p.nextAdmissionId >= ANTILOOP.nextAdmissionId) {
    ANTILOOP.nextAdmissionId = p.nextAdmissionId;
  }
  if (p.requests && typeof p.requests === 'object') {
    for (const [rid, r] of Object.entries(p.requests)) {
      if (!rid || !r) continue;
      ANTILOOP.requests.set(rid, {
        count: r.count || 0, first: r.first || 0, last: r.last || 0, tripped: !!r.tripped,
        agents: new Set(Array.isArray(r.agents) ? r.agents : []),
        edges: new Map(Array.isArray(r.edges) ? r.edges : [])
      });
    }
  }
  if (p.pairs && typeof p.pairs === 'object') {
    for (const [k, arr] of Object.entries(p.pairs)) {
      if (Array.isArray(arr)) ANTILOOP.pairs.set(k, arr);
    }
  }
  if (p.pairHours && typeof p.pairHours === 'object') {
    for (const [k, arr] of Object.entries(p.pairHours)) {
      if (Array.isArray(arr)) ANTILOOP.pairHours.set(k, arr);
    }
  }
  if (ANTILOOP.requests.size || ANTILOOP.pairs.size || ANTILOOP.pairHours.size) {
    console.log('[antiloop] estado restaurado: ' + ANTILOOP.requests.size + ' requests, ' +
      ANTILOOP.pairs.size + ' pairs, ' + ANTILOOP.pairHours.size + ' pairHours, nextAdmissionId=' + ANTILOOP.nextAdmissionId);
  }
}

function loadState() {
  try {
    const raw = fs.readFileSync(STATE_FILE, 'utf8');
    const s = JSON.parse(raw);
    if (s && typeof s.relay === 'string' && typeof s.lastSeen === 'number' && s.lastSeen > 0) {
      // H-NEW-02 migration: legacy seenIds were plain strings; new format is
      // [{id, ts}]. Normalize old-format entries to {id, ts: 0} so isSeen()
      // falls through to the legacy includes() check if needed.
      const raw = Array.isArray(s.seenIds) ? s.seenIds : [];
      const seenIds = raw.map(e => (typeof e === 'string' ? {id: e, ts: 0} : e));
      bridgeState = {relay: s.relay, lastSeen: s.lastSeen, seenIds, antiloop: s.antiloop || null, pendingSince: (typeof s.pendingSince === 'number' ? s.pendingSince : null), dropped: Array.isArray(s.dropped) ? s.dropped.filter(d => d && d.id) : [], droppedOverflow: !!s.droppedOverflow, recoveryWatermark: (typeof s.recoveryWatermark === 'number' ? s.recoveryWatermark : s.lastSeen), delivery: (s.delivery && typeof s.delivery === 'object') ? s.delivery : {}};
      // AUDIT-13: contador incremental — inicializar al cargar estado (tras
      // restart, without this deliverySize would stay 0 and markDelivery would use a
      // wrong size for the soft-limit/cap).
      deliverySize = bridgeState.delivery ? Object.keys(bridgeState.delivery).length : 0;
      console.log('[nostr] previous state:', bridgeState.relay, 'last event', new Date(bridgeState.lastSeen * 1000).toISOString(), '(' + bridgeState.seenIds.length + ' ids seen)');
      // NOTE: deserializeAntiloop(s.antiloop) is NOT called here — ANTILOOP
      // (a const) is not initialized yet at this point in module init (TDZ).
      // The anti-loop state is restored right after `const ANTILOOP` is defined.
    } else {
      // LOW-9: valid JSON but UNEXPECTED state shape (no valid relay/lastSeen).
      // We cannot trust what was processed either -> fail-closed.
      throw new SyntaxError('forma de estado inesperada (falta relay/lastSeen válidos)');
    }
  } catch (e) {
    // LOW-9 (audit 462e62b): tell the cases apart. Before, ENOENT / JSON
    // corrupt / EACCES fell together into "full backlog", which could cause
    // silent REPLAY (re-processing already-delivered DMs) if the file existed
    // but was corrupted or unreadable.
    if (e && e.code === 'ENOENT') {
      // First boot with no state file: full backlog is legitimate.
      console.log('[nostr] sin archivo de estado previo: subscription de backlog completo.');
      return;
    }
    // Anything else (corrupt JSON, EACCES/EPERM, EIO, invalid shape):
    // we do NOT know what was processed -> fail-closed. Aborting avoids re-delivering or
    // losing DMs; the operator can inspect/remove .bridge-state.json
    // (or the backup) and boot clean with explicit intent.
    const motivo = e && e.code
      ? e.code + ': ' + e.message
      : (e && e.message ? e.message : String(e));
    console.error('[nostr] ERROR FATAL loading state file (' + STATE_FILE + '): ' + motivo);
    console.error('[nostr] ' + (e && e.name === 'SyntaxError'
      ? 'Estado corrupto (JSON inválido). Mueve/elimina el archivo para arrancar limpio.'
      : 'No puedo confiar en el estado previo. Abortando para evitar replay/duplicación de DMs (fail-closed).'));
    process.exit(1);
  }
}

function markSeen(id) {
  if (!bridgeState || !id) return;
  if (!bridgeState.seenIds) bridgeState.seenIds = [];
  const now = Math.floor(Date.now() / 1000);
  // H-NEW-02 (MEDIO): purge by TIME, not by a fixed count, so the seen-
  // buffer always covers the full STATE_OVERLAP_SECS window regardless of
  // traffic rate. With a plain count cap, >200 events in <120s would evict
  // an id that is still inside the overlap, and a reconnect would re-deliver
  // (and re-process) it. Entries older than the overlap are dropped; the
  // SEEN_IDS_MAX cap stays only as a hard safety ceiling for extreme bursts.
  if (bridgeState.seenIds.length > 0) {
    bridgeState.seenIds = bridgeState.seenIds.filter(e => e && e.ts && (now - e.ts) < STATE_OVERLAP_SECS + 60);
  }
  // ALTO-2: seeing an id means a previously-dropped instance of it (tracked
  // in the ledger) is considered recovered -> remove it from dropped[] and
  // re-evaluate the sticky pendingSince marker. AUDIT-2: also clears the
  // droppedOverflow flag once the survivor ledger drains (see recoverDropped).
  if (id && bridgeState.dropped && bridgeState.dropped.some(d => d && d.id === id)) {
    recoverDropped(id);
    releasePendingSinceIfRecovered();
  }
  if (bridgeState.seenIds.some(e => e && e.id === id)) return; // already recent
  bridgeState.seenIds.unshift({id, ts: now});
  if (bridgeState.seenIds.length > SEEN_IDS_MAX) bridgeState.seenIds.length = SEEN_IDS_MAX;
  markStateDirty();
}

// ALTO-2: persistent ledger of gift-wraps dropped under backpressure. Kept
// in bridgeState so a crash does not lose track of what still needs to be
// recovered. Bounded by DROPPED_MAX (older entries evicted by time too).
//
// AUDIT-2 (MEDIO): truncating the ledger WITHOUT tracking the overflow breaks
// the recovery guarantee. If > DROPPED_MAX distinct wraps are dropped in a
// burst, the oldest entries are evicted from the ledger, but the since-anchor
// (pendingSince) only released once `dropped` is empty. Those evicted ids are
// gone from the ledger AND never recovered, yet the anchor would still be
// released when the (smaller) survivor list drains — losing D1..Dk silently.
// Fix: a persistent `droppedOverflow` flag is set whenever the ledger truncates.
// While set, releasePendingSinceIfRecovered() MUST NOT drop the anchor, so the
// relay keeps re-delivering by RANGE (cursor watermark), not per-id, which
// covers even the evicted drops. The flag clears once the survivor set has
// fully drained AND the cursor has safely advanced past the overflow window.
// `createdAt` (opcional): el created_at REAL del evento descartado, si se
// conoce en el punto del drop. AUDIT-16 (🔴 ALTO): el cursor de Nostr es
// temporal y pendingSince no debe anclarse solo a la hora LOCAL de rechazo
// (Date.now) but also the event's created_at, which may be earlier
// (backlog): a late wrap with created_at=1000 rejected at instant
// t=5000 is only re-reachable if the next subscription's `since` covers
// >= created_at. We store that created_at in the entry so (a)
// pendingSince = min(pendingSince, dropped.created_at) and (b) allow
// point recovery by id in subscribeIncoming (see fetchDroppedByIds).
function recordDropped(id, createdAt) {
  if (!bridgeState || !id) return;
  if (!bridgeState.dropped) bridgeState.dropped = [];
  if (bridgeState.dropped.some(d => d && d.id === id)) return; // already tracked
  const entry = {id, ts: Math.floor(Date.now() / 1000)};
  // If the event's created_at is a valid timestamp and earlier than the local
  // time, we keep it: the range [created_at, ...] is what the relay
  // must re-deliver, not the local reception time (which may be far
  // ahead for a wrap that arrived late due to backlog).
  if (typeof createdAt === 'number' && createdAt > 0) {
    entry.created_at = createdAt;
    if (bridgeState.pendingSince == null || createdAt < bridgeState.pendingSince) {
      bridgeState.pendingSince = createdAt;
      markStateDirty();
    }
  }
  bridgeState.dropped.unshift(entry);
  if (bridgeState.dropped.length > DROPPED_MAX) {
    // AUDIT-2: the ledger overflowed — the evicted (oldest) ids are no longer
    // individually tracked. Flag it so the range-anchor holds until recovery.
    bridgeState.dropped.length = DROPPED_MAX;
    bridgeState.droppedOverflow = true;
    markStateDirty();
  }
  markStateDirty();
}

// ALTO-2: `pendingSince` is sticky and is only released once the dropped-id
// ledger is empty (every dropped id has been recovered/seen again). If the
// ledger is non-empty we keep anchoring `since` so the relay re-delivers the
// dropped events. This MUST NOT be called simply because the queue drained.
function releasePendingSinceIfRecovered() {
  if (!bridgeState || bridgeState.pendingSince == null) return;
  // AUDIT-2: while the ledger has overflowed, per-id recovery is incomplete —
  // the evicted drops are only recoverable by RANGE. Hold the anchor until the
  // surviving set drains AND the overflow flag clears (see recoverDropped).
  if (bridgeState.dropped && bridgeState.dropped.length > 0) return; // still pending recovery
  if (bridgeState.droppedOverflow) return; // overflow: evicted ids need range recovery
  bridgeState.pendingSince = null;
  markStateDirty();
}

// AUDIT-2: called when a drop id is seen again (recovered). Clears the
// persistent overflow flag once the survivor ledger has fully drained; the
// range anchor (pendingSince, held by releasePendingSinceIfRecovered while
// droppedOverflow) guarantees the evicted ids were re-delivered by RANGE
// across the previous subscriptions before we consider recovery complete.
function recoverDropped(id) {
  if (!bridgeState) return;
  if (id && bridgeState.dropped && bridgeState.dropped.some(d => d && d.id === id)) {
    bridgeState.dropped = bridgeState.dropped.filter(d => d && d.id !== id);
  }
  if (bridgeState.droppedOverflow && (!bridgeState.dropped || bridgeState.dropped.length === 0)) {
    bridgeState.droppedOverflow = false;
    markStateDirty();
  }
}

function isSeen(id) {
  if (!bridgeState || !id || !bridgeState.seenIds) return false;
  // Time-based: an id is only 'seen' if still within the overlap window.
  const now = Math.floor(Date.now() / 1000);
  for (const e of bridgeState.seenIds) {
    if (e && e.id === id && e.ts && (now - e.ts) < STATE_OVERLAP_SECS + 60) return true;
  }
  return bridgeState.seenIds.includes(id); // legacy plain-string fallback
}

// LOW-8: ephemeral cache of REJECTED frame IDs (by size, etc.). Does not
// touch seenIds[] (dedup of processable events). With its own cap/TTL.
function isRejected(id) {
  if (!id || !rejectedIds) return false;
  const now = Math.floor(Date.now() / 1000);
  return rejectedIds.some(e => e && e.id === id && e.ts && (now - e.ts) < STATE_OVERLAP_SECS + 60);
}
function markRejected(id) {
  if (!id) return;
  if (isRejected(id)) return; // already rejected recently
  const now = Math.floor(Date.now() / 1000);
  rejectedIds.unshift({id, ts: now});
  if (rejectedIds.length > REJECTED_IDS_MAX) rejectedIds.length = REJECTED_IDS_MAX;
}

function flushState() {
  if (!stateDirty || !bridgeState) return;
  stateDirty = false;
  persistState();
}

// ALTO-3 (audit 462e62b): immediate, non-debounced durable write. Used for
// delivery-critical transitions (pending/delivered) so a crash on the 5s
// debounce timer cannot re-deliver a DM that was already committed, or lose
// one that had not yet been delivered.
function flushStateNow() {
  stateDirty = false; // clear pending flag: we're writing right now
  persistState();
}

function persistState() {
  if (!bridgeState) return;
  try {
    const tmp = STATE_FILE + '.tmp';
    // H-04: embed the persistable anti-loop state (identity + rates) so a
    // crash+restart does not re-deliver committed DMs.
    const payload = JSON.parse(JSON.stringify(bridgeState));
    payload.antiloop = serializeAntiloop();
    // Kill-switch durability: persist the per-side pause so it survives a
    // restart/reconnect (a paused side must not silently resume).
    payload.paused = {jitsi: !!PAUSED.jitsi, nostr: !!PAUSED.nostr};
    const fd = fs.openSync(tmp, 'w', 0o600);
    const data = Buffer.from(JSON.stringify(payload, null, 2) + '\n', 'utf8');
    fs.writeSync(fd, data, 0, data.length, 0);
    try {
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, STATE_FILE);
  } catch (e) {
    console.error('[nostr] error writing state:', e.message);
  }
}

// H-04: mark the bridge state dirty so the anti-loop admission/rollback
// identity is persisted debounced (same 5s timer as lastSeen/seenIds).
function markStateDirty() {
  stateDirty = true;
  if (!stateTimer) {
    stateTimer = setTimeout(() => { stateTimer = null; flushState(); }, STATE_FLUSH_MS);
  }
}

// ALTO-3 + MEDIO-4: durable delivery ledger. `bridgeState.delivery[id]` =
// {status: 'pending'|'delivered', ts}. Written with fsync immediately so a
// crash cannot re-deliver a committed DM (delivered) nor discard an
// un-delivered one (pending -> retry on restart).
//
// AUDIT-2 (MEDIO): the ledger is now BOUNDED. Without a TTL/cap, each unique
// gift-wrap id becomes a permanent entry written with a synchronous fsync
// (flushStateNow), so a flood of distinct ids grows the state file unbounded
// (I/O-amplification DoS). Eviction is LAZY (inside markDelivery, never on the
// hot path of fsync alone): `delivered` entries expire only by recovery
// watermark (never by wall clock), `pending` entries by PENDING_TTL_SECS, and
// the hard cap applies FAIL-CLOSED (no silent eviction of a still-relevant
// `delivered` to make room — see evictDeliveryLedger). A `pending` entry that
// outlives its TTL is treated as having crashed mid-operation: the wrap will
// be re-requested via

// the since-cursor on the next subscription, so no message is lost — the
// ledger only stops carrying the stale intent.
function nowSec() {
  return Math.floor(Date.now() / 1000);
}

// AUDIT-4 (SECURITY, auditor's option B): expiration by WATERMARK, not by
// wall clock. A `delivered` is safe to delete ONLY when the recovery cursor
// (lastSeen) already proves the relay cannot re-deliver it:
// the replay asks `since = cursor - STATE_OVERLAP_SECS`, so an event
// delivered at `ts` is already unreachable when lastSeen > ts + OVERLAP + margin.
// During a long downtime lastSeen does NOT advance -> delivered does NOT expire -> no
// replay after a >30 min outage. Unlike delivered, `pending` DOES
// expire by wall clock (PENDING_TTL_SECS): a pending that never finishes
// indicates a crash mid-operation, and the relay re-delivers it on
// resume (no replay risk as with delivered). The guarantee is
// distinta y por eso el TTL es distinto.
//
// Fail-closed cap (AUDIT-4, MEDIUM): DELIVERY_MAX bounds the state against the
// I/O DoS, but NEVER silently evicts a `delivered` still
// within its watermark window to make room — that would lose
// exactly-once. If the ledger is full of immature delivered, we prefer
// evicting first old pending and already-unreachable delivered; if it still
// does not fit and an immature delivered would have to be deleted, we do NOT
function deliveredCanExpire(e, lastEnd) {
  if (!e || !e.ts) return false;
  // A delivered with no recovery watermark (there was never confirmed
  // processing) can never be discarded.
  if (!lastEnd || lastEnd <= 0) return false;
  // lastEnd - (ts + OVERLAP + margin) >= 0  ->  the cursor already passed the window.
  return e.ts + STATE_OVERLAP_SECS + DELIVERED_WATERMARK_MARGIN_SECS < lastEnd;
}

// AUDIT-10 (root): separate the RECEPTION cursor (lastSeen) from the
// RECOVERY watermark (recoveryWatermark). HIGH-7 came from mixing them:
// updateLastSeen() advances lastSeen by reception time BEFORE the event is
// authenticated/admitted, so a burst of non-admitted events could inflate
// lastSeen and deliveredCanExpire() would consider delivered unreachable
// that the relay could still re-deliver (break exactly-once) or leave a
// legitimate L outside the overlap (loss).
//
// The only legitimate basis for deliveredCanExpire() is the recovery
// watermark: it ONLY advances when there is EVIDENCE the relay already
// traversed the range (successful markDelivery of a real event). A frame
// received but rejected/not-admitted does NOT move recoveryWatermark, no
// matter how much lastSeen advances. CRITICAL: the watermark is NEVER set to
// Date.now() of any internal call — a long downtime freezes the watermark and
// a single event CANNOT confirm the relay traversed all elapsed time
// (traversing months of backlog takes many messages, not one). Each processed
// event confirms at most one extra short backtrack window
// (RECOVERY_WATERMARK_STEP_SECS); the advance is incremental and never
// exceeds the local clock. The initial establishment (0 -> X) is also bounded
// to that step: a cold boot expires at most delivered older than
// (first + overlap + margin), never the entire fallen time.
function advanceRecoveryWatermark() {
  if (!bridgeState) return;
  const now = Math.floor(Date.now() / 1000);
  const prev = bridgeState.recoveryWatermark || 0;
  // Advance limit confirmed by this event: 1 backlog step, NEVER a free
  // jump to the local clock. prev + step, capped at `now` by local clock
  // (an event cannot confirm progress beyond the current moment).
  const target = Math.min(prev + RECOVERY_WATERMARK_STEP_SECS, now);
  // Only if we actually advanced (covers initial 0 -> step, and incremental
  // advances). Never goes backward (target >= prev by the sum).
  if (target > prev) {
    bridgeState.recoveryWatermark = target;
    markStateDirty();
  }
}

function evictDeliveryLedger(aggressive) {
  if (!bridgeState || !bridgeState.delivery) return 0;
  const d = bridgeState.delivery;
  const now = nowSec();
  // AUDIT-10: use the RECOVERY watermark (not lastSeen). A delivered is
  // only safe to delete when enough real traffic was PROCESSED to prove the
  // relay can no longer re-deliver it.
  const lastEnd = bridgeState.recoveryWatermark || 0;
  let changed = 0;
  for (const id of Object.keys(d)) {
    const e = d[id];
    if (!e) continue;
    if (e.status === 'delivered') {
      // delivered: only by watermark (safe to delete when the cursor can no longer
      // re-deliver it). Never by wall clock.
      if (deliveredCanExpire(e, lastEnd)) { delete d[id]; changed++; deliverySize--; }
    } else {
      // pending: by wall clock (crash mid-op; the relay retries).
      if (e.ts && (now - e.ts) >= PENDING_TTL_SECS) { delete d[id]; changed++; deliverySize--; }
    }
  }
  // Cap: evict the expirables first (old pending and already-unreachable delivered)
  // via FIFO by ts (AUDIT-4). AUDIT-5: in this second phase ONLY
  // `pending` entries that ALREADY passed their TTL are removed (now-ts >= PENDING_TTL_SECS);
  // never a still-valid pending (would lose the durable evidence of an
  // in-flight operation). If there aren't enough expirables to drop below the cap,
  // NO valid pending nor immature delivered is deleted -> fail-closed:
  // admission keeps rejecting (backpressure) instead of losing exactly-once.
  const ids = Object.keys(d);
  if (ids.length > DELIVERY_MAX) {
    ids.sort((a, b) => {
      const ea = d[a], eb = d[b];
      return (ea && eb ? (ea.ts || 0) - (eb.ts || 0) : 0);
    });
    // Incremental counter instead of Object.keys(d).length inside the loop
    // (avoid O(n²) on large ledgers: Object.keys is O(n) and was called on every
    // iteration, hanging the process with >DELIVERY_MAX immature delivered).
    let size = ids.length;
    for (const id of ids) {
      const e = d[id];
      if (e && e.status === 'pending' && e.ts && (now - e.ts) >= PENDING_TTL_SECS) {
        delete d[id]; changed++; size--; deliverySize--;
      }
      if (size <= DELIVERY_MAX) break;
    }
    // If after purging expired pending it is still above the cap, we do NOT evict
    // immature delivered nor valid pending (fail-closed; backpressure below).
    // (fail-closed: backpressure is accepted below, no silent loss.)
  }
  return changed;
}

// AUDIT-6 (MEDIUM): operational escape mechanism against a PERMANENT admission
// lock. If the ledger reaches DELIVERY_MAX full of watermark-protected delivered
// entries (lastSeen stalled by a recovery/backpressure condition),
// markDelivery of a new pending would return false forever -> admission DoS even
// though there is NO duplicate message or loss. This flag activates when normal
// cleanup does not free enough space and we are in backpressure, requesting a
// re-scan/retry so the cursor advances and frees already-unreachable delivered.
// libere delivered ya inalcanzables.
// AUDIT-8 (MEDIUM): the recovery rescan must NOT become a reconnection loop
// against the relay/process. If the ledger stays full (lastSeen does not advance)
// and every rejected event re-invokes requestDeliveryRescan(), we would get
// connect/close/connect/close... indefinitely. When hardening AUDIT-7
// (recordDropped + sticky pendingSince on the fail-closed rejection) the anchor
// is not released until the drop is recovered -> if the ledger does not empty,
// the rescan is re-requested on every event -> reconnection DoS.
// Fix: exponential backoff + rescan limit per time window.
//   RESCAN_MIN_INTERVAL_MS : minimum separation between rescans (respect the
//     WS natural reconnection cycle, which already has its own 5s).
//   RESCAN_MAX_BACKOFF_MS  : ceiling of the exponential backoff.
//   RESCAN_MAX_PER_MINUTE  : hard limit of rescans per 60s window.
// Backoff is applied by resistance: each failed retry (that frees no space)
// doubles the wait up to the ceiling, and the window counter prevents bursts.
// The NATURAL WS reconnection (onclose -> 5s) does NOT go through here and is
// not limited by these constants.
const RESCAN_MIN_INTERVAL_MS = CONFIG.rescanMinIntervalMs || 5000;
const RESCAN_MAX_BACKOFF_MS = CONFIG.rescanMaxBackoffMs || 60000;
const RESCAN_MAX_PER_MINUTE = CONFIG.rescanMaxPerMinute || 6;
// AUDIT-9 (MEDIO): el rescan debe DEMOSTRAR progreso. Un re-scan ciego que
// reconecta sin liberar el ledger (lastSeen no avanza / delivery no decrece)
// insiste indefinidamente sobre algo que no funciona. Tras N rescans
// consecutivos SIN progreso real, entramos en estado BACKPRESSURE
// (rescanStalled=true): se suprime TODO rescan hasta que markDelivery / un
// evento admitido demuestre avance real o expire un respiro de ventana.
const RESCAN_MAX_STALLED = CONFIG.rescanMaxStalled || 3;   // rescans fallidos consecutivos -> BACKPRESSURE
const RESCAN_STALL_COOLDOWN_MS = CONFIG.rescanStallCooldownMs || (60 * 1000); // respiro antes de reintentar tras estancarse
let rescanStalled = false;          // BACKPRESSURE explícito: sin progreso, no más rescans
let rescanStalledSince = 0;         // ms epoch en que se activó el estancamiento (para el respiro)
let deliveryRescanNeeded = false;
let deliveryRescanScheduled = false;
let rescanAttempts = 0;             // ráfaga actual (backoff exponencial)
let rescanWindowStart = 0;          // inicio de la ventana de 60s (ms epoch)
let rescanWindowCount = 0;          // rescans dentro de la ventana actual
let lastRescanAt = 0;              // ms epoch del último rescan emitido
function rescanBackoffMs() {
  // Exponencial: min-interval * 2^attempts, capado al techo.
  const attempts = Math.min(rescanAttempts, 30); // acota 2^attempts
  return Math.min(RESCAN_MIN_INTERVAL_MS * Math.pow(2, attempts), RESCAN_MAX_BACKOFF_MS);
}
// Delegado de reconexion de la suscripcion entrante. Lo registra
// subscribeIncoming() al iniciar (y la reconexion es idempotente: abre un
// nuevo WebSocket cuyo onclose vuelve a llamar subscribeIncoming). Permite a
// requestDeliveryRescan() disparar un re-scan sin acoplarse a la closure JITSI.
let reconnectIncoming = null;

// Solicita un re-scan controlado de la suscripcion nostr. Cuando el ledger
// esta lleno de delivered inmaduros (lastSeen congelado) y no se libera espacio
// con la limpieza perezosa, forzamos una reconexion con el cursor anclado en
// pendingSince para que el relay re-entregue los drops y lastSeen avance ->
// delivered ya inalcanzables se liberan. Se programa una sola vez (guard)
// para evitar bucles de reconexion; el siguiente ciclo natural la rearma.
function requestDeliveryRescan() {
  deliveryRescanNeeded = true;
  if (deliveryRescanScheduled) return; // a rescan is already in progress; the
  // request is recorded in deliveryRescanNeeded and consumed on emission.

  // AUDIT-9 (MEDIUM): explicit BACKPRESSURE state. If the rescan did not show
  // real progress (see requestDeliveryRescanProgress), do NOT emit more rescans
  // until the cooldown breather expires. The NATURAL WS reconnection
  // (onclose -> 5s) does NOT go through here and stays operational.
  const nowMs = Date.now();
  if (rescanStalled) {
    if (rescanStalledSince !== 0 && nowMs - rescanStalledSince < RESCAN_STALL_COOLDOWN_MS) {
      console.warn('[nostr] rescan suprimido por BACKPRESSURE (sin progreso real): reintento en ' +
        Math.ceil((RESCAN_STALL_COOLDOWN_MS - (nowMs - rescanStalledSince)) / 1000) + 's');
      return;
    }
    // Breather fulfilled: we allow one retry (the next cycle reassesses it).
    rescanStalled = false;
    rescanStalledSince = 0;
    rescanAttempts = 0;
  }

  // AUDIT-8: per-minute window reset.
  const now = Date.now();
  if (rescanWindowStart === 0 || now - rescanWindowStart >= 60000) {
    rescanWindowStart = now;
    rescanWindowCount = 0;
    rescanAttempts = 0;
  }

  // If we already used this window's rescans, do NOT schedule more: we stay
  // `deliveryRescanNeeded = true` but without a loop. The next natural cycle
  // (WS reconnection or an event that admits and frees space) will re-arm it.
  if (rescanWindowCount >= RESCAN_MAX_PER_MINUTE) {
    console.warn('[nostr] rescan suprimido: techo de ' + RESCAN_MAX_PER_MINUTE + ' rescans/min alcanzado (el ledger sigue lleno), se reintenta en el proximo ciclo');
    return;
  }

  // Exponential backoff after bursts: if the retry does NOT free space and
  // we ask again, the wait lengthens up to the ceiling. The first rescan
  // after a breather of >= maxBackoff returns to min-interval.
  const sinceLast = now - lastRescanAt;
  const waitMs = (lastRescanAt === 0 || sinceLast >= RESCAN_MAX_BACKOFF_MS)
    ? RESCAN_MIN_INTERVAL_MS            // window start: minimum wait
    : rescanBackoffMs();                 // in burst: exponential

  deliveryRescanScheduled = true;
  rescanAttempts++;
  rescanWindowCount++;
  lastRescanAt = now;
  // Diferido para no interferir con el handler en curso (evita recursion rara
  // con markDelivery -> reconnect -> markDelivery). En waitMs se cierra la
  // conexion de suscripcion; su onclose vuelve a llamar subscribeIncoming(),
  // que recalcula el cursor (anclado a pendingSince si hay drops) y re-scanea.
  // AUDIT-9 (MEDIO): registrar el estado ANTES de emitir, para poder medir
  // progress after reconnection: recoveryWatermark, ledger size and the
  // conjunto de drops pendientes.
  const beforeWatermark = bridgeState ? (bridgeState.recoveryWatermark || 0) : 0;
  const beforeDeliveryCount = bridgeState ? deliverySize : 0;
  const beforeDropped = new Set((bridgeState && bridgeState.dropped)
    ? bridgeState.dropped.map(d => d && d.id) : []);

  setTimeout(() => {
    deliveryRescanScheduled = false;
    // AUDIT-8: emit the rescan. If requests are still pending (burst) and
    // there is still window budget, the next call will reschedule with
    // backoff — it NEVER reconnects in an unbounded loop. When the rescans/min
    // ceiling is reached, requestDeliveryRescan() suppresses the next one and
    // leaves deliveryRescanNeeded=true for the next natural cycle.
    deliveryRescanNeeded = false;
    // AUDIT-9 (MEDIUM): trigger the reconnection if a subscription is started;
    // progress measurement ALWAYS runs (even without reconnectIncoming,
    // e.g. in tests or if the WS is not started) so we can detect that the
    // rescan achieved nothing and enter BACKPRESSURE.
    let reconnected = false;
    if (typeof reconnectIncoming === 'function') {
      reconnectIncoming();
      reconnected = true;
    } else {
      console.warn('[nostr] re-scan solicitado pero la conexion de suscripcion no esta iniciada');
    }
    // AUDIT-9 (MEDIUM): the subscription restart processes the backlog
    // asynchronously (onmessage -> enqueue/markDelivery). Real progress is
    // measured a few seconds later (room for the relay to re-deliver
    // and free unreachable delivered).
    // AUDIT-12 (🟡 MEDIUM): the progress criterion does NOT use pendingSince by
    // itself — pendingSince changes when ANOTHER drop is recorded, without
    // recovering anything (delays detection of a truly stalled rescan). Real
    //   - recoveryWatermark advanced (the relay traversed new range), OR
    //   - the ledger freed entries (expired delivered / purged pending), OR
    //   - a CONCRETE dropped was re-admitted via markDelivery (exists in delivery).
    // lastSeen (raw reception) does not count either: it proves no recovery.
    //
    // 🟠 MEDIUM (AUDIT-17): an ID that DISAPPEARS from `dropped` is NOT progress
    // by itself — it may leave by pruning/overflow/cleanup without being
    // processed. The rescan measures RECOVERY: that the ID entered `delivery`
    // (markDelivery), not just that it no longer is in the drops ledger (avoids
    // false progress positives that delay the BACKPRESSURE).
    setTimeout(() => {
      const afterWatermark = bridgeState ? (bridgeState.recoveryWatermark || 0) : 0;
      const afterDeliveryCount = bridgeState ? deliverySize : 0;
      const delivery = (bridgeState && bridgeState.delivery)
        ? bridgeState.delivery : {};
      // AUDIT-17 (🟠 MEDIUM): it is NOT enough that the ID left `dropped` (that
      // also happens with pruning/overflow/cleanup without processing the
      // message). Relocation progress = a CONCRETE dropped came BACK INTO
      // `delivery` via markDelivery (re-admission) / recoverDropped, which
      // proves real processing. We used to use `afterDropped` (later set) and
      // an ID disappearing through any of those unrelated paths gave a false
      // positive of progress.
      let droppedRecovered = false;
      for (const id of beforeDropped) {
        if (Object.prototype.hasOwnProperty.call(delivery, id)
            && delivery[id] && delivery[id].status !== 'dropped') {
          // The previous dropped is now in `delivery` with an admitted status
          // (delivered/pending), not merely absent from the drops ledger.
          droppedRecovered = true;
          break;
        }
      }
      const progressed = (afterWatermark > beforeWatermark)
        || (afterDeliveryCount < beforeDeliveryCount)
        || droppedRecovered;
      if (!progressed) {
        rescanAttempts++;
        if (rescanAttempts >= RESCAN_MAX_STALLED) {
          rescanStalled = true;
          rescanStalledSince = Date.now();
          console.warn('[nostr] rescan sin progreso real tras ' + rescanAttempts +
            ' intentos; entrando en BACKPRESSURE (' + RESCAN_STALL_COOLDOWN_MS + 'ms cooldown)' +
            (reconnected ? '' : ' (sin reconexion disponible)'));
        }
      } else {
        // There was progress: reset the stall burst.
        rescanAttempts = 0;
      }
    }, RESCAN_MIN_INTERVAL_MS * 2); // ~2x the minimum: time to process the backlog
  }, waitMs);
}

function markDelivery(id, status, createdAt) {
  if (!bridgeState || !id) return false;
  if (!bridgeState.delivery) bridgeState.delivery = {};
  // AUDIT-4 + AUDIT-5 (fail-closed): if the ledger is full of immature
  // delivered / valid pending and there is no room for a new pending without
  // breaking the guarantee, we do NOT admit -> we return FALSE and the caller
  // MUST abort (do not process the command). The relay will re-deliver the
  // event. Without this, the handler would run the operation without a
  // durable `pending` entry, and a later replay would re-run it (break
  // AUDIT-6: on reaching the soft-limit we force aggressive cleanup (re-scan
  // of the cursor) to try to free space BEFORE rejecting; never evicting a
  // still-protected delivered nor valid pending. If after cleanup it is still
  // full with legitimate immature delivered, it rejects fail-closed (avoiding
  // the permanent lock by chaining the re-scan in the next cycle).
  const pruned = evictDeliveryLedger(status === 'pending' && deliverySize >= DELIVERY_SOFT_LIMIT);
  const n = deliverySize;
  if (status === 'pending' && n >= DELIVERY_MAX && !bridgeState.delivery[id]) {
    // AUDIT-6: request a re-scan so the cursor advances and frees already
    // unreachable delivered; if the relay has nothing more to deliver there
    // will be no promotion, but this prevents a frozen lastSeen from freezing
    deliveryRescanNeeded = true;
    console.warn('[nostr] delivery ledger lleno; admision rechazada (fail-closed, backpressure) y re-scan programado');
    backpressureRejected++;
    // NEW-AUDIT (HIGH, no-loss): the fail-closed rejection for a full ledger is
    // A DROP the relay will NOT re-deliver unless the `since` of the next
    // subscription covers it. updateLastSeen() (called before admission, in
    // the handler) already advanced lastSeen with the reception time, and the
    // next subscription cursor would anchor to lastSeen (only to pendingSince
    // if a drop is registered). Without this, a legitimate `L` rejected here
    // would fall outside the overlap (lastSeen - 120) during a burst -> the
    // relay does not re-deliver it -> PERMANENT loss of a legitimate DM (not
    // just DoS). We reuse the ALTO-2 enqueueGiftWrap mechanism: register the
    // id in the drops ledger and anchor pendingSince STICKY until the drop is
    // re-seen, so the `since` never jumps ahead of `L`.
    if (bridgeState) {
      // The created_at of the discarded event (when the caller knows it: the
      // backlog path via handleIncomingGiftWrap passes the real wrap) is used
      // to anchor pendingSince to MIN(local cursor, created_at) and for
      // point recovery by id. AUDIT-16 (🔴): anchoring ONLY to the local
      // rejection time loses a delayed event with an earlier created_at (the
      // relay will not re-deliver it if the `since` does not cover its
      recordDropped(id, createdAt);
      const nowTs = Math.floor(Date.now() / 1000);
      if (bridgeState.pendingSince == null || nowTs < bridgeState.pendingSince) {
        bridgeState.pendingSince = nowTs;
        markStateDirty();
      }
    }
    requestDeliveryRescan();
    return false; // no admission; the relay will re-deliver
  }
  // AUDIT-10 (root): the recovery watermark advances via processWatermark()
  // with the timestamp of the REAL confirmed event (processed/admitted), NOT
  // by Date.now() of an internal admission — that would expire delivered the
  // relay never confirmed traversing (break exactly-once after downtime).
  // Eviction uses the already-established watermark.
  evictDeliveryLedger(false);
  bridgeState.delivery[id] = {status, ts: nowSec()};
  deliverySize++;
  if (pruned) markStateDirty();
  flushStateNow();
  // AUDIT-9 (MEDIUM): a successful admission (or a promotion to delivered)
  // is REAL ledger progress -> exit BACKPRESSURE and reset the stall burst.
  // We only consider progress if we really freed/advanced: a new pending
  // entering does not free, but a promotion/rejection does; here we treat
  // any ledger write as recovery activity and reset the counter so the
  // normal admission cycle is not penalized.
  if (rescanStalled) {
    rescanStalled = false;
    rescanStalledSince = 0;
    console.log('[nostr] BACKPRESSURE levantado: admision en el ledger (progreso real)');
  }
  rescanAttempts = 0;
  return true;
}

function deliveryStatus(id) {
  if (!bridgeState || !bridgeState.delivery || !id) return null;
  return (bridgeState.delivery[id] && bridgeState.delivery[id].status) || null;
}

// AUDIT-3 (SECURITY): terminal helper for the delivery ledger. Every terminal
// path of handleIncomingGiftWrap MUST confirm the outcome so a wrap is
// promoted to `delivered` (idempotent, durable) exactly when the operation
// completed, or stays `pending` (relay replay retries it) on failure, or is
// removed (rejected=true) when the handler decided not to process it.
// Without this, `status`/`help`/`join`/`leave`/`[sala]` etc. would leave a
// permanent `pending` that is never deduplicated -> the same gift-wrap is
// re-executed on every reconnect within the overlap window (spurious repeats;
// state-mutating commands like join/leave run twice).
function finishDelivery(id, ok, rejected) {
  if (!id) return;
  if (rejected) {
    // The handler definitively won't process it (e.g. no room, not
    // processable, permission denied by M01): drop the durable intent so it
    // can't linger as pending. IMPORTANT: rejected does NOT advance the
    // recovery watermark — the relay will re-deliver, and we have NO evidence
    // it actually traversed this range.
    const d = bridgeState && bridgeState.delivery;
    if (d && d[id]) { delete d[id]; deliverySize--; markStateDirty(); }
    return;
  }
  if (ok) {
    // AUDIT-M01-OPCION2 (kaieriksen, 🔴 BLOCKING): the watermark now advances
    // with the bridge's LOCAL clock (confirmed stream progress: the ledger
    // confirmed the processing of a real event), NEVER with the gift-wrap's
    // `created_at` (input controlled by the sender). Previously it called
    // processWatermark(wrapTs) with giftWrap.created_at, so an AUTHORIZED
    // sender could advance recoveryWatermark up to +23h with a forged
    // created_at, and the watermark drives delivered eviction
    // (deliveredCanExpire) — manipulable by the sender, not by relay progress.
    // advanceRecoveryWatermark() uses local Date.now() and
    // honors AUDIT-10 (no first jump from 0 without prior watermark evidence):
    // the bridge clock is not controlled by any sender.
    markDelivery(id, 'delivered');
    advanceRecoveryWatermark();
  }
  // else: leave as `pending` -> the relay replay (overlap/since-anchor)
  // will retry it. No loss, no duplicate delivery.
}

function updateLastSeen(tsIgnored) {
  // M-NEW-02: decouple the anti-backlog cursor from the incoming event's
  // created_at entirely. NIP-17 lets a sender stamp backdated OR future
  // timestamps, so created_at is NOT a reliable cursor: a backdated wrap
  // would advance lastSeen to the past (re-delivering old wraps) and a
  // future one would hide real DMs until the wall clock catches up.
  //
  // Instead, the cursor tracks the bridge's OWN reception time (real clock,
  // never trusted from the wire). The `since` of the next subscription is
  // then lastReception - overlap, which overlaps only the last few seconds
  // of traffic — and the persistent `seenIds[]` (by event id) is the
  // authoritative duplicate guard that prevents re-processing.
  //
  // The old created_at argument is deliberately ignored (kept as
  // `tsIgnored` to avoid touching all call sites); it is no longer the
  // source of truth for the backlog cursor.
  if (!bridgeState) return;
  const now = Math.floor(Date.now() / 1000);
  if (now > bridgeState.lastSeen) {
    bridgeState.lastSeen = now;
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

// Initialize the durable kill-switch (pause) FIRST, in ANY mode (jitsi,
// nostr, both). It must not depend on bridgeState (which only exists in
// NOSTR_MODE) — without its own file, a jitsi-only deployment would lose
// the pause on restart.
loadPause();

// Initialize state only in nostr/both mode (where the subscription exists)
if (NOSTR_MODE) {
  loadState();
  // If there was no previous state for this relay, initialize it so that
  // updateLastSeen()/markSeen() can persist from the very first event.
  if (!bridgeState || bridgeState.relay !== CONFIG.nostr.relay) {
    bridgeState = {relay: CONFIG.nostr.relay, lastSeen: 0, seenIds: [], pendingSince: null, dropped: [], droppedOverflow: false, recoveryWatermark: 0, delivery: {}};
    deliverySize = 0;
  }
}

const NICK = CONFIG.nick || 'secretario';
const ROOM_SUFFIX = CONFIG.roomSuffix || '@conference.meet.example.com';

// ---------------------------------------------------------------------------
// Recordings (jitsi mode only; shared by the HTTP API if applicable)
// ---------------------------------------------------------------------------
const RECORDINGS_DIR = CONFIG.recordingsDir || '/var/recordings';
const DL_BASE = CONFIG.downloadBase || 'https://meet.example.com';
const DL_SECRET_FILE = CONFIG.downloadSecretFile || '/opt/recordings-serve/.secret';
const DL_EXPIRY_HOURS = CONFIG.downloadExpiryHours || 24;
function readDlSecret() {
  try {
    assertPrivateFile(DL_SECRET_FILE, 'recording download secret');
    return fs.readFileSync(DL_SECRET_FILE, 'utf8').trim();
  } catch (e) {
    console.error('[recordings] download secret unavailable:', e.message);
    return null;
  }
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
    const files = fs.readdirSync(RECORDINGS_DIR).filter(f => /^[A-Za-z0-9._-]+\.mp4$/i.test(f));
    return files.flatMap(f => {
      const full = path.join(RECORDINGS_DIR, f);
      let st;
      try { st = fs.lstatSync(full); } catch (_) { return []; }
      // Never follow operator-controlled symlinks from a recordings directory.
      if (!st.isFile()) return [];
      return [{name: f, size: st.size, mtime: st.mtime.toISOString()}];
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
let DERIVED = null;
try {
  DERIVED = loadOrgRouting(ORG_FILE);
} catch (e) {
  if (e.code === 'EMISSING') {
    // No org.yaml deployed -> legacy manual routing is the intended fallback.
    console.warn('[bridge] org.yaml no encontrado (' + ORG_FILE + '); usando routing manual del config.json.');
    DERIVED = null;
  } else {
    // M-09 FAIL-CLOSED: org.yaml is present but invalid/unreadable.
    // We do NOT start with a possibly-permissive/outdated manual routing that
    // deviates from the normative source of truth. Aborting is the safe move.
    console.error('[bridge] ERROR FATAL (fail-closed): ' + e.message);
    console.error('[bridge] Corrige org.yaml o elimínalo si quieres volver al routing manual.');
    throw e;
  }
}
if (DERIVED) {
  // MEDIO-5 (audit 462e62b): org.yaml is the ONLY source of truth for
  // identity when it exists and is valid. Previously it did
  //   CONFIG.agents = Object.assign({}, DERIVED.agents, CONFIG.agents || {})
  // which allowed an 'dave' (or a non-existent 'superadmin') from the
  // config.json to OVERWRITE the npub derived from org.yaml, escalating
  // privileges or violating the source of truth. Now: no merge. Manual agents
  // are ONLY used if org.yaml does not exist (legacy fallback below).
  if (CONFIG.agents && Object.keys(CONFIG.agents).length) {
    console.warn('[bridge] WARNING: agents del config.json IGNORADOS — org.yaml (' + ORG_FILE + ') es la única fuente de verdad de identidad (MEDIO-5).');
  }
  CONFIG.agents = DERIVED.agents;
  if (CONFIG.routing && CONFIG.routing.permissions && Object.keys(CONFIG.routing.permissions).length) {
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
//   "permissions": { "carol": ["dave", "alice", "bob"], "dave": ["carol"], ... },
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
// Persistence: the delivery-critical anti-loop state (nextAdmissionId,
// requests, pairs, pairHours) is persisted in the same .bridge-state.json
// (H-04) so a crash+restart cannot re-deliver already-committed DMs.
// The `hashes` maps (canon+shingles) stay in-memory: losing them can only
// re-allow a fuzzy dedup, never re-deliver a committed DM. The drop count
// is exposed in /status.
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

// H-04: restore the persisted anti-loop state now that ANTILOOP exists.
// (loadState() runs earlier in module init and only stashed s.antiloop in
// bridgeState.antiloop to avoid touching the not-yet-initialized const.)
if (bridgeState && bridgeState.antiloop) {
  deserializeAntiloop(bridgeState.antiloop);
}

// --- Envelope de protocolo (norma v1.3) ------------------------------------
// Formato: PRIMERA LÍNEA `[env] {json}` seguida del mensaje (F2-01: el
// bridge delivers the envelope as the real first line; the [from] goes after).
// The bridge seals/validates it; the personas keep it when replying (norma).
//
// F2-06: the JSON is parsed from the COMPLETE first line (not with a non-
// greedy regex over the object) — supports `}` inside JSON strings.
// F2-02: strict type/range validation. An invalid envelope is treated
// as nonexistent (null) and the message falls through to the remaining defenses.
function envelopeMac(env, rest) {
  // The anti-loop envelope is security-sensitive metadata. Sign both the
  // metadata and the payload so an attacker cannot copy a valid envelope and
  // alter the message underneath it. bridgeSk is initialized before any
  // runtime message can reach this function.
  const unsigned = {...env};
  delete unsigned.sig;
  const canonical = JSON.stringify(unsigned) + '\n' + String(rest);
  return crypto.createHmac('sha256', Buffer.from(bridgeSk)).update(canonical).digest('hex');
}

function parseEnvelope(text) {
  const str = String(text);
  const nl = str.indexOf('\n');
  const firstLine = (nl === -1 ? str : str.slice(0, nl)).trim();
  const mm = firstLine.match(/^\[env\]\s+/);
  if (!mm) return null;
  const jsonPart = firstLine.slice(mm[0].length);
  let env;
  try { env = JSON.parse(jsonPart); } catch (_) { return null; }
  if (!env || typeof env !== 'object' || Array.isArray(env)) return null;
  if (!Number.isSafeInteger(env.hops) || env.hops < 0) return null;
  if (env.trace !== undefined && !Array.isArray(env.trace)) return null;
  if (env.trace && !env.trace.every(x => typeof x === 'string')) return null;
  if (env.expires !== undefined && (!Number.isSafeInteger(env.expires) || env.expires <= 0)) return null;
  if (env.rid !== undefined && typeof env.rid !== 'string') return null;
  if (env.trace === undefined) env.trace = [];
  const rest = (nl === -1 ? '' : str.slice(nl + 1)).trim();
  let authenticated = false;
  if (typeof env.sig === 'string' && /^[0-9a-f]{64}$/i.test(env.sig)) {
    const expected = envelopeMac(env, rest);
    authenticated = crypto.timingSafeEqual(Buffer.from(env.sig, 'hex'), Buffer.from(expected, 'hex'));
  }
  return {env, rest, authenticated};
}

function stripEnvelopeLine(text) {
  const str = String(text);
  const nl = str.indexOf('\n');
  const firstLine = (nl === -1 ? str : str.slice(0, nl)).trim();
  if (!/^\[env\]\s+/.test(firstLine)) return str.trim();
  return (nl === -1 ? '' : str.slice(nl + 1)).trim();
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
// ALTO-1 (audit 462e62b): full cryptographic authentication of a NIP-17
// gift-wrap chain before the bridge processes it.
//
// Returns the decrypted rumor ({pubkey, content, created_at, id}).
// Throws on ANY of:
//   - wrap: kind !== 1059, id !== getEventHash, or invalid schnorr sig
//     (prevents replay: a re-stamped clone with a new id/arbitrary sig fails)
//   - seal: kind !== 13, id !== getEventHash, or invalid sig
//   - rumor: kind !== 14, id !== getEventHash, or seal.pubkey !== rumor.pubkey
//
// Kept as a standalone pure helper so the regression test can exercise the
// exact code path handleIncomingGiftWrap() uses, without booting the whole
// bridge (relay, rooms, etc.).
function unwrapAndVerifyGiftWrap(giftWrap) {
  if (!giftWrap || typeof giftWrap !== 'object') throw new Error('no event');
  // ── wrap (kind:1059) ──
  if (giftWrap.kind !== 1059) throw new Error('wrap kind != 1059 (' + giftWrap.kind + ')');
  if (giftWrap.id !== getEventHash(giftWrap)) throw new Error('wrap id no canónico');
  if (!verifyEvent(giftWrap)) throw new Error('wrap firma inválida');

  const seal = JSON.parse(nip44.decrypt(giftWrap.content, nip44.getConversationKey(bridgeSk, giftWrap.pubkey)));
  // ── seal (kind:13) ──
  if (!seal || typeof seal !== 'object') throw new Error('seal no descifrado');
  if (seal.kind !== 13) throw new Error('seal kind != 13 (' + seal.kind + ')');
  if (seal.id !== getEventHash(seal)) throw new Error('seal id no canónico');
  if (!verifyEvent(seal)) throw new Error('seal firma inválida');

  const unwrapped = JSON.parse(nip44.decrypt(seal.content, nip44.getConversationKey(bridgeSk, seal.pubkey)));
  // ── rumor (kind:14) ──
  if (!unwrapped || typeof unwrapped !== 'object') throw new Error('rumor no descifrado');
  if (unwrapped.kind !== 14) throw new Error('rumor kind != 14 (' + unwrapped.kind + ')');
  if (unwrapped.id !== getEventHash(unwrapped)) throw new Error('rumor id no canónico');
  if (unwrapped.pubkey !== seal.pubkey) {
    throw new Error('rumor.pubkey != seal.pubkey (posible spoofing de identidad)');
  }
  return unwrapped;
}

function stampEnvelope(text, from, to) {
  const parsed = parseEnvelope(text);
  // Only a bridge-signed envelope is trusted. If an attacker supplies a
  // forged [env] line, discard that line entirely and start a fresh envelope
  // rather than preserving attacker-controlled metadata in the payload.
  const rest = parsed ? parsed.rest : stripEnvelopeLine(text);
  const env = parsed && parsed.authenticated ? {...parsed.env} : {rid: extractRid(rest) || undefined, hops: 0, trace: []};
  delete env.sig;
  env.hops = (env.hops || 0) + 1;
  if (env.trace[env.trace.length - 1] !== from) env.trace.push(from);
  env.trace.push(to);
  if (!env.expires) env.expires = Date.now() + ANTILOOP.expireMs;
  env.sig = envelopeMac(env, rest);
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
    .replace(/[^\p{L}\p{N}\s]/gu, ' ') // keep letters/numbers in ANY script (Unicode), drop the rest (emoji, punct)
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
  if (parsed && parsed.authenticated) {
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
  // M-07: an empty canonical (e.g. pure emoji / CJK handled by `canonicalize`) must not
  // collapse every such message into the SAME hash — otherwise the 2nd of any two
  // non-Latin messages gets dropped as a false-positive duplicate. Skip exact dedup
  // when canonicalize produced no comparable text.
  if (!canon) {
    if (ANTILOOP.hashes.has(h)) {
      ANTILOOP.hashes.delete(h); // don't let a stale empty-canon entry poison future dedup
    }
  } else if (ANTILOOP.hashes.has(h)) {
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

  // H-04: persist the admission (identity + rates) so a crash+restart cannot
  // re-deliver this DM. Debounced flush reuses the existing state timer.
  markStateDirty();

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
  // H-04: persist the compensation so a restart keeps the rolled-back state
  // consistent (no dangling admission from the pre-crash snapshot).
  markStateDirty();
}


// ---------------------------------------------------------------------------
// Nostr — common layer (publishDM, subscribe, handling)
// ---------------------------------------------------------------------------
const bridgeNsec = readSecret(CONFIG.nostr, 'nsec', 'nsecFile', 'Nostr bridge');
if (!bridgeNsec) throw new Error('Missing nostr.nsec (use "vault:NAME" or "env:VAR")');
const {data: bridgeSk} = nip19.decode(bridgeNsec);
const bridgePk = getPublicKey(bridgeSk);

let relaySk = null;
if (JITSI_MODE) {
  const relayNsec = readSecret(CONFIG.nostr, 'relayNsec', 'relayNsecFile', 'Jitsi relay');
  if (!relayNsec) {
    throw new Error(
      'Jitsi room relaying requires a separate relayNsec identity ("vault:NAME" or "env:VAR"). ' +
      'Room attendee content must never be signed by the trusted bridge principal.'
    );
  }
  ({data: relaySk} = nip19.decode(relayNsec));
}

async function publishDMWithKey(secretKey, recipientPk, content, title) {

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
    pubkey: getPublicKey(secretKey)
  };
  rumor.id = getEventHash(rumor); // mandatory canonical id
  const seal = finalizeEvent({
    kind: SEAL_KIND,
    content: nip44.encrypt(JSON.stringify(rumor), nip44.getConversationKey(secretKey, recipientPk)),
    created_at: nowTs,
    tags: []
  }, secretKey);
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
        const ev = finalizeEvent(makeAuthEvent(CONFIG.nostr.relay, m[1]), secretKey);
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

async function publishDM(recipientPk, content, title) {
  return publishDMWithKey(bridgeSk, recipientPk, content, title);
}

// Subscription: listen to gift-wraps addressed to the bridge
function subscribeIncoming() {
  const ws = new WebSocket(CONFIG.nostr.relay);
  // AUDIT-6: registrar el delegado para que requestDeliveryRescan() pueda
  // forzar un re-scan cerrando esta conexion (onclose -> subscribeIncoming).
  reconnectIncoming = () => { try { ws.close(); } catch (_) {} };
  // `since`: start from the last PROCESSED event (with an overlap margin) so
  // the historical backlog is not reprocessed on every reconnect.
  // The processing cursor is recoveryWatermark (the range the relay has
  // confirmed as traversed by actually admitting/processing), NOT lastSeen —
  // lastSeen is the receive cursor (last local reception, advances by
  // Date.now() BEFORE admitting), so it does NOT represent which range was
  // processed. Anchoring `since` to raw lastSeen could skip received events
  // not yet admitted under backlog+backpressure. H-NEW-01: if there are
  // pending drops, pendingSince (STICKY) is the most conservative anchor and
  // never lets an unrecovered drop slip past.
  // No previous state -> full backlog (original behavior).
  let since = null;
  if (bridgeState && bridgeState.relay === CONFIG.nostr.relay) {
    const recovery = bridgeState.recoveryWatermark || 0;
    // Base anchor: the processing cursor (range confirmed as traversed).
    let cursor = recovery;
    // If there are unrecovered drops, pendingSince is earlier and conservative:
    // we use it as the anchor (never pass ahead of an unrecovered drop).
    if (bridgeState.pendingSince != null) {
      cursor = (cursor === 0 || bridgeState.pendingSince < cursor)
        ? bridgeState.pendingSince : cursor;
    }
    if (cursor > 0) {
      since = cursor - STATE_OVERLAP_SECS;
      console.log('[nostr] subscription since', new Date(since * 1000).toISOString(), '(process cursor ' + new Date(cursor * 1000).toISOString() +
        (bridgeState.pendingSince != null ? ', pending drop marker active' : '') + ', lastSeen ' + new Date((bridgeState.lastSeen || 0) * 1000).toISOString() + ')');
    }
  }
  if (since === null) {
    console.log('[nostr] no previous processed state for this relay: full backlog subscription');
  }
  const REQ_FILTER = {kinds: [1059], '#p': [bridgePk]};
  if (since !== null) REQ_FILTER.since = since;
  // AUDIT-16 (🔴 HIGH): point recovery by ID. The `since` is a temporal
  // cursor: a wrap delayed by backlog (much-earlier created_at) can fall
  // outside [since, now] even with pendingSince anchored to its created_at
  // (relays do not guarantee ordered delivery). To not rely only on the
  // temporal cursor, we also ask the relay for EACH pending drop id
  // explicitly (`ids` filter), outside the temporal range. The EVENTs of
  // this sub route the same way via enqueueGiftWrap -> markSeen ->
  // recoverDropped, so each recovered drop is removed from the ledger and,
  // when it empties, releasePendingSinceIfRecovered frees the anchor.
  const droppedIds = (bridgeState && bridgeState.dropped && bridgeState.dropped.length)
    ? bridgeState.dropped.filter(d => d && d.id).map(d => d.id)
    : [];
  const sendReq = () => {
    ws.send(JSON.stringify(['REQ', 'bridge-in', REQ_FILTER]));
    // Point fetch by id: only if there are pending drops, in batches to avoid
    // emitting a giant `ids` filter (respect the relay's frame limits).
    if (droppedIds.length > 0) {
      const BATCH = 100;
      for (let i = 0; i < droppedIds.length; i += BATCH) {
        const batch = droppedIds.slice(i, i + BATCH);
        ws.send(JSON.stringify(['REQ', 'bridge-in-byid-' + i, {ids: batch, kinds: [1059]}]));
      }
      console.log('[nostr] fetch puntual por id de', droppedIds.length, 'drop(s) pendiente(s)');
    }
  };
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
    // M-04: cap on the raw frame size before parsing/decrypting.
    // A gift-wrap exceeding the cap is discarded without entering unwrapEvent().
    const rawBytes = typeof e.data === 'string' ? Buffer.byteLength(e.data) : (e.data && e.data.byteLength !== undefined ? e.data.byteLength : -1);
    if (rawBytes !== -1 && rawBytes > NOSTR_MAX_FRAME_BYTES) {
      console.warn('[nostr] frame demasiado grande (' + rawBytes + ' B > ' + NOSTR_MAX_FRAME_BYTES + ' B): ignorado antes de parsear/desencriptar');
      // Mark as seen so we do not infinitely retry an event that will always
      // fail by size (avoids a mini-DoS of relay retries). LOW-8: we use the
      // markRejected cache (SEPARATE from seenIds) to not pollute dedup.
      try {
        const big = JSON.parse(e.data.toString());
        if (big[0] === 'EVENT' && big[2] && big[2].id) markRejected(big[2].id);
      } catch (_) { /* giant non-parseable frame: no id to mark */ }
      return;
    }
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
      // M-05: enqueue with backpressure instead of launching N unbounded async handlers.
      enqueueGiftWrap(m[2]);
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
    // ALTO-3 + MEDIO-4: only skip if it was DELIVERED (committed durably).
    // A wrap that is seen but still `pending` (or has no delivery record)
    // must be RETRIED — otherwise a transient publish failure would mark it
    // seen and drop it forever (MEDIO-4), and a crash before the 5s flush
    // could re-deliver a committed DM (ALTO-3).
    // AUDIT-4 (downtime replay): the dedup relies on the durable ledger as the
    // AUTHORITATIVE source of "already delivered". isSeen() (seenIds) expires
    // after ~180s, so after a long downtime isSeen(id) is false even though the
    // delivery is delivered — requiring BOTH would re-run the command. That is
    // why the gate is deliveryStatus==='delivered' (durable, watermark-backed)
    // and does NOT require isSeen(). isSeen stays as a fast shortcut
    // only for the in-window case (though redundant).
    if (deliveryStatus(giftWrap.id) === 'delivered') {
      console.log('[nostr] duplicate gift-wrap (ya entregado) ignorado:', giftWrap.id.slice(0, 8));
      return;
    }
    // Record the last seen event (even if paused or from an
    // pubkey no autorizado): el estado marca lo ya RECIBIDO, no lo
    // procesado, para no re-entregar DMs viejos tras un reinicio.
    // M-NEW-02: the cursor advances on the bridge's own reception clock, NOT
    // the event's created_at (see updateLastSeen). seenIds[] (by event id)
    // is the real duplicate guard; overlap + seenIds absorb the backlog.
    if (giftWrap) updateLastSeen(0);
    markSeen(giftWrap.id);
    // M-NEW (test adversarial): verify the gift-wrap origin cryptographically.
    // nostr-tools 2.24.1 unwrapEvent() returns the rumor's DECLARED pubkey but
    // NEVER checks that it matches who actually signed the seal/wrap. An
    // attacker who knows a legit agent's pubkey (public on the relay) and can
    // encrypt to the bridge with their own key can forge a wrap whose rumor
    // claims to be that agent — and the bridge would accept it as a DM from
    // that agent (identity spoofing). We therefore unwrap in two explicit
    // steps and REQUIRE seal.pubkey === rumor.pubkey (the seal signer is the
    // real sender), rejecting the DM otherwise.
    // ALTO-1 (audit 462e62b): authenticate the ENTIRE NIP-17 chain before
    // trusting it. Without this, an observer of the relay can replay a legit
    // gift-wrap: copy pubkey+content, re-stamp created_at, give it a new id +
    // arbitrary sig, and the bridge (which keys dedup by id, not content)
    // would re-execute the command. Rejection criteria in
    // unwrapAndVerifyGiftWrap(): wrap kind/sig/id, seal kind/sig/id, rumor
    // kind/id + seal.pubkey===rumor.pubkey.
    //
    // AUDIT-3 (SECURITY): auth + allowlist happen BEFORE the durable delivery
    // ledger. Previously markSeen+markDelivery(pending) ran first, so a
    // NIP-17-valid wrap from an UNAUTHORIZED sender (or a paused bridge)
    // still entered `delivery` as a permanent 'pending' entry and forced a
    // synchronous fsync each time — an I/O-amplification DoS by anyone able to
    // sign valid wraps to the bridge. Now: only AUTHENTICATED + AUTHORIZED
    // (+ not paused) wraps reach the ledger as 'pending'. Unauthorized /
    // malformed / paused inputs are discarded (or deferred to replay by the
    // relay) without ever touching `delivery`.
    let unwrapped;
    try {
      unwrapped = unwrapAndVerifyGiftWrap(giftWrap);
    } catch (e) {
      console.warn('[nostr] gift-wrap inválido o no autenticado, ignorado:', e.message);
      return;
    }
    const senderPk = unwrapped.pubkey;
    const senderName = agentByPubkey.get(senderPk);
    if (!senderName) {
      console.log('[nostr] DM from unauthorized pubkey, ignored:', senderPk.slice(0, 8));
      return;
    }
    // Kill-switch: nostr paused -> DMs are ignored SILENTLY (no
    // response, so bots process nothing and burn no tokens). AUDIT-3: checked
    // BEFORE the ledger — a paused bridge does NOT admit the wrap to
    // `delivery`, so no permanent 'pending' accumulates; the relay/replay
    // will re-deliver it once resumed (no-admission policy).
    if (PAUSED.nostr) {
      console.log('[nostr] paused: DM ignored');
      return;
    }
    // ALTO-3: declare intent to deliver, durably (fsync) BEFORE processing,
    // so a crash between admission and publish cannot silently re-drop an
    // undelivered DM nor re-distribute a delivered one. AUDIT-3: only reached
    // for authenticated + authorized + non-paused wraps (see above), so the
    // ledger holds ONLY processable in-flight work.
    //
    // AUDIT-5 (ALTO fail-closed): markDelivery MUST return true (admitted) or
    // the handler MUST NOT process the command at all. If the ledger is full
    // of delivered/pending inmaduros and the durable `pending` admission is
    // rejected, executing anyway would run the operation WITHOUT a durable
    // intent -> a subsequent relay replay / crash re-executes it (break
    // exactly-once). So: no admission -> return (relay re-delivers).
    if (giftWrap.id) {
      // AUDIT-16: propagar el created_at real del wrap al camino fail-closed
      // para que pendingSince se ancle al MIN(cursor local, created_at) y se
      // stays in the drops ledger (point recovery by id). A wrap delayed by
      // backlog has a created_at earlier than the local rejection time;
      // anchoring only to Date.now() would make it unreachable.
      const admitted = markDelivery(giftWrap.id, 'pending', giftWrap.created_at);
      if (!admitted) {
        console.warn('[nostr] admision durable rechazada; NO se procesa (backpressure fail-closed, relay reintentara)');
        return;
      }
    }
    const content = unwrapped.content;
    console.log('[nostr] DM from', senderName, ':', content.slice(0, 80));
    const cmd = content.trim().toLowerCase();

    // --- Comandos comunes ---
    if (cmd === 'status' || cmd === 'help' || cmd === 'routes') {
      const ok = await publishDM(senderPk, buildHelp(senderName), 'Bridge').catch(err => { console.error('[bridge] DM failed:', err.message); return false; });
      // AUDIT-M01-OPCION2: status/help/routes are READ-only commands with
      // no effect on the stream; they do not advance the watermark (local clock).
      finishDelivery(giftWrap.id, !!ok, false);
      return;
    }
    if (cmd === 'grabaciones' || cmd === 'recordings' || cmd === 'grabaciones?') {
      if (!JITSI_MODE) {
        const ok = await publishDM(senderPk, 'Este bridge no gestiona salas Jitsi (modo ' + MODE + ').', 'Bridge').catch(() => false);
        finishDelivery(giftWrap.id, !!ok, false);
        return;
      }
      // AUDIT kaieriksen M01: recordings is a room-agnostic privileged action
      // (reads every recording). Only `full`-permission agents (or agents with
      // at least one restricted room) may list recordings; fail closed.
      if (!agentCanOperateRoom(senderName, null)) {
        console.log('[nostr] recordings denegado a', senderName, '(no full permission)');
        const ok = await publishDM(senderPk, 'No tienes permiso para listar grabaciones.', 'Bridge').catch(() => false);
        finishDelivery(giftWrap.id, false, true);
        return;
      }
      const recs = listRecordings();
      const reply = formatRecordingsList(recs);
      console.log('[recordings] listado pedido por', senderName, '->', reply.slice(0, 60));
      const ok = await publishDM(senderPk, reply, 'Grabaciones').catch(err => { console.error('[recordings] DM failed:', err.message); return false; });
      finishDelivery(giftWrap.id, !!ok, false);
      return;
    }

    // --- Routing DM↔DM (nostr/both mode): "@agent text" ---
    if (NOSTR_MODE) {
      const route = parseRouteTarget(content);
      if (route) {
        await handleRoute(senderName, senderPk, route, giftWrap.id);
        return;
      }
    }

    // --- Modo jitsi: comandos de salas y mensajes a sala ---
    if (JITSI_MODE) {
      if (PAUSED.jitsi) {
        console.log('[nostr] jitsi paused: DM from', senderName, 'ignored (room command)');
        const ok = await publishDM(senderPk, '⏸ The Jitsi bridge is paused. Resume with POST /pause {side:jitsi, paused:false}.', 'Bridge').catch(() => false);
        finishDelivery(giftWrap.id, !!ok);
        return;
      }
      const joinM = content.match(/^join(?:\s+(\[[^\]]+\]|\S+))?/i);
      const leaveM = content.match(/^leave(?:\s+(\[[^\]]+\]|\S+))?/i);
      if (joinM || leaveM) {
        if (!handleJoinLeaveFn) {
          console.log('[nostr] handleJoinLeave no disponible (jitsi no iniciado)');
          finishDelivery(giftWrap.id, false, true);
          return;
        }
        // AUDIT kaieriksen M01: authorization is enforced inside
        // handleJoinLeave per target room (a bare `join` with no room falls
        // back to room-agnostic: only `full` agents).
        const ok = await handleJoinLeaveFn(senderName, senderPk, content, joinM, leaveM);
        finishDelivery(giftWrap.id, !!ok, false);
        return;
      }
      let room = null, text = content;
      const m = content.match(/^\[([^\]]+)\]\s*(.*)$/s);
      if (m) { room = m[1].trim(); text = m[2].trim(); }
      if (!room) room = lastRoomByAgent.get(senderName);
      if (!room || !text) {
        console.log('[nostr] DM sin sala ni texto, ignorado');
        finishDelivery(giftWrap.id, false, true);
        return;
      }
      // AUDIT kaieriksen M01: gate message injection by sender + room scope.
      if (!agentCanOperateRoom(senderName, room)) {
        console.log('[nostr] inyección en sala', room, 'denegada a', senderName, '(sin permiso para esa sala)');
        const ok = await publishDM(senderPk, 'No tienes permiso para escribir en la sala ' + room + '.', 'Bridge').catch(() => false);
        finishDelivery(giftWrap.id, false, true);
        return;
      }
      if (!rooms.has(room)) {
        console.log('[nostr] sala', room, 'no activa en el puente, ignorado');
        finishDelivery(giftWrap.id, false, true);
        return;
      }
      const ok = await sendToRoom(room, `[${senderName}] ${text}`);
      finishDelivery(giftWrap.id, !!ok, false);
      return;
    }

    console.log('[nostr] DM no procesado (modo ' + MODE + '):', content.slice(0, 60));
    finishDelivery(giftWrap.id, false, true);
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
async function handleRoute(fromName, fromPk, route, giftWrapId) {
  const toPk = agentByName.get(route.to);
  if (!toPk) {
    await publishDM(fromPk, 'Agente desconocido: @' + route.to + '. Agentes: ' + [...agentByName.keys()].join(', '), 'Bridge').catch(err => console.warn('[routing] aviso "agente desconocido" no entregado a', fromPk.slice(0, 8) + ':', err.message));
    // 🟡 LOW (audit): DETERMINISTIC rejection — the retry will never change
    // the result (the agent does not exist). Finish to not leave a useless
    // `pending` consuming the ledger until PENDING_TTL_SECS.
    finishDelivery(giftWrapId, false, true);
    return;
  }
  if (!routingAllowed(fromName, route.to, routingPerms, routingDefault)) {
    console.log('[routing] bloqueado:', fromName, '->', route.to);
    await publishDM(fromPk, 'No tienes permiso para escribir a @' + route.to + '.', 'Bridge').catch(err => console.warn('[routing] aviso "sin permiso" no entregado a', fromPk.slice(0, 8) + ':', err.message));
    // 🟡 LOW (audit): DETERMINISTIC rejection — no permission will not change
    // on retry. Finish (rejected) to free the ledger from this pending.
    finishDelivery(giftWrapId, false, true);
    return;
  }

  // Anti-loop: content dedup + pair rate + request_id short-circuit.
  // If it trips, the message is dropped IN SILENCE (log + counter, no reply to sender).
  const loop = antiLoopCheck(fromName, route.to, route.text);
  if (!loop.ok) {
    console.warn('[antiloop] message blocked (' + loop.reason + '):', loop.detail);
    // 🟡 LOW (audit): DETERMINISTIC rejection — the anti-loop will keep
    // blocking on retry (same content/spam/duplicate). Finish (rejected) to
    // not leave a useless pending in the ledger.
    finishDelivery(giftWrapId, false, true);
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
    // ALTO-3 + MEDIO-4: delivered durably BEFORE notifying the sender, so a
    // later crash does not re-distribute or lose it.
    if (giftWrapId) {
      markDelivery(giftWrapId, 'delivered');
      // AUDIT-M01-OPCION2: a successful routing also confirms real processing
      // -> the watermark advances with the bridge's LOCAL clock (confirmed
      // stream progress), never with a sender-created created_at.
      advanceRecoveryWatermark();
    }
    await publishDM(fromPk, `Mensaje entregado a @${route.to}.`, 'Bridge').catch(() => {});
  } else {
    // F2-10 + F2-R02: the publication failed — compensate ONLY the
    // anti-loop state consumed by THIS admission (hash/pair/rid), using the
    // COMMIT admission token. So the sender's retry does not fall into a
    // false loop positive due to a network failure, and concurrent
    // admissions that touched the same structures are not undone.
    antiLoopRollback(admission);
    ANTILOOP.routed--;
    // MEDIO-4: keep delivery in 'pending' (it stays non-seen-as-delivered), so
    // a retry / reconnect can deliver it; do NOT mark delivered.
    await publishDM(fromPk, `No pude entregar el mensaje a @${route.to}.`, 'Bridge').catch(err => console.warn('[routing] aviso de fallo de entrega no entregado a', fromPk.slice(0, 8) + ':', err.message));
  }
}


function buildUntrustedRoomRelayPayload(room, nick, text) {
  return '[phantombridge-relay:v1] ' + JSON.stringify({
    origin: 'jitsi-room',
    version: 1,
    room: String(room),
    speaker: String(nick).replace(/[\r\n\[\]]/g, '_').slice(0, 120),
    text: String(text).slice(0, 16000),
  });
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

// --- Authorisation gate for agent-controlled Jitsi paths -------------------
// (AUDIT: kaieriksen M01 — the configured `permissions` were never enforced
// on join/leave/inject/recordings: roomAgents only limits *recipients*, it
// does not authorise the *sender*, so any authenticated agent could control
// arbitrary rooms and inject messages. Now every room command and message is
// gated by sender + room scope before being accepted.)
//
// Config shape:
//   "permissions": {
//     "full":            ["bob", "alice"],        // agents that may control ANY room
//     "restricted": { "room-a": ["dave"], ... }  // room -> agents allowed in that room
//   }
//
// Resolution (fail closed):
//   - if `restricted` has an explicit entry for the room, ONLY agents listed
//     there (plus `full` agents) are allowed;
//   - otherwise, if `full` is non-empty, ONLY `full` agents are allowed
//     (explicit allow-list, nothing else);
//   - if NO `permissions` block is configured at all, fall back to the
//     legacy behaviour (any authenticated agent) for backward compatibility.
// AUDIT M01 (fail-closed, kaieriksen): distinguir "bloque permissions AUSENTE"
// (legacy/open) de "bloque PRESENTE" (fail-closed por defecto). Un bloque
// present but empty or malformed must NOT activate legacy: the operator who
// wrote "permissions": {} expects "I granted nothing -> nobody operates",
// and a "permissions" with an invalid shape (e.g. full: "alice" instead of
// an array, or a non-boolean object) must not open the bridge either. We use
// hasOwnProperty to detect the block even if it is empty ({}) or malformed.
const permConfigured = Object.prototype.hasOwnProperty.call(CONFIG, 'permissions');
const permObject = permConfigured && CONFIG.permissions
  && typeof CONFIG.permissions === 'object' && !Array.isArray(CONFIG.permissions)
  ? CONFIG.permissions : {};
const permFull = new Set(
  (Array.isArray(permObject.full)) ? permObject.full : []
);
const permRestricted = new Map();
if (permConfigured && permObject.restricted
    && typeof permObject.restricted === 'object'
    && !Array.isArray(permObject.restricted)) {
  for (const [room, agents] of Object.entries(permObject.restricted)) {
    if (Array.isArray(agents)) permRestricted.set(room, new Set(agents));
  }
}

// Can this sender operate a room command (join/leave/inject/recordings) on
// `room`? room defaults to "*" for room-agnostic commands (e.g. recordings).
function agentCanOperateRoom(senderName, room /* string|null */) {
  if (!senderName) return false;
  if (!permConfigured) return true; // legacy: no permissions block -> open
  if (permFull.has(senderName)) return true; // full: any room
  if (room) {
    const allowed = permRestricted.get(room);
    if (allowed) return allowed.has(senderName);
    // Room not in restricted map: only `full` agents (already checked above).
    return false;
  }
  // Room-agnostic action (recordings): only `full` agents.
  return false;
}

// Pure permission decision logic, extracted from agentCanOperateRoom to be
// able to test the configuration matrix in isolation (without loading the
// whole module, which needs a full config and NIP-17 valid network — see the
// kaieriksen review §4 criticism: "the tests do not actually test the matrix").
// Resolves "can sender operate room?" against a raw permissions object:
//   undefined / no block            -> legacy (open)
//   {} / malformed / no grants      -> fail-closed (deny)
//   { full: [names] }                 -> full: any room + room-agnostic
//   { restricted: {room:[names]} }    -> only in listed rooms, room-agnostic needs full
// Returns true if the block is absent (legacy) or the sender meets the rule;
// false in any other case (fail-closed).
function evalRoomPermission(permConfig, senderName, room /* string|null */) {
  if (!senderName) return false;
  const configured = permConfig !== undefined;
  if (!configured) return true; // legacy: no permissions block -> open
  if (permConfig === null || typeof permConfig !== 'object' || Array.isArray(permConfig)) return false;
  const full = new Set(Array.isArray(permConfig.full) ? permConfig.full : []);
  if (full.has(senderName)) return true;
  if (room) {
    if (permConfig.restricted && typeof permConfig.restricted === 'object'
        && !Array.isArray(permConfig.restricted)) {
      const allowed = permConfig.restricted[room];
      if (allowed && Array.isArray(allowed)) return allowed.includes(senderName);
    }
    return false;
  }
  // Room-agnostic action (recordings): only `full` agents.
  return false;
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
// joinRoom/leaveRoom viven dentro del bloque JITSI (block-scoped) y NO son
// visible outside it. The HTTP handler (http.createServer, outside the block)
// references them -> ReferenceError 'joinRoom is not defined' when calling
// POST /join or /leave. We expose module-level aliases (same pattern as
// handleJoinLeaveFn) and assign them inside the block. The only way for
// HTTP /join /leave to work in Jitsi/both mode.
let joinRoomFn = null;
let leaveRoomFn = null;
if (JITSI_MODE) {
  // TLS is configured only on the XMPP client. Never monkey-patch node:tls
  // globally: unrelated connections must keep normal certificate verification.
  // For private/self-signed deployments, install the CA in the host trust store
  // or set NODE_EXTRA_CA_CERTS before starting the process.
  if (CONFIG.xmpp.rejectUnauthorized === false) {
    throw new Error(
      'CONFIG.xmpp.rejectUnauthorized=false is forbidden. ' +
      'Use a trusted certificate/CA (or NODE_EXTRA_CA_CERTS) instead; ' +
      'PhantomBridge never disables TLS verification.'
    );
  }

  const {client, xml, jid} = require('@xmpp/client');
  const xmppPassword = readSecret(CONFIG.xmpp, 'password', 'passwordFile', 'XMPP');
  if (!xmppPassword) throw new Error('Missing XMPP password/passwordFile');

  xmpp = client({
    service: CONFIG.xmpp.service || 'xmpps://127.0.0.1:5223',
    domain: CONFIG.xmpp.domain || 'auth.meet.example.com',
    username: CONFIG.xmpp.username || 'bridge',
    password: xmppPassword,
    ...(CONFIG.xmpp.caFile ? {ca: fs.readFileSync(path.resolve(path.dirname(CONFIG_PATH), CONFIG.xmpp.caFile))} : {}),
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
    // Never auto-join an unmanaged room because of an incoming stanza.
    // A remote occupant must not be able to expand the bridge's room set.
    if (!rooms.has(room)) {
      console.log('[xmpp] ignoring message from unmanaged room:', room);
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
      // Room attendees are untrusted external input. Never sign their text with
      // the bridge's authenticated principal. The separate relay identity must
      // be classified as untrusted/relay_npubs by the receiving phantombot.
      const content = buildUntrustedRoomRelayPayload(room, nick, text);
      publishDMWithKey(relaySk, pk, content, `Jitsi ${room}`).catch(err => console.error('[nostr] DM failed:', err.message));
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
    if (typeof room !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(room)) {
      return {ok: false, error: 'invalid room name'};
    }
    if (typeof (opts.nick || NICK) !== 'string' || !/^[^\x00-\x1f\x7f/]{1,120}$/.test(opts.nick || NICK)) {
      return {ok: false, error: 'invalid nick'};
    }
    if (opts.password != null && (typeof opts.password !== 'string' || opts.password.length > 512)) {
      return {ok: false, error: 'invalid room password'};
    }
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
    // L-02: return whether we actually left a room, so HTTP /leave can
    // report an error when the room was never active instead of a fake ok.
    if (!rooms.has(room)) return false;
    const roomJid = room + ROOM_SUFFIX;
    const nick = rooms.get(room).nick || NICK;
    try {
      await xmpp.send(xml('presence', {to: `${roomJid}/${nick}`, type: 'unavailable'}));
      rooms.delete(room);
      lastActivity.delete(room);
      roomOccupants.delete(room);
      cancelAloneLeave(room);
      console.log('[xmpp] salido de', room);
      return true;
    } catch (err) {
      console.error('[xmpp] error leave', room, err.message);
      return false;
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

// ---------------------------------------------------------------------------
// persistConfig — serialized atomic writer for the config file.
// ---------------------------------------------------------------------------
// AUDIT kaieriksen M04 (🔴 BLOQUEANTE): `/register` y `persistRoomTimeout`
// wrote CONFIG_PATH+'.tmp' with writeFileSync+renameSync WITHOUT serializing.
// Two concurrent HTTP requests (or /register + a join with timeout) could use
// the same `.tmp`, and a rename of one overwrote the other's temp/destination
// -> loss of room records or timeouts.
//
// Fix: a single promise queue (writeConfigChain) serializes ALL writes; each
// one uses a UNIQUE temp name (pid+counter) and renames atomically. Two
// writers never share the same `.tmp`.
let _cfgWriteSeq = 0;
let writeConfigChain = Promise.resolve();
function persistConfig() {
  _cfgWriteSeq += 1;
  const tmp = CONFIG_PATH + '.tmp.' + process.pid + '.' + _cfgWriteSeq;
  const op = () => new Promise((resolve, reject) => {
    let fd = null;
    try {
      // AUDIT kaieriksen M04 (fsync / crash durability, MEDIUM): writeFileSync
      // + renameSync gave NAME atomicity but, without fsync, a crash or
      // power-loss right after the rename could restore the previous state or
      // a non-durable transition. For strict no-loss of configuration:
      // write -> fsync(fd) -> close -> rename -> fsync of the parent directory
      // (so the rename itself reaches persistent storage). writeFileSync
      // already flushes the kernel buffer to disk via its internal close, but
      // we add explicit fsync of the fd and the directory to cover the
      // durability of the rename.
      const data = Buffer.from(JSON.stringify(CONFIG, null, 2), 'utf8');
      fd = fs.openSync(tmp, 'w', 0o600);
      fs.writeSync(fd, data, 0, data.length, 0);
      fs.fsyncSync(fd);       // durability of the content (to persistent storage)
      fs.closeSync(fd);
      fd = null;
      fs.renameSync(tmp, CONFIG_PATH); // atomic on the same filesystem
      // fsync the parent directory so the rename is durable.
      try {
        const dirFd = fs.openSync(path.dirname(CONFIG_PATH), 'r');
        try { fs.fsyncSync(dirFd); } finally { fs.closeSync(dirFd); }
      } catch (e) {
        // Directory fsync unsupported on some FS (Windows, certain overlays).
        // Not fatal: the content is already durable; only the rename might
        // not survive an immediate crash. Log and continue.
        console.warn('[persistConfig] fsync de directorio no soportado:', e.message);
      }
      resolve();
    } catch (e) {
      if (fd !== null) { try { fs.closeSync(fd); } catch (_) {} }
      try { fs.unlinkSync(tmp); } catch (_) {}
      reject(e);
    }
  });
  // AUDIT kaieriksen M04 (🔴 HIGH, poisoned queue): ... see the full note
  // further up in this function. A failure isolates the operation but never
  // poisons the chain.
  const chained = writeConfigChain.then(op);
  writeConfigChain = chained.catch(() => {});
  return chained;
}

async function persistRoomTimeout(room, timeout) {
    const n = parseInt(timeout, 10);
    if (isNaN(n) || n <= 0) return;
    roomTimeouts.set(room, n);
    CONFIG.roomTimeouts = CONFIG.roomTimeouts || {};
    CONFIG.roomTimeouts[room] = n;
    // AUDIT M04: atomic serialized write (single queue, unique temp).
    await persistConfig();
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
    if (PAUSED.jitsi) { console.log('[xmpp] send to', room, 'rejected: jitsi paused'); return false; }
    const roomJid = room + ROOM_SUFFIX;
    try {
      await xmpp.send(xml('message', {to: roomJid, type: 'groupchat'}, xml('body', {}, text)));
      console.log('[xmpp] ->', room, ':', text.slice(0, 80));
      return true;
    } catch (err) {
      console.error('[xmpp] error send:', err.message);
      return false;
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
    // AUDIT kaieriksen M01: before acting, gate the room command by
    // sender + room scope. A bare `join`/`leave` with no explicit room is
    // treated as room-agnostic (only `full` agents). Fail closed.
    if (!agentCanOperateRoom(senderName, roomName)) {
      console.log('[nostr] join/leave en sala', roomName, 'denegado a', senderName, '(sin permiso para esa sala)');
      await publishDM(senderPk, 'No tienes permiso para operar la sala ' + roomName + '.', 'Bridge').catch(() => {});
      return;
    }
    const opts = {nick: null, password: null, timeout: null};
    for (const f of content.matchAll(/--(nick|password|timeout)\s+(\S+)/gi)) opts[f[1].toLowerCase()] = f[2];
    if (isJoin) {
      await joinRoom(roomName, opts);
      const ok = rooms.has(roomName);
      // L-01: only persist the timeout after a successful activation, so a
      // failed join does not dirty CONFIG.roomTimeouts.
      if (ok && opts.timeout) await persistRoomTimeout(roomName, opts.timeout);
      const nick = ok ? rooms.get(roomName).nick : opts.nick || NICK;
      const reply = ok
        ? `Room ${roomName} activated in the bridge (joining as ${nick}${opts.password ? ', with password' : ''}).`
        : `Could not activate room ${roomName}. Check the bridge logs.`;
      console.log('[bridge] join via DM from', senderName, '->', roomName, 'nick=' + nick, ok ? 'ok' : 'failed');
      await publishDM(senderPk, reply, 'Bridge').catch(err => console.error('[bridge] DM failed:', err.message));
      return ok;
    } else {
      await leaveRoom(roomName);
      const ok = !rooms.has(roomName);
      const reply = rooms.has(roomName)
        ? `Could not leave room ${roomName}.`
        : `Room ${roomName} deactivated from the bridge.`;
      console.log('[bridge] leave via DM from', senderName, '->', roomName);
      await publishDM(senderPk, reply, 'Bridge').catch(err => console.error('[bridge] DM failed:', err.message));
      return ok;
    }
  }
  handleJoinLeaveFn = handleJoinLeave;
  joinRoomFn = joinRoom;
  leaveRoomFn = leaveRoom;

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
// MEDIO-7 (audit 462e62b): the HTTP MUTATION endpoints (/join, /leave,
// /promote, /register, /pause) allowed ANY local process to operate the bridge
// or trigger XMPP admin actions without a credential. Now they require a
// secret token (Bearer / X-Admin-Token). The read-only GETs (/status,
// /recordings...) also require auth: localhost is not an authorization
// boundary on a shared host.
//
// The token is resolved from CONFIG.httpAdminToken as a secret reference
// ("vault:NAME" via `phantombot vault get`, or "env:VAR"), or from the
// operator-injected PHANTOMBRIDGE_ADMIN_TOKEN environment variable. If the
// operator does not configure it, a RANDOM one is generated at runtime
// (fail-closed) and logged ONCE at startup so the operator can retrieve it.
// We never leave an admin endpoint open without a credential.
let ADMIN_TOKEN;
if (CONFIG.httpAdminTokenFile) {
  throw new Error('HTTP admin token: httpAdminTokenFile (tool-owned plaintext file) is no longer supported — use httpAdminToken: "vault:NAME" (phantombot vault) or "env:VAR", or PHANTOMBRIDGE_ADMIN_TOKEN');
} else if (CONFIG.httpAdminToken) {
  ADMIN_TOKEN = resolveSecretRef(CONFIG.httpAdminToken, 'HTTP admin token');
} else if (process.env.PHANTOMBRIDGE_ADMIN_TOKEN) {
  ADMIN_TOKEN = process.env.PHANTOMBRIDGE_ADMIN_TOKEN.trim();
} else {
  ADMIN_TOKEN = crypto.randomBytes(24).toString('hex');
}
if (ADMIN_TOKEN.length < 16) throw new Error('HTTP admin token must be at least 16 characters');
function getAdminToken() { return ADMIN_TOKEN; }

// Returns true if the request carries the correct admin token.
function hasAdminAuth(req) {
  const h = (req.headers['authorization'] || '').replace(/^Bearer\s+/i, '');
  const x = req.headers['x-admin-token'] || '';
  const provided = h || x;
  if (!provided) return false;
  try {
    return require('crypto').timingSafeEqual(Buffer.from(provided), Buffer.from(ADMIN_TOKEN));
  } catch (e) {
    return false; // different lengths -> timingSafeEqual throws -> not authorized
  }
}

// Guard for mutation endpoints: 401 if the token is missing.
function requireAdmin(req, res) {
  if (hasAdminAuth(req)) return true;
  res.statusCode = 401;
  res.end(JSON.stringify({ok: false, error: 'admin token required (set CONFIG.httpAdminToken or PHANTOMBRIDGE_ADMIN_TOKEN)'}));
  return false;
}

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
    if (!requireAdmin(req, res)) { req.pause(); return; }
    handleBody(async (body) => {
      try {
        if (!JITSI_MODE) return res.end(JSON.stringify({ok: false, error: 'mode ' + MODE + ': no Jitsi rooms'}));
        if (PAUSED.jitsi) return res.end(JSON.stringify({ok: false, error: 'jitsi paused'}));
        const {room, nick, password, timeout} = parseBody(body);
        if (!room) return res.end(JSON.stringify({ok: false, error: 'room required'}));
        const result = await joinRoomFn(room, {nick, password, timeout});
        if (!result || result.ok === false) {
          res.statusCode = 502;
          return res.end(JSON.stringify({ok: false, error: (result && result.error) || 'error joining the room'}));
        }
        // L-01: only persist the room timeout after a SUCCESSFUL join, so a
        // failed join cannot dirty CONFIG.roomTimeouts with a stale timeout.
        if (timeout) await persistRoomTimeout(room, timeout);
        res.end(JSON.stringify({ok: true, room, nick: nick || NICK, password: !!password, timeout: getTimeoutMin(room), allocError: result.allocError || null, already: !!result.already}));
      } catch (e) { res.statusCode = e.statusCode || 500; res.end(JSON.stringify({ok: false, error: e.message})); }
    });
      } else if (req.method === 'POST' && req.url === '/leave') {
    if (!requireAdmin(req, res)) { req.pause(); return; }
    handleBody(async (body) => {
      try {
        if (!JITSI_MODE) return res.end(JSON.stringify({ok: false, error: 'mode ' + MODE + ': no Jitsi rooms'}));
        const {room} = parseBody(body);
        if (!room) return res.end(JSON.stringify({ok: false, error: 'room required'}));
        // L-02: report an error when the room was never active instead of a fake ok.
        const left = await leaveRoomFn(room);
        if (!left) return res.end(JSON.stringify({ok: false, error: 'sala no activa en el puente: ' + room}));
        res.end(JSON.stringify({ok: true, room}));
      } catch (e) { res.statusCode = e.statusCode || 500; res.end(JSON.stringify({ok: false, error: e.message})); }
    });
  } else if (req.method === 'POST' && req.url === '/promote') {
    if (!requireAdmin(req, res)) { req.pause(); return; }
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
    // AUDIT kaieriksen M05 (🔴 BLOCKING 1): the /recordings listing was
    // PUBLIC and minted download bearer URLs (mintDownloadUrl -> HMAC token
    // valid 24h). Any client reaching the HTTP server could get the signed
    // URLs and download every MP4 via /dl/..., bypassing the requireAdmin
    // added to /recordings/:name. Fail-closed: this endpoint ALSO requires
    // the admin token. The listing for authenticated Nostr agents remains
    // covered by the `recordings` DM (M01 gate via agentCanOperateRoom),
    // which does not expose the HTTP server.
    if (!requireAdmin(req, res)) { req.pause(); return; }
    const recs = listRecordings();
    if (!Array.isArray(recs)) {
      res.statusCode = 500;
      return res.end(JSON.stringify({ok: false, error: (recs && recs.error) || 'error listando grabaciones'}));
    }
    recs.forEach(r => { r.url = mintDownloadUrl(r.name); });
    res.end(JSON.stringify({ok: true, recordings: recs}));
  } else if (req.method === 'GET' && req.url.startsWith('/recordings/')) {
    // AUDIT kaieriksen M05 (🔴 BLOCKING): the direct recordings download was
    // UNAUTHENTICATED — binding to 127.0.0.1 is not an auth barrier on a
    // shared host (any local process/user reaching the port could read every
    // MP4). Fix: require the admin token on this route too, same as the
    // mutation endpoints. The /recordings listing (names + URLs signed with
    // expiry) still works for auth agents;
    // the direct download stays protected.
    if (!requireAdmin(req, res)) { req.pause(); return; }
    const raw = req.url.slice('/recordings/'.length);
    let name;
    try {
      name = decodeURIComponent(raw);
    } catch {
      res.statusCode = 400;
      return res.end(JSON.stringify({ok: false, error: 'invalid name'}));
    }
    if (name.includes('/') || name.includes('\\')) {
      res.statusCode = 400;
      return res.end(JSON.stringify({ok: false, error: 'invalid name'}));
    }
    if (!/^[A-Za-z0-9._-]+\.mp4$/i.test(name)) {
      res.statusCode = 400;
      return res.end(JSON.stringify({ok: false, error: 'invalid recording file'}));
    }
    const full = path.join(RECORDINGS_DIR, name);
    let st;
    try { st = fs.lstatSync(full); } catch (_) { st = null; }
    if (!st) {
      res.statusCode = 404;
      return res.end(JSON.stringify({ok: false, error: 'no existe'}));
    }
    if (!st.isFile()) {
      res.statusCode = 404;
      return res.end(JSON.stringify({ok: false, error: 'no existe'}));
    }
    res.setHeader('Content-Type', 'video/mp4');
    res.setHeader('Content-Disposition', 'attachment; filename="' + name + '"');
    const stream = fs.createReadStream(full);
    stream.on('error', () => { res.statusCode = 500; res.end('error leyendo archivo'); });
    stream.pipe(res);
  } else if (req.method === 'POST' && req.url === '/register') {
    if (!requireAdmin(req, res)) { req.pause(); return; }
    handleBody(async (body) => {
      let room = null;
      let previousAgents = null;
      let previousConfigAgents = null;
      let previousTimeout = null;
      try {
        if (!JITSI_MODE) return res.end(JSON.stringify({ok: false, error: 'modo ' + MODE + ': sin salas Jitsi'}));
        if (PAUSED.jitsi) return res.end(JSON.stringify({ok: false, error: 'jitsi pausado'}));
        const parsed = parseBody(body);
        room = parsed.room;
        const agents = parsed.agents;
        const timeout = parsed.timeout;
        if (!room) return res.end(JSON.stringify({ok: false, error: 'room requerido'}));
        if (!Array.isArray(agents)) return res.end(JSON.stringify({ok: false, error: 'agents debe ser array'}));
        const unknown = agents.filter(a => !agentByName.has(a));
        if (unknown.length) return res.end(JSON.stringify({ok: false, error: 'agentes desconocidos: ' + unknown.join(', ')}));
        previousAgents = roomAgents.has(room) ? [...roomAgents.get(room)] : null;
        previousConfigAgents = CONFIG.roomAgents && Object.prototype.hasOwnProperty.call(CONFIG.roomAgents, room)
          ? [...CONFIG.roomAgents[room]] : null;
        previousTimeout = roomTimeouts.has(room) ? roomTimeouts.get(room) : null;
        // agents=[] = modo broadcast (responder a todos): se borra del Map en
        // runtime, but the empty entry IS PERSISTED in CONFIG so the
        // broadcast sobreviva reinicios (comportamiento intencional).
        if (agents.length === 0) roomAgents.delete(room);
        else roomAgents.set(room, agents);
        CONFIG.roomAgents = CONFIG.roomAgents || {};
        CONFIG.roomAgents[room] = agents;
        if (timeout) {
          await persistRoomTimeout(room, timeout);
        } else {
          // Persist the room-agent change before reporting success.
          await persistConfig();
        }
        console.log('[bridge] /register', room, agents.length ? agents.join(',') : '(broadcast)', timeout ? 'timeout=' + timeout + 'min' : '');
        res.end(JSON.stringify({ok: true, room, agents, broadcast: agents.length === 0, timeout: getTimeoutMin(room)}));
      } catch (e) {
        // Persistence failure must not leave a state in RAM that differs from
        // the on-disk source of truth. Roll the mutation back before replying.
        if (previousAgents) roomAgents.set(room, previousAgents); else roomAgents.delete(room);
        CONFIG.roomAgents = CONFIG.roomAgents || {};
        if (previousConfigAgents) CONFIG.roomAgents[room] = previousConfigAgents;
        else delete CONFIG.roomAgents[room];
        if (previousTimeout !== null) roomTimeouts.set(room, previousTimeout);
        else roomTimeouts.delete(room);
        res.statusCode = e.statusCode || 500;
        res.end(JSON.stringify({ok: false, error: e.message}));
      }
    });
  } else if (req.method === 'POST' && req.url === '/pause') {
    if (!requireAdmin(req, res)) { req.pause(); return; }
    handleBody(async (body) => {
      try {
        const {side, paused} = parseBody(body);
        if (typeof paused !== 'boolean') { res.statusCode = 400; return res.end(JSON.stringify({ok: false, error: 'paused must be boolean'})); }
        const state = setPaused(side, paused);
        console.log('[bridge] pause', side, '=', paused, '(state:', JSON.stringify(state) + ')');
        if (side === 'jitsi' || side === 'both') {
          if (paused && JITSI_MODE) {
            for (const room of [...rooms.keys()]) {
              await leaveRoomFn(room);
              console.log('[bridge] jitsi pause: room', room, 'left');
            }
          }
        }
        res.end(JSON.stringify({ok: true, side, paused, state}));
      } catch (e) { res.statusCode = e.statusCode || 400; res.end(JSON.stringify({ok: false, error: e.message})); }
    });
  } else if (req.method === 'GET' && req.url === '/status') {
    // localhost is a transport binding, not an authorization boundary.
    if (!requireAdmin(req, res)) { req.pause(); return; }
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
  if (!CONFIG.nostr || !CONFIG.nostr.relay || !readSecret(CONFIG.nostr, 'nsec', 'nsecFile', 'Nostr bridge')) {
    console.error('[bridge] incomplete config: missing nostr.relay / nostr.nsec (use "vault:NAME" or "env:VAR")');
    process.exit(1);
  }
  if (JITSI_MODE && (!CONFIG.xmpp || !readSecret(CONFIG.xmpp, 'password', 'passwordFile', 'XMPP'))) {
    console.error('[bridge] mode ' + MODE + ' requires XMPP credentials');
    process.exit(1);
  }

  if (!CONFIG.httpAdminToken && !CONFIG.httpAdminTokenFile && !process.env.PHANTOMBRIDGE_ADMIN_TOKEN) {
    console.error('[http] refusing to start: configure httpAdminToken ("vault:NAME"/"env:VAR") or PHANTOMBRIDGE_ADMIN_TOKEN');
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
  handleRoute,
  buildHelp,
  PAUSED,
  isPaused,
  setPaused,
  antiLoopCheck,
  antiLoopRollback,
  unwrapAndVerifyGiftWrap,
  resolveRid,
  ANTILOOP,
  parseEnvelope,
  envelopeMac,
  stampEnvelope,
  extractRid,
  enqueueGiftWrap,
  pumpNostrQueue,
  recordDropped,
  recoverDropped,
  releasePendingSinceIfRecovered,
  updateLastSeen,   // AUDIT-4/5 🟡: cursor de recepción (lectura para tests del backlog no autorizado)
  markSeen,
  isSeen,
  markRejected,
  isRejected,
  rejectedIds,   // LOW-8: caché efímero de frames rechazados (lectura para tests)
  markDelivery,
  deliveryStatus,
  // AUDIT-M01-BLOCKER2: finishDelivery expuesto para probar el invariante de
  // que un evento rejected (denegado) NO avanza el watermark y que el avance
  // occurs only on the success path (ok=true).
  finishDelivery,
  // AUDIT-10 (root): recovery watermark (advances ONLY with processed/admitted
  // events, not with raw reception). Exposed for tests.
  advanceRecoveryWatermark,
  // AUDIT-M01-OPCION2-FIX: processWatermark(ts) REMOVED. Feeding the cursor
  // from external timestamps (sender's created_at) reintroduces the attack
  // surface; the only legitimate advance is advanceRecoveryWatermark() with a
  // bounded step (never a free jump to Date.now() nor a ts from the wire).
  // AUDIT-5/6: the primitive `let`s are exposed as LIVE GETTERS so tests
  // read the CURRENT value (exporting the primitive by value freezes them).
  get backpressureRejected() { return backpressureRejected; },
  requestDeliveryRescan,  // AUDIT-6: request recovery re-scan (read for tests)
  get deliveryRescanNeeded() { return deliveryRescanNeeded; },
  // AUDIT-8: backoff/rescan-limit state (read for tests)
  get rescanWindowCount() { return rescanWindowCount; },
  get rescanWindowStart() { return rescanWindowStart; },
  get lastRescanAt() { return lastRescanAt; },
  // AUDIT-9: BACKPRESSURE rescan state (read for tests)
  get rescanStalled() { return rescanStalled; },
  get rescanStalledSince() { return rescanStalledSince; },
  get rescanAttempts() { return rescanAttempts; },
  _resetRescanStateForTest: () => { rescanAttempts = 0; rescanWindowStart = 0; rescanWindowCount = 0; lastRescanAt = 0; deliveryRescanNeeded = false; deliveryRescanScheduled = false; rescanStalled = false; rescanStalledSince = 0; },
  flushStateNow,
  STATE_FILE,
  getBridgeState: () => bridgeState,
  // AUDIT-10 (root): recovery watermark (only advances with processed/admitted
  // events, not with raw reception). Read for tests.
  get recoveryWatermark() { return bridgeState ? bridgeState.recoveryWatermark : 0; },
  get lastSeen() { return bridgeState ? bridgeState.lastSeen : 0; },
  // Test-only: seed the module-level state so ALTO-2 regression tests can
  // exercise the real functions without booting a relay. Not used in prod.
  _setBridgeStateForTest: (bs) => { bridgeState = bs; if (bs && bs.delivery) deliverySize = Object.keys(bs.delivery).length; else deliverySize = 0; },
  server,   // HTTP API (para tests: server.listen(0) y fetch)
  getAdminToken, // MEDIO-7: token de admin para que los tests autentiquen los POST
  // AUDIT kaieriksen M01: authorization gate by sender+room for agent-controlled
  // paths (join/leave/inject/recordings). Exposed for tests.
  agentCanOperateRoom,
  buildUntrustedRoomRelayPayload,
  evalRoomPermission, // lógica pura de la matriz de permisos (test aislado)
  // LOW-10: TLS bypass decision (local hosts only) for tests.
  CONFIG,
};
