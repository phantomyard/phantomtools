#!/usr/bin/env node
// PhantomBridge — org.yaml hierarchy → bridge routing (source of truth).
//
// The ecosystem norm (v1.6): the org.yaml compiled by PhantomOrg is the
// SOURCE OF TRUTH for the organization hierarchy (roles.reports_to +
// escalation_matrix). This module translates that hierarchy into the
// bridge's DM↔DM routing so that the structure defined in PhantomOrg is
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
// If org.yaml is missing, the bridge may use legacy manual routing. If the file
// exists but is malformed or uses an unsupported schema version, startup fails
// closed; malformed source-of-truth data must never widen access.

const fs = require('fs');
const yaml = require('js-yaml');
const {nip19} = require('nostr-tools');

// Parse a YAML document (js-yaml). Throws on malformed input.
const ORG_SCHEMA_VERSION = 1;

function parseOrgYaml(text) {
  const doc = yaml.load(text);
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) {
    throw new Error('org.yaml vacío o no es un mapa');
  }
  if (doc.version !== ORG_SCHEMA_VERSION) {
    throw new Error(
      'org.yaml version incompatible: se requiere version: ' + ORG_SCHEMA_VERSION +
      ' y se recibió ' + JSON.stringify(doc.version)
    );
  }
  if (!Array.isArray(doc.roles) || !Array.isArray(doc.actors) || !Array.isArray(doc.escalation_matrix)) {
    throw new Error('org.yaml incompleto: roles, actors y escalation_matrix deben ser arrays');
  }
  return doc;
}

// org.actors: [{id, role, npub, ...}] → {id: hexPubkey}
// Every actor must have a valid, unique identity; malformed entries fail closed.
function deriveAgents(org) {
  const agents = {};
  const seenPubkey = new Set();
  const seenActor = new Set();
  for (const actor of org.actors) {
    if (!actor || typeof actor !== 'object' || !actor.id || !actor.role || !actor.npub) {
      throw new Error('actor inválido en org.yaml: cada actor requiere id, role y npub (FAIL-CLOSED)');
    }
    let decoded;
    try {
      decoded = nip19.decode(actor.npub);
    } catch (e) {
      throw new Error('npub inválido para actor "' + actor.id + '": ' + e.message);
    }
    if (decoded.type !== 'npub') {
      throw new Error('"' + actor.npub + '" no es un npub (actor ' + actor.id + ')');
    }
    const hex = typeof decoded.data === 'string' ? decoded.data : Buffer.from(decoded.data).toString('hex');
    if (!/^[0-9a-f]{64}$/i.test(hex)) {
      throw new Error('pubkey inválido para actor "' + actor.id + '"');
    }
    const pub = hex.toLowerCase();
    if (seenActor.has(actor.id)) {
      throw new Error('actor.id duplicado en org.yaml: "' + actor.id + '" (identidad ambigua, FAIL-CLOSED)');
    }
    if (seenPubkey.has(pub)) {
      throw new Error('pubkey duplicado en org.yaml (hex ' + pub.slice(0, 8) + '...) compartido por más de un actor; identidad ambigua, FAIL-CLOSED');
    }
    seenActor.add(actor.id);
    seenPubkey.add(pub);
    agents[actor.id] = pub;
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
function validateOrgReferences(org) {
  const roleIds = new Set();
  for (const role of org.roles) {
    if (!role || typeof role !== 'object' || !role.id) {
      throw new Error('role inválido en org.yaml: falta id');
    }
    if (roleIds.has(role.id)) throw new Error('role.id duplicado en org.yaml: "' + role.id + '"');
    roleIds.add(role.id);
  }

  for (const actor of org.actors) {
    if (!roleIds.has(actor.role)) {
      throw new Error('actor "' + actor.id + '" referencia role inexistente "' + actor.role + '"');
    }
  }

  for (const role of org.roles) {
    if (role.reports_to !== undefined && role.reports_to !== null && !roleIds.has(role.reports_to)) {
      throw new Error('role "' + role.id + '" reports_to inexistente "' + role.reports_to + '"');
    }
  }

  for (const esc of org.escalation_matrix) {
    if (!esc || typeof esc !== 'object' || !esc.from || !esc.to) {
      throw new Error('entrada inválida en escalation_matrix: se requieren from y to');
    }
    if (esc.from !== '*' && !roleIds.has(esc.from)) {
      throw new Error('escalation_matrix.from referencia role inexistente "' + esc.from + '"');
    }
    if (!roleIds.has(esc.to)) {
      throw new Error('escalation_matrix.to referencia role inexistente "' + esc.to + '"');
    }
  }
}

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

// Load + derive from orgFile.
// Returns {agents, routing} on success, or throws a descriptive Error:
//  - org.yaml AUSENTE  -> Error with .code='EMISSING' (legacy fallback to manual
//    config.json routing is legitimate when the file simply isn't deployed).
//  - org.yaml PRESENTE pero inválido/roto -> Error with .code='EINVALID'.
//    This is FAIL-CLOSED: an invalid source of truth must NOT silently fall
//    back to the (possibly permissive/obsolete) manual routing, because that
//    would deviate from the normative org.yaml hierarchy. Callers decide how
//    to handle each code.
function loadOrgRouting(orgFile) {
  let text;
  let readErr;
  try {
    text = fs.readFileSync(orgFile, 'utf8');
  } catch (e) {
    readErr = e;
  }
  if (readErr) {
    const missing = readErr.code === 'ENOENT';
    const err = new Error(
      missing
        ? 'org.yaml no encontrado (' + orgFile + ')'
        : 'org.yaml ilegible (' + orgFile + '): ' + readErr.message
    );
    err.code = missing ? 'EMISSING' : 'EINVALID';
    throw err;
  }
  try {
    const org = parseOrgYaml(text);
    validateOrgReferences(org);
    return {
      agents: deriveAgents(org),
      routing: deriveRouting(org),
    };
  } catch (e) {
    // FAIL-CLOSED: the file exists but cannot be parsed/derived. Surface this
    // loudly instead of silently falling back to manual routing.
    const err = new Error('org.yaml inválido (' + orgFile + '): ' + e.message);
    err.code = 'EINVALID';
    throw err;
  }
}

module.exports = {ORG_SCHEMA_VERSION, parseOrgYaml, validateOrgReferences, deriveAgents, deriveRouting, actorsByRole, loadOrgRouting};
