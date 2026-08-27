// Shared bootstrap for PhantomBridge unit tests.
//
// Generates an isolated synthetic config (fictional org, example.com, env:
// secret references) so tests never depend on a local config.json — which is
// gitignored and production-scoped. Requiring this module sets process.umask
// (so written config files land 0600) and exports a valid test nsec via
// PHANTOMBRIDGE_TEST_NSEC, which the synthetic config references as
// "env:PHANTOMBRIDGE_TEST_NSEC".
'use strict';
process.umask(0o077);

const fs = require('fs');
const os = require('os');
const path = require('path');
const {generateSecretKey, getPublicKey, nip19} = require('nostr-tools');

const TEST_NSEC = nip19.nsecEncode(generateSecretKey());
process.env.PHANTOMBRIDGE_TEST_NSEC = TEST_NSEC;

// Synthetic base config for nostr-mode unit tests (no relay, no XMPP).
function baseConfig(overrides = {}) {
  return Object.assign({
    mode: 'nostr',
    nick: 'bridge-test',
    httpPort: 0,
    nostr: { relay: 'ws://127.0.0.1:19999', nsec: 'env:PHANTOMBRIDGE_TEST_NSEC' },
    agents: {
      alice: getPublicKey(generateSecretKey()),
      bob: getPublicKey(generateSecretKey()),
    },
    routing: { permissions: { alice: ['bob'], bob: ['alice'] }, default: 'deny' },
  }, overrides);
}

// Writes a synthetic config to an isolated temp dir, sets
// PHANTOMBRIDGE_CONFIG, and returns { tmpDir, tmpState, configPath, config }.
function setup(overrides = {}) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pb-test-'));
  const tmpState = path.join(tmpDir, 'state.json');
  const config = baseConfig(Object.assign({ stateFile: tmpState }, overrides));
  const configPath = path.join(tmpDir, 'config.json');
  fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
  fs.chmodSync(configPath, 0o600);
  process.env.PHANTOMBRIDGE_CONFIG = configPath;
  return { tmpDir, tmpState, configPath, config };
}

module.exports = { baseConfig, setup, TEST_NSEC };
