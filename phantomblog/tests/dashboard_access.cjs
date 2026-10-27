'use strict';
// Execute the real dashboard bootstrap in a minimal browser surface.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');

async function scenario(valid) {
  const code = 'synthetic-fragment-credential';
  const requests = [];
  const actions = [];
  const elements = new Map();
  const location = {hash: '#c=' + code, pathname: '/'};
  const context = {
    URLSearchParams, setTimeout: () => 1, clearTimeout: () => {},
    window: {location, history: {replaceState: (_, __, path) => {
      assert.equal(path, '/'); location.hash = ''; actions.push('clean');
    }}, addEventListener: () => {}},
    document: {
      querySelector: id => {
        if (!elements.has(id)) elements.set(id, {hidden: false, textContent: '',
          classList: {add: () => {}, remove: () => {}}, addEventListener: () => {}});
        return elements.get(id);
      }, querySelectorAll: () => []
    },
    fetch: async (url, options) => {
      assert.equal(location.hash, '');
      assert.ok(!url.includes(code));
      actions.push('fetch'); requests.push({url, options});
      if (url === '/api/redeem') return {ok: valid, json: async () => valid ? {ok: true} : {error: 'Invalid link'}};
      assert.equal(url, '/api/state');
      return {ok: true, json: async () => ({model: {articles: [], categories: [], connections: {}}, revision: 'fixture'})};
    }
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(actions[0], 'clean');
  assert.equal(requests[0].url, '/api/redeem');
  assert.deepEqual(JSON.parse(requests[0].options.body), {code});
  assert.equal(vm.runInContext('accessCode', context), null);
  assert.equal(requests.length, valid ? 2 : 1);
  if (valid) {
    assert.equal(elements.get('#app').hidden, false);
    assert.equal(elements.get('#login').hidden, true);
  } else {
    assert.match(elements.get('#notice').textContent, /Request a new link/);
  }
}
(async () => { await scenario(true); await scenario(false); })().catch(error => { console.error(error); process.exitCode = 1; });
