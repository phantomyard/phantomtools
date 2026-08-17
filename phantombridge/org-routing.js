#!/usr/bin/env node
// PhantomBridge — org.yaml hierarchy → bridge routing (source of truth).
//
// The ecosystem norm (v1.6): the org.yaml compiled by PhantomForge is the
// SOURCE OF TRUTH for the organization hierarchy (roles.reports_to +
// escalation_matrix). This module translates that hierarchy into the
// bridge's DM↔DM routing so that the structure defined in PhantomForge is
// replicated in bot↔bot communications.
//
// Derivation rules:
//   1. roles.reports_to  → BIDIRECTIONAL edges between each actor and every
//      actor holding the boss role (a manager must be able to talk to their
//      reports and vice versa).
//   2. escalation_matrix → DIRECTIONAL edges from→to (the escalator talks to
//      the escalation target). A "*" from means every actor may escalate to
//      that role (e.g. "*"→ceo = everyone can escalate to the CEO).
//   3. default: "deny" — pairs without an explicit rule cannot talk.
//
// If org.yaml is missing or unparseable, loadOrgRouting() returns null and
// the bridge falls back to the manual config.json routing (legacy behavior).

const fs = require('fs');
const yaml = require('js-yaml');
const {nip19} = require('nostr-tools');

// Parse a YAML document (js-yaml). Throws on malformed input.
function parseOrgYaml(text) {
  const doc = yaml.load(text);
  if (!doc || typeof doc !== 'object') {
    throw new Error('org.yaml vacío o no es un mapa');
  }
  return doc;
}

// org.actors: [{id, role, npub, ...}] → {id: hexPubkey}
// Actors without a valid npub are skipped (warning printed).
function deriveAgents(org) {
  const agents = {};
  for (const actor of (org.actors || [])) {
    if (!actor || !actor.id || !actor.npub) continue;
    let decoded;
    try {
      decoded = nip19.decode(actor.npub);
    } catch (e) {
      console.warn('[org-routing] npub inválido para actor "' + actor.id + '":', e.message);
      continue;
    }
    if (decoded.type !== 'npub') {
      console.warn('[org-routing] "' + actor.npub + '" no es un npub (actor ' + actor.id + '); se omite.');
      continue;
    }
    // nostr-tools v2 returns the hex pubkey as a string (64 chars).
    const hex = typeof decoded.data === 'string' ? decoded.data : Buffer.from(decoded.data).toString('hex');
    if (!/^[0-9a-f]{64}$/i.test(hex)) {
      console.warn('[org-routing] pubkey inválido para actor "' + actor.id + '"; se omite.');
      continue;
    }
    agents[actor.id] = hex.toLowerCase();
  }
  return agents;
}

// roleId -> [actorId]
function actorsByRole(org) {
  const map = {};
  for (const actor of (org.actors || [])) {
    if (!actor || !actor.id || !actor.role) continue;
    (map[actor.role] = map[actor.role] || []).push(actor.id);
  }
  return map;
}

function addEdge(perms, from, to) {
  if (!from || !to || from === to) return;
  (perms[from] = perms[from] || []);
  if (!perms[from].includes(to)) perms[from].push(to);
}

// org → {permissions: {from: [to, ...]}, default: "deny"}
function deriveRouting(org) {
  const perms = {};
  const byRole = actorsByRole(org);
  const roles = org.roles || [];

  // 1. reports_to → bidirectional (manager ↔ report)
  for (const actor of (org.actors || [])) {
    if (!actor || !actor.id || !actor.role) continue;
    const role = roles.find(r => r && r.id === actor.role);
    if (!role || !role.reports_to) continue;
    const bosses = byRole[role.reports_to] || [];
    for (const boss of bosses) {
      addEdge(perms, actor.id, boss);
      addEdge(perms, boss, actor.id);
    }
  }

  // 2. escalation_matrix → directional (escalator → target; "*" = everyone)
  for (const esc of (org.escalation_matrix || [])) {
    if (!esc || !esc.to) continue;
    const targets = byRole[esc.to] || [];
    if (esc.from === '*') {
      for (const actor of (org.actors || [])) {
        if (!actor || !actor.id) continue;
        for (const t of targets) addEdge(perms, actor.id, t);
      }
    } else {
      const sources = byRole[esc.from] || [];
      for (const s of sources) {
        for (const t of targets) addEdge(perms, s, t);
      }
    }
  }

  // Deterministic output (sorted keys and values).
  const permissions = {};
  for (const from of Object.keys(perms).sort()) {
    permissions[from] = perms[from].sort();
  }
  return {permissions, default: 'deny'};
}

// Load + derive from orgFile. Returns {agents, routing} or null on any failure
// (missing file or parse error) so the caller can fall back to manual config.
function loadOrgRouting(orgFile) {
  let text;
  try {
    text = fs.readFileSync(orgFile, 'utf8');
  } catch (e) {
    return null;
  }
  try {
    const org = parseOrgYaml(text);
    return {
      agents: deriveAgents(org),
      routing: deriveRouting(org),
    };
  } catch (e) {
    console.warn('[org-routing] org.yaml inválido (' + orgFile + '): ' + e.message + ' — fallback a routing manual.');
    return null;
  }
}

module.exports = {parseOrgYaml, deriveAgents, deriveRouting, actorsByRole, loadOrgRouting};
