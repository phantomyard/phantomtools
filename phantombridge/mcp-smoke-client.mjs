#!/usr/bin/env node
/**
 * mcp-smoke-client.mjs — MCP client mínimo (stdio) para probar mcp-bridge.mjs
 * end-to-end sin depender de phantombot. Habla MCP por stdio como lo haría
 * cualquier harness (Claude/Codex/phantombot proxy).
 *
 * Uso: node mcp-smoke-client.mjs <tool> [jsonArgs]
 *   node mcp-smoke-client.mjs bridge_status
 *   node mcp-smoke-client.mjs bridge_pause '{"side":"nostr","paused":true}'
 *   node mcp-smoke-client.mjs bridge_join '{"room":"mia"}'
 *   node mcp-smoke-client.mjs bridge_leave '{"room":"mia"}'
 */
import { spawn } from "node:child_process";

const SERVER = process.argv[2] ; // path script
const tool = process.argv[3];
const argsRaw = process.argv[4] || "{}";
const args = JSON.parse(argsRaw);

const child = spawn("node", [SERVER], { stdio: ["pipe", "pipe", "pipe"] });
let stderrBuf = "";
child.stderr.on("data", (d) => (stderrBuf += d));

const pending = new Map();
let nextId = 1;
let tools = null;

function send(obj) {
  child.stdin.write(JSON.stringify(obj) + "\n");
}

function rpc(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ jsonrpc: "2.0", id, method, params });
  });
}

function onLine(line) {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
  }
}

let buf = "";
child.stdout.on("data", (d) => {
  buf += d.toString();
  let nl;
  while ((nl = buf.indexOf("\n")) >= 0) {
    onLine(buf.slice(0, nl));
    buf = buf.slice(nl + 1);
  }
});

async function main() {
  const init = await rpc("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "smoke-client", version: "1" },
  });
  send({ jsonrpc: "2.0", method: "notifications/initialized" });

  const t = await rpc("tools/list", {});
  tools = t.tools;

  if (tool === "tools/list") {
    console.log(JSON.stringify({ tools: tools.map((x) => x.name) }, null, 2));
    return;
  }

  const found = tools.find((x) => x.name === tool);
  if (!found) {
    console.log(JSON.stringify({ error: `tool not found: ${tool}`, available: tools.map((x) => x.name) }, null, 2));
    return;
  }

  const call = await rpc("tools/call", { name: tool, arguments: args });
  console.log(JSON.stringify({ tool, result: call }, null, 2));
}

main()
  .catch((e) => console.log(JSON.stringify({ fatal: e.message, stderr: stderrBuf }, null, 2)))
  .finally(() => { child.kill(); });
