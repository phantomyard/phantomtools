process.umask(0o077);
// Tests of the org.yaml → agents + routing DM↔DM mapping (norm v1.6).
// Unit tests of org-routing.js + integration with bridge.js (HTTP /status).
// Usage: node test-org-routing.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');

// ---------------------------------------------------------------------------
// Fixture: estructura de Example Org (flow-maps YAML como org.yaml real)
// ---------------------------------------------------------------------------
const HEX = {
  alice: '1111111111111111111111111111111111111111111111111111111111111111',
  bob: '2222222222222222222222222222222222222222222222222222222222222222',
  carol: '3333333333333333333333333333333333333333333333333333333333333333',
  dave: '4444444444444444444444444444444444444444444444444444444444444444',
  erin: '5555555555555555555555555555555555555555555555555555555555555555',
};
const FIXTURE_NPUBS = [
  HEX.alice, HEX.bob, HEX.carol, HEX.dave, HEX.erin
].map(pk => nip19.npubEncode(pk));


const ORG_AU = `
version: 1
organization:
  id: example-org
departments:
  - { id: direccion, name: "Dirección", parent: null }
  - { id: operaciones, name: "Operaciones", parent: direccion }
  - { id: formacion, name: "Formación", parent: direccion }
  - { id: finanzas, name: "Finanzas", parent: direccion }
roles:
  - { id: ceo, name: "CEO", department: direccion, reports_to: null }
  - { id: chief_of_staff, name: "Chief of Staff", department: direccion, reports_to: ceo }
  - { id: cfo, name: "CFO", department: finanzas, reports_to: ceo }
  - { id: project_lead, name: "Project Lead", department: operaciones, reports_to: chief_of_staff }
  - { id: training_lead, name: "Training Lead", department: formacion, reports_to: chief_of_staff }
actors:
  - { id: alice, role: ceo, npub: ${FIXTURE_NPUBS[0]} }
  - { id: bob, role: chief_of_staff, npub: ${FIXTURE_NPUBS[1]} }
  - { id: carol, role: cfo, npub: ${FIXTURE_NPUBS[2]} }
  - { id: dave, role: project_lead, npub: ${FIXTURE_NPUBS[3]} }
  - { id: erin, role: training_lead, npub: ${FIXTURE_NPUBS[4]} }
escalation_matrix:
  - { from: project_lead, to: chief_of_staff, condition: "bloqueo operativo" }
  - { from: training_lead, to: chief_of_staff, condition: "contenido fuera de alcance" }
  - { from: cfo, to: ceo, condition: "gasto por encima del umbral" }
  - { from: chief_of_staff, to: ceo, condition: "bloqueo no resuelto" }
  - { from: "*", to: ceo, condition: "excepción Category 0" }
`;

// Expected result of deriveRouting(ORG_AU):
//   reports_to bidirectional: alice↔bob, alice↔carol, bob↔dave, bob↔erin
//   escalation *→ceo: dave→alice, erin→alice (directional)
const EXPECTED_ROUTING = {
  alice: ['bob', 'carol'],
  bob: ['alice', 'dave', 'erin'],
  carol: ['alice'],
  dave: ['alice', 'bob'],
  erin: ['alice', 'bob'],
};

const {parseOrgYaml, deriveAgents, deriveRouting, loadOrgRouting} = require('./org-routing.js');

let passed = 0, failed = 0;
let _chain = Promise.resolve();
function t(name, fn) {
  _chain = _chain.then(async () => {
    try { await fn(); passed++; console.log('  ok:', name); }
    catch (e) { failed++; console.error('  FAIL:', name, '-', e.message); }
  });
}

// ---------------------------------------------------------------------------
// parseOrgYaml
// ---------------------------------------------------------------------------
console.log('parseOrgYaml:');
t('flow-maps YAML parse', () => {
  const org = parseOrgYaml(ORG_AU);
  assert.strictEqual(org.roles.length, 5);
  assert.strictEqual(org.roles[1].reports_to, 'ceo');
  assert.strictEqual(org.escalation_matrix[4].from, '*');
});
t('invalid yaml throws', () => {
  assert.throws(() => parseOrgYaml('a: [unclosed'));
});

