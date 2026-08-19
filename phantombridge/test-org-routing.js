process.umask(0o077);
// Tests del mapeo org.yaml → agents + routing DM↔DM (norma v1.6).
// Unit tests de org-routing.js + integración con bridge.js (HTTP /status).
// Uso: node test-org-routing.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');

// ---------------------------------------------------------------------------
// Fixture: estructura de Aquaponics United (flow-maps YAML como org.yaml real)
// ---------------------------------------------------------------------------
const HEX = {
  paco: '1111111111111111111111111111111111111111111111111111111111111111',
  pepa: '2222222222222222222222222222222222222222222222222222222222222222',
  roberto: '3333333333333333333333333333333333333333333333333333333333333333',
  alma: '4444444444444444444444444444444444444444444444444444444444444444',
  elena: '5555555555555555555555555555555555555555555555555555555555555555',
};
const FIXTURE_NPUBS = [
  HEX.paco, HEX.pepa, HEX.roberto, HEX.alma, HEX.elena
].map(pk => nip19.npubEncode(pk));


const ORG_AU = `
version: 1
organization:
  id: aquaponics-united
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
  - { id: paco, role: ceo, npub: ${FIXTURE_NPUBS[0]} }
  - { id: pepa, role: chief_of_staff, npub: ${FIXTURE_NPUBS[1]} }
  - { id: roberto, role: cfo, npub: ${FIXTURE_NPUBS[2]} }
  - { id: alma, role: project_lead, npub: ${FIXTURE_NPUBS[3]} }
  - { id: elena, role: training_lead, npub: ${FIXTURE_NPUBS[4]} }
escalation_matrix:
  - { from: project_lead, to: chief_of_staff, condition: "bloqueo operativo" }
  - { from: training_lead, to: chief_of_staff, condition: "contenido fuera de alcance" }
  - { from: cfo, to: ceo, condition: "gasto por encima del umbral" }
  - { from: chief_of_staff, to: ceo, condition: "bloqueo no resuelto" }
  - { from: "*", to: ceo, condition: "excepción Category 0" }
`;

// Resultado esperado de deriveRouting(ORG_AU):
//   reports_to bidireccional: paco↔pepa, paco↔roberto, pepa↔alma, pepa↔elena
//   escalation *→ceo: alma→paco, elena→paco (direccional)
const EXPECTED_ROUTING = {
  paco: ['pepa', 'roberto'],
  pepa: ['alma', 'elena', 'paco'],
  roberto: ['paco'],
  alma: ['paco', 'pepa'],
  elena: ['paco', 'pepa'],
};

const {parseOrgYaml, deriveAgents, deriveRouting, loadOrgRouting} = require('./org-routing.js');

let passed = 0, failed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('  ok:', name); }
  catch (e) { failed++; console.error('  FAIL:', name, '-', e.message); }
}

// ---------------------------------------------------------------------------
// parseOrgYaml
// ---------------------------------------------------------------------------
console.log('parseOrgYaml:');
t('flow-maps YAML parsean', () => {
  const org = parseOrgYaml(ORG_AU);
  assert.strictEqual(org.roles.length, 5);
  assert.strictEqual(org.roles[1].reports_to, 'ceo');
  assert.strictEqual(org.escalation_matrix[4].from, '*');
});
t('yaml inválido lanza', () => {
  assert.throws(() => parseOrgYaml('a: [unclosed'));
});

// ---------------------------------------------------------------------------
// deriveAgents
// ---------------------------------------------------------------------------
console.log('deriveAgents:');
t('npubs reales de AU → hex exacto', () => {
  const agents = deriveAgents(parseOrgYaml(ORG_AU));
  assert.deepStrictEqual(agents, HEX);
});
t('actor sin npub falla cerrado', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors.push({id: 'fantasma', role: 'ceo'});
  assert.throws(() => deriveAgents(org), /requiere id, role y npub/);
});
t('npub inválido falla cerrado', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors.push({id: 'roto', role: 'ceo', npub: 'npub1noesvalido'});
  assert.throws(() => deriveAgents(org), /npub inválido/);
});

