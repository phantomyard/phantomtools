// Relay reachability diagnostic (diagnostic-only, fail-quiet).
//
// The canonical relay list is maintained in ONE place: the static file the
// PhantomChat PWA serves at https://chat.phantomyard.ai/relays.json. phantombot
// fetches that same URL on startup, so where a persona can RECEIVE is decided
// by that file — not by this bridge, and not by config.json.
//
// This bridge publishes DMs to `nostr.relay` only. When that relay is absent
// from the list the personas actually resolve, every routed DM is published on
// a relay nobody subscribed to: the routing log stays healthy
// (`[routing] alice -> bob`) while the recipient never sees the message. That
// silent failure is what this diagnostic exists to break.
//
// It never changes the relay set, never blocks startup, and stays quiet
// whenever the list cannot be fetched (offline, 404, malformed).
'use strict';

const DEFAULT_CANONICAL_RELAYS_URL = 'https://chat.phantomyard.ai/relays.json';
const DEFAULT_TIMEOUT_MS = 5000;

// The URL the personas use. `PHANTOMCHAT_RELAYS_URL` is phantombot's own
// override, so a deployment that points its personas at a mirror must be
// checked against that mirror — not against the public default.
function canonicalRelaysUrl(env) {
  const raw = (((env || process.env).PHANTOMCHAT_RELAYS_URL) || '').trim();
  return raw || DEFAULT_CANONICAL_RELAYS_URL;
}

// Same acceptance rule phantombot applies to the fetched list.
function validRelays(value) {
  if (!Array.isArray(value)) return [];
  return value.filter((u) => typeof u === 'string' && (u.startsWith('wss://') || u.startsWith('ws://')));
}

// Best-effort read: returns the fetched relay URLs, or null on ANY failure
// (network error, non-200, malformed body, nothing usable after validation).
async function fetchCanonicalRelays(options = {}) {
  const url = options.url || canonicalRelaysUrl(options.env);
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : DEFAULT_TIMEOUT_MS;
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  if (typeof fetchImpl !== 'function') return null;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetchImpl(url, {signal: ac.signal});
    if (!res || !res.ok) return null;
    const body = await res.json();
    const relays = validRelays(body && body.relays);
    return relays.length > 0 ? relays : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// Warn when `relay` (the relay this bridge publishes to) is missing from the
// list the agents read. Returns {checked, present?}; never throws.
async function warnIfRelayNotCanonical(relay, options = {}) {
  const list = await fetchCanonicalRelays(options);
  if (!list) return {checked: false};
  if (list.includes(relay)) return {checked: true, present: true};
  const url = options.url || canonicalRelaysUrl(options.env);
  const warn = options.warn || console.warn;
  warn('[nostr] WARNING: configured relay ' + relay + ' is NOT in the canonical relay list (' + url + ').');
  warn('[nostr] WARNING: personas resolve their relays from that list, so every DM routed here is published on a relay they never read — routed, but silently never delivered.');
  warn('[nostr] WARNING: serve a list that includes this relay and point each persona at it (PHANTOMCHAT_RELAYS_URL), then restart the personas.');
  return {checked: true, present: false};
}

module.exports = {
  DEFAULT_CANONICAL_RELAYS_URL,
  DEFAULT_TIMEOUT_MS,
  canonicalRelaysUrl,
  validRelays,
  fetchCanonicalRelays,
  warnIfRelayNotCanonical,
};