// ---------------------------------------------------------------------------
// deriveAgents
// ---------------------------------------------------------------------------
console.log('deriveAgents:');
t('fictional org npubs -> exact hex', () => {
  const agents = deriveAgents(parseOrgYaml(ORG_AU));
  assert.deepStrictEqual(agents, HEX);
});
t('actor without npub fails closed', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors.push({id: 'fantasma', role: 'ceo'});
  assert.throws(() => deriveAgents(org), /requires id, role and npub/);
});
t('invalid npub fails closed', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors.push({id: 'roto', role: 'ceo', npub: 'npub1noesvalido'});
  assert.throws(() => deriveAgents(org), /invalid npub/);
});

// MEDIO-6: FAIL-CLOSED on ambiguous identities (duplicated pubkey/actor.id).
// MEDIO-6: FAIL-CLOSED on ambiguous identities (duplicated pubkey/actor.id).
function freshNpub() { return nip19.npubEncode(getPublicKey(generateSecretKey())); }
t('duplicated actor.id -> FAIL-CLOSED (throws)', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors = [
    {id: 'doble', role: 'ceo', npub: freshNpub()},
    {id: 'doble', role: 'worker', npub: freshNpub()},
  ];
  assert.throws(() => deriveAgents(org), /duplicate/i);
});
t('duplicated pubkey between actors -> FAIL-CLOSED (throws)', () => {
  const shared = freshNpub();
  const org = parseOrgYaml(ORG_AU);
  org.actors = [
    {id: 'ceoX', role: 'ceo', npub: shared},
    {id: 'workerX', role: 'worker', npub: shared},
  ];
  assert.throws(() => deriveAgents(org), /duplicate/i);
});
t('loadOrgRouting with duplicated pubkey -> EINVALID (fail-closed, no fallback)', () => {
  const shared = freshNpub();
  const yaml = 'actors:\n  - id: a\n    role: ceo\n    npub: ' + shared +
    '\n  - id: b\n    role: worker\n    npub: ' + shared + '\n';
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'org-dup-'));
  const file = path.join(tmpDir, 'dup-org.yaml');
  fs.writeFileSync(file, yaml);
  try {
    assert.throws(() => loadOrgRouting(file), (e) => { assert.strictEqual(e.code, 'EINVALID'); return true; });
  } finally {
    fs.rmSync(tmpDir, {recursive: true, force: true});
  }
});

// ---------------------------------------------------------------------------
// deriveRouting
// ---------------------------------------------------------------------------
console.log('deriveRouting:');
t('org hierarchy -> expected routing', () => {
  const routing = deriveRouting(parseOrgYaml(ORG_AU));
  assert.deepStrictEqual(routing.permissions, EXPECTED_ROUTING);
  assert.strictEqual(routing.default, 'deny');
});
t('reports_to is bidirectional (alice↔carol)', () => {
  const {permissions} = deriveRouting(parseOrgYaml(ORG_AU));
  assert.ok(permissions.alice.includes('carol'));
  assert.ok(permissions.carol.includes('alice'));
});
t('escalation *→ceo is directional (dave→alice, not alice→dave)', () => {
  const {permissions} = deriveRouting(parseOrgYaml(ORG_AU));
  assert.ok(permissions.dave.includes('alice'));
  assert.ok(!permissions.alice.includes('dave'));
});
t('no explicit rule -> no edge (carol→erin)', () => {
  const {permissions} = deriveRouting(parseOrgYaml(ORG_AU));
  assert.ok(!permissions.carol.includes('erin'));
});
t('multi-actor per role: all role actors receive the edge', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors.push({id: 'ayudante', role: 'project_lead', npub: nip19.npubEncode(getPublicKey(generateSecretKey()))});
  const {permissions} = deriveRouting(org);
  assert.ok(permissions.ayudante.includes('bob')); // project_lead reports to chief_of_staff
  assert.ok(permissions.bob.includes('ayudante'));
});
t('no roles/actors -> empty with default deny', () => {
  const routing = deriveRouting({roles: [], actors: [], escalation_matrix: []});
  assert.deepStrictEqual(routing.permissions, {});
  assert.strictEqual(routing.default, 'deny');
});