// MEDIO-6: FAIL-CLOSED ante identidades ambiguas (pubkey/actor.id duplicados).
// MEDIO-6: FAIL-CLOSED ante identidades ambiguas (pubkey/actor.id duplicados).
function freshNpub() { return nip19.npubEncode(getPublicKey(generateSecretKey())); }
t('actor.id duplicado → FAIL-CLOSED (lanza)', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors = [
    {id: 'doble', role: 'ceo', npub: freshNpub()},
    {id: 'doble', role: 'worker', npub: freshNpub()},
  ];
  assert.throws(() => deriveAgents(org), /duplicado/i);
});
t('pubkey duplicado entre actores → FAIL-CLOSED (lanza)', () => {
  const shared = freshNpub();
  const org = parseOrgYaml(ORG_AU);
  org.actors = [
    {id: 'ceoX', role: 'ceo', npub: shared},
    {id: 'workerX', role: 'worker', npub: shared},
  ];
  assert.throws(() => deriveAgents(org), /duplicado/i);
});
t('loadOrgRouting ante pubkey duplicado → EINVALID (fail-closed, no fallback)', () => {
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
t('jerarquía AU → routing esperado', () => {
  const routing = deriveRouting(parseOrgYaml(ORG_AU));
  assert.deepStrictEqual(routing.permissions, EXPECTED_ROUTING);
  assert.strictEqual(routing.default, 'deny');
});
t('reports_to es bidireccional (paco↔roberto)', () => {
  const {permissions} = deriveRouting(parseOrgYaml(ORG_AU));
  assert.ok(permissions.paco.includes('roberto'));
  assert.ok(permissions.roberto.includes('paco'));
});
t('escalada *→ceo es direccional (alma→paco, no paco→alma)', () => {
  const {permissions} = deriveRouting(parseOrgYaml(ORG_AU));
  assert.ok(permissions.alma.includes('paco'));
  assert.ok(!permissions.paco.includes('alma'));
});
t('sin regla explícita → no hay arista (roberto→elena)', () => {
  const {permissions} = deriveRouting(parseOrgYaml(ORG_AU));
  assert.ok(!permissions.roberto.includes('elena'));
});
t('multi-actor por rol: todos los actores del rol reciben la arista', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors.push({id: 'ayudante', role: 'project_lead', npub: nip19.npubEncode(getPublicKey(generateSecretKey()))});
  const {permissions} = deriveRouting(org);
  assert.ok(permissions.ayudante.includes('pepa')); // project_lead reporta a chief_of_staff
  assert.ok(permissions.pepa.includes('ayudante'));
});
t('sin roles/actors → vacío con default deny', () => {
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
const ORG_FILE = path.join(TEST_DIR, 'org-au.yaml');
fs.writeFileSync(ORG_FILE, ORG_AU);

t('archivo válido → {agents, routing}', () => {
  const r = loadOrgRouting(ORG_FILE);
  assert.deepStrictEqual(r.agents, HEX);
  assert.deepStrictEqual(r.routing.permissions, EXPECTED_ROUTING);
});
t('archivo inexistente → EMISSING (legacy fallback legítimo)', () => {
  let err = null;
  try { loadOrgRouting(path.join(TEST_DIR, 'no-existe.yaml')); }
  catch (e) { err = e; }
  assert.ok(err && err.code === 'EMISSING', 'debe lanzar Error con code EMISSING');
});
t('yaml roto → EINVALID (FAIL-CLOSED, no fallback silencioso)', () => {
  const bad = path.join(TEST_DIR, 'roto.yaml');
  fs.writeFileSync(bad, 'roles: [unclosed');
  let err = null;
  try { loadOrgRouting(bad); }
  catch (e) { err = e; }
  assert.ok(err && err.code === 'EINVALID', 'debe lanzar Error con code EINVALID');
});

// ---------------------------------------------------------------------------
// Integración con bridge.js: el routing derivado sustituye al manual
// ---------------------------------------------------------------------------
console.log('integración bridge.js:');
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
  httpPort: 0, // listen(0) lo asigna el test
  nostr: {relay: 'ws://127.0.0.1:19999', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC'},
  orgFile: ORG_FILE,
  // routing manual presente PERO debe ser ignorado (org.yaml manda)
  agents: {extra: getPublicKey(generateSecretKey())},
  routing: {permissions: {extra: ['paco']}, default: 'deny'},
}, null, 2));
fs.chmodSync(CFG, 0o600);

t('bridge usa routing derivado de org.yaml, no el manual', async () => {
  delete require.cache[require.resolve('./bridge.js')];
  process.env.PHANTOMBRIDGE_CONFIG = CFG;
  const bridge = require('./bridge.js');
  await new Promise(resolve => bridge.server.listen(0, '127.0.0.1', resolve));
  const port = bridge.server.address().port;
  const status = await getJson(port, '/status', bridge.getAdminToken());
  assert.deepStrictEqual(status.routing.permissions, EXPECTED_ROUTING);
  assert.strictEqual(status.routing.default, 'deny');
  assert.strictEqual(status.routing.permissions.extra, undefined, 'routing manual ignorado');
  // MEDIO-5: agents del config NO se complementan — org.yaml es la ÚNICA
  // fuente de verdad de identidad. Un 'extra' del config NO debe aparecer.
  assert.strictEqual(status.agents.extra, undefined, 'agent extra del config IGNORADO (org.yaml manda)');
  assert.ok(status.agents.paco, 'agent derivado presente');
  await new Promise(resolve => bridge.server.close(resolve));
});

console.log(`\n${passed} passed, ${failed} failed`);
fs.rmSync(TEST_DIR, {recursive: true, force: true});
process.exit(failed ? 1 : 0);
