#!/usr/bin/env node
// MCP admin-token auth regression (PR #24 review, kaieriksen 2026-08-20):
// mcp-bridge.mjs must resolve httpAdminToken with the SAME vault:/env:
// reference semantics as bridge.js. A literal "vault:NAME"/"env:VAR" string
// must never be sent as the bearer token, and legacy tool-owned plaintext
// file keys (httpAdminTokenFile) are rejected. Covered with BOTH reference
// types end-to-end through loadAdminToken (the exact function bridgeFetch
// uses on every MCP call).
'use strict';
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

let passed = 0, failed = 0;
async function t(n, fn) {
  try { await fn(); console.log('  ok:', n); passed++; }
  catch (e) { console.error('  FAIL:', n, '-', e.message); failed++; }
}

async function main() {
  // ESM module: dynamic import (this test file stays CJS like the rest).
  const { loadAdminToken } = await import('./mcp-bridge.mjs');

  // --- env: reference -------------------------------------------------------
  await t('env: admin token resolves to the injected value', async () => {
    process.env.PHANTOMBRIDGE_TEST_ADMIN = 'test-admin-token-123456';
    const tok = await loadAdminToken({ httpAdminToken: 'env:PHANTOMBRIDGE_TEST_ADMIN' });
    assert.strictEqual(tok, 'test-admin-token-123456');
    delete process.env.PHANTOMBRIDGE_TEST_ADMIN;
  });

  await t('env: admin token with unset variable fails closed', async () => {
    delete process.env.PHANTOMBRIDGE_TEST_ADMIN;
    await assert.rejects(
      () => loadAdminToken({ httpAdminToken: 'env:PHANTOMBRIDGE_TEST_ADMIN' }),
      /is not set/,
    );
  });

  // --- vault: reference (fake `phantombot` on PATH) --------------------------
  await t('vault: admin token resolves via phantombot vault get', async () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'pb-mcp-vault-'));
    const bin = path.join(tmp, 'phantombot');
    // Minimal shim: `phantombot vault get <name>` echoes a fixed token.
    fs.writeFileSync(bin, '#!/bin/sh\nif [ "$1" = "vault" ] && [ "$2" = "get" ]; then echo "vault-token-123456"; fi\n');
    fs.chmodSync(bin, 0o755);
    const oldPath = process.env.PATH;
    process.env.PATH = tmp + path.delimiter + (oldPath || '');
    try {
      const tok = await loadAdminToken({ httpAdminToken: 'vault:bridge-admin-token' });
      assert.strictEqual(tok, 'vault-token-123456');
    } finally {
      process.env.PATH = oldPath;
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });

  await t('vault: admin token with unresolvable name fails closed', async () => {
    await assert.rejects(
      () => loadAdminToken({ httpAdminToken: 'vault:nonexistent-bridge-token' }),
      /cannot resolve vault:/,
    );
  });

  // --- rejection of plaintext / legacy file forms ---------------------------
  await t('plaintext inline admin token is rejected', async () => {
    await assert.rejects(
      () => loadAdminToken({ httpAdminToken: 'test-admin-token-123456' }),
      /plaintext secret not allowed/,
    );
  });

  await t('legacy httpAdminTokenFile key is rejected', async () => {
    await assert.rejects(
      () => loadAdminToken({ httpAdminTokenFile: './secrets/admin.token' }),
      /no longer supported/,
    );
  });

  await t('missing admin token fails closed', async () => {
    delete process.env.PHANTOMBRIDGE_ADMIN_TOKEN;
    await assert.rejects(
      () => loadAdminToken({}),
      /not configured/,
    );
  });

  await t('PHANTOMBRIDGE_ADMIN_TOKEN env fallback still works', async () => {
    process.env.PHANTOMBRIDGE_ADMIN_TOKEN = 'env-fallback-token-123456';
    const tok = await loadAdminToken({});
    assert.strictEqual(tok, 'env-fallback-token-123456');
    delete process.env.PHANTOMBRIDGE_ADMIN_TOKEN;
  });

  console.log(`\nMCP admin-token auth regression: ${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error('FATAL:', e); process.exit(1); });
