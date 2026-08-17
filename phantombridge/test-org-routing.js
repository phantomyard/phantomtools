// Tests del mapeo org.yaml → agents + routing DM↔DM (norma v1.6).
// Unit tests de org-routing.js + integración con bridge.js (HTTP /status).
// Uso: node test-org-routing.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');

// ---------------------------------------------------------------------------
// Fixture: estructura de una organización de ejemplo (flow-maps YAML como org.yaml real)
// ---------------------------------------------------------------------------
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
  - { id: paco, role: ceo, npub: npub14jsyt77akpjdl6m70ru805xxqjxtqrajzv9yc59anj4t9uhg4tts72ua3d }
  - { id: pepa, role: chief_of_staff, npub: npub1tplxldx86d3kya4n2t83nzd30r0r4katn9aw06mc6yh0yp2lpcpq0f0gtu }
  - { id: roberto, role: cfo, npub: npub1n63l0sav8ruvpvu7ateqtrsekgv24yu77en00eve8459zec3ed7sq64jq5 }
  - { id: alma, role: project_lead, npub: npub1wufqmlsam5nwvy9elmg9r5myvyahf9rws5pwfyakwzxq5ymkaknqal6420 }
  - { id: elena, role: training_lead, npub: npub1ve9n5gg772q0rn3ujmkssxausv6d7jaszs2aftq3dpu8r29qq4pqtyludf }
escalation_matrix:
  - { from: project_lead, to: chief_of_staff, condition: "bloqueo operativo" }
  - { from: training_lead, to: chief_of_staff, condition: "contenido fuera de alcance" }
  - { from: cfo, to: ceo, condition: "gasto por encima del umbral" }
  - { from: chief_of_staff, to: ceo, condition: "bloqueo no resuelto" }
  - { from: "*", to: ceo, condition: "excepción Category 0" }
`;

// Hex reales de los npubs de AU (derivados con nip19.decode; verificados
// contra los hashes del config.json del bridge en el VPS).
const HEX = {
  paco: 'aca045fbddb064dfeb7e78f877d0c6048cb00fb2130a4c50bd9caab2f2e8aad7',
  pepa: '587e6fb4c7d3636276b352cf1989b178de3adbab997ae7eb78d12ef2055f0e02',
  roberto: '9ea3f7c3ac38f8c0b39eeaf2058e19b218aa939ef666f7e5993d68516711cb7d',
  alma: '77120dfe1ddd26e610b9fed051d364613b74946e8502e493b6708c0a1376eda6',
  elena: '664b3a211ef280f1ce3c96ed081bbc8334df4bb01415d4ac11687871a8a00542',
};

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
t('actor sin npub se omite', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors.push({id: 'fantasma', role: 'ceo'}); // sin npub
  const agents = deriveAgents(org);
  assert.strictEqual(agents.fantasma, undefined);
  assert.strictEqual(Object.keys(agents).length, 5);
});
t('npub inválido se omite (con warning)', () => {
  const org = parseOrgYaml(ORG_AU);
  org.actors.push({id: 'roto', role: 'ceo', npub: 'npub1noesvalido'});
  const agents = deriveAgents(org);
  assert.strictEqual(agents.roto, undefined);
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
t('archivo inexistente → null', () => {
  assert.strictEqual(loadOrgRouting(path.join(TEST_DIR, 'no-existe.yaml')), null);
});
t('yaml roto → null (con warning)', () => {
  const bad = path.join(TEST_DIR, 'roto.yaml');
  fs.writeFileSync(bad, 'roles: [unclosed');
  assert.strictEqual(loadOrgRouting(bad), null);
});

// ---------------------------------------------------------------------------
// Integración con bridge.js: el routing derivado sustituye al manual
// ---------------------------------------------------------------------------
console.log('integración bridge.js:');
function getJson(port, p) {
  return new Promise((resolve, reject) => {
    const req = require('http').get({host: '127.0.0.1', port, path: p}, (res) => {
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
fs.writeFileSync(CFG, JSON.stringify({
  mode: 'nostr',
  nick: 'secretario',
  httpPort: 0, // listen(0) lo asigna el test
  nostr: {relay: 'ws://127.0.0.1:19999', nsec: nip19.nsecEncode(generateSecretKey())},
  orgFile: ORG_FILE,
  // routing manual presente PERO debe ser ignorado (org.yaml manda)
  agents: {extra: getPublicKey(generateSecretKey())},
  routing: {permissions: {extra: ['paco']}, default: 'deny'},
}, null, 2));

t('bridge usa routing derivado de org.yaml, no el manual', async () => {
  delete require.cache[require.resolve('./bridge.js')];
  process.env.PHANTOMBRIDGE_CONFIG = CFG;
  const bridge = require('./bridge.js');
  await new Promise(resolve => bridge.server.listen(0, '127.0.0.1', resolve));
  const port = bridge.server.address().port;
  const status = await getJson(port, '/status');
  assert.deepStrictEqual(status.routing.permissions, EXPECTED_ROUTING);
  assert.strictEqual(status.routing.default, 'deny');
  assert.strictEqual(status.routing.permissions.extra, undefined, 'routing manual ignorado');
  // agents: los del config.json complementan a los derivados
  assert.ok(status.agents.extra, 'agent extra del config mergeado');
  assert.ok(status.agents.paco, 'agent derivado presente');
  await new Promise(resolve => bridge.server.close(resolve));
});

console.log(`\n${passed} passed, ${failed} failed`);
fs.rmSync(TEST_DIR, {recursive: true, force: true});
process.exit(failed ? 1 : 0);
