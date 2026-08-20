// Shared secret-reference resolution for PhantomBridge (CONTRIBUTING.md §4.6).
//
// Both the bridge runtime (bridge.js) and the MCP server (mcp-bridge.mjs)
// resolve secrets through THIS module, so the two processes interpret
// vault:/env: references identically and reject plaintext/file secrets the
// same way. A secret is configured as a REFERENCE, never as a plaintext value
// or a tool-owned plaintext file:
//   "vault:NAME" -> resolved via `phantombot vault get NAME`
//                   (the persona's AES-256-GCM vault; fail-closed on error).
//   "env:VAR"    -> resolved from the operator-injected environment variable.
// Any other non-empty string is a plaintext inline secret and is REJECTED.
// Legacy tool-owned plaintext file keys (*File) are REJECTED with a migration
// hint. The bridge owns no plaintext secret store.
'use strict';
const { execFileSync } = require('child_process');

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
      out = execFileSync('phantombot', ['vault', 'get', name], { encoding: 'utf8' });
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

module.exports = { resolveSecretRef, readSecret };