// ---------------------------------------------------------------------------
// loadOrgRouting
// ---------------------------------------------------------------------------
console.log('loadOrgRouting:');
const TEST_DIR = path.join(__dirname, '.test-tmp');
fs.mkdirSync(TEST_DIR, {recursive: true});
const ORG_FILE = path.join(TEST_DIR, 'org-example.yaml');
fs.writeFileSync(ORG_FILE, ORG_AU);

t('valid file -> {agents, routing}', () => {
  const r = loadOrgRouting(ORG_FILE);
  assert.deepStrictEqual(r.agents, HEX);
  assert.deepStrictEqual(r.routing.permissions, EXPECTED_ROUTING);
});
t('missing file -> EMISSING (legitimate legacy fallback)', () => {
  let err = null;
  try { loadOrgRouting(path.join(TEST_DIR, 'no-existe.yaml')); }
  catch (e) { err = e; }
  assert.ok(err && err.code === 'EMISSING', 'must throw Error with code EMISSING');
});
t('broken yaml -> EINVALID (FAIL-CLOSED, no silent fallback)', () => {
  const bad = path.join(TEST_DIR, 'roto.yaml');
  fs.writeFileSync(bad, 'roles: [unclosed');
  let err = null;
  try { loadOrgRouting(bad); }
  catch (e) { err = e; }
  assert.ok(err && err.code === 'EINVALID', 'must throw Error with code EINVALID');
});

// ---------------------------------------------------------------------------
// Integration with bridge.js: the derived routing replaces the manual one
// ---------------------------------------------------------------------------
console.log('bridge.js integration:');
function getJson(port, p, token) {
  return new Promise((resolve, reject) => {
    const req = require('http').get({
      host: '127.0.0.1',
      port,
      path: p,
      headers: {authorization: 'Bearer ' + token},
    }, (res) => {
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
  });
}

const CFG = path.join(TEST_DIR, 'config-org.json');
process.env.PHANTOMBRIDGE_TEST_NSEC = nip19.nsecEncode(generateSecretKey());
fs.writeFileSync(CFG, JSON.stringify({
  mode: 'nostr',
  nick: 'secretario',
  httpPort: 0, // listen(0) assigns it in the test
  nostr: {relay: 'ws://127.0.0.1:19999', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'},
  orgFile: ORG_FILE,
  // manual routing present BUT it must be ignored (org.yaml rules)
  agents: {extra: getPublicKey(generateSecretKey())},
  routing: {permissions: {extra: ['alice']}, default: 'deny'},
}, null, 2));
fs.chmodSync(CFG, 0o600);

t('bridge uses the routing derived from org.yaml, not the manual one', async () => {
  delete require.cache[require.resolve('./bridge.js')];
  process.env.PHANTOMBRIDGE_CONFIG = CFG;
  const bridge = require('./bridge.js');
  await new Promise(resolve => bridge.server.listen(0, '127.0.0.1', resolve));
  const port = bridge.server.address().port;
  const status = await getJson(port, '/status', bridge.getAdminToken());
  assert.deepStrictEqual(status.routing.permissions, EXPECTED_ROUTING);
  assert.strictEqual(status.routing.default, 'deny');
  assert.strictEqual(status.routing.permissions.extra, undefined, 'manual routing ignored');
  // MEDIO-5: config agents are NOT complemented — org.yaml is the ONLY
  // identity source of truth. A config 'extra' must NOT appear.
  assert.strictEqual(status.agents.extra, undefined, 'config extra agent IGNORED (org.yaml rules)');
  assert.ok(status.agents.alice, 'derived agent present');
  await new Promise(resolve => bridge.server.close(resolve));
});

_chain.then(() => {
  console.log(`\n${passed} passed, ${failed} failed`);
  fs.rmSync(TEST_DIR, {recursive: true, force: true});
  process.exit(failed ? 1 : 0);
}).catch((e) => { console.error('FATAL:', e && e.message); process.exit(1); });
