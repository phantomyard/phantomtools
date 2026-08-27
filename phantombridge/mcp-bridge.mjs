/**
 * mcp-bridge.mjs — MCP (Model Context Protocol) stdio server for PhantomBridge.
 *
 * Exposes the local HTTP API of the Jitsi↔Nostr bridge (default 127.0.0.1:8090)
 * as MCP tools, so an MCP-native harness / orchestrator (phantombot, Claude,
 * Codex, OpenClaw...) can drive the bridge without curl or SSH.
 *
 * Tools:
 *   bridge_status  -> GET  /status
 *   bridge_pause   -> POST /pause           (kill-switch per side)
 *   bridge_join    -> POST /join            (Jitsi line only)
 *   bridge_leave   -> POST /leave           (Jitsi line only)
 *
 * LINE-AWARE (scope cotejo with the bridge, per bridge-mcp #1):
 *   The bridge runs TWO independent lines:
 *     - Telegram bot<->bot (Nostr DM<->DM routing)  -> active when NOSTR_MODE
 *     - Jitsi (XMPP MUC rooms: join/leave/message)  -> active when JITSI_MODE
 *                                                 AND the XMPP channel is online
 *   `config.mode = "both"` refers to the PRE-MCP infra (jitsi+nostr), NOT to
 *   Telegram-vs-Jitsi granularity. Therefore this MCP server:
 *     - ALWAYS exposes bridge_status and bridge_pause (both lines share them).
 *     - Dynamically exposes bridge_join / bridge_leave ONLY when the Jitsi
 *       line is actually available: /status reports xmpp:"online" (or the
 *       mode implies JITSI and XMPP is up). If Jitsi is not up, the tools
 *       are NOT announced (tools/list omits them) — they are not callable.
 *   This avoids the `joinRoom is not defined` class of errors, which happen
 *   when the HTTP endpoint exists but the Jitsi runtime scope was never
 *   initialized (Telegram-only / XMPP down).
 *
 * Design rules (aligned with the phantombot ecosystem):
 *   - Uses the official @modelcontextprotocol/sdk (what phantombot uses).
 *   - Reads bridge config (httpPort / host) from the same config.json.
 *   - Logs to stderr; stdout clean for the MCP wire (phantombot #369).
 *   - Register: phantombot mcp add bridge --stdio --command node
 *       --args /path/to/mcp-bridge.mjs
 *
 * Register: phantombot mcp add bridge --stdio --command node
 *       --args /path/to/mcp-bridge.mjs
 *
 * Testing: the admin-token resolution seam is covered by test-mcp-auth.js
 * (unit + MCP-to-HTTP round trip using both vault: and env: reference types).
 * End-to-end integration is verified with phantombot's built-in MCP tools.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const require = createRequire(import.meta.url);
const { resolveSecretRef } = require("./secrets.js");

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** Resolve the bridge config.json: explicit arg > ./config.json next to this file. */
async function loadBridgeConfig() {
  const explicit = process.argv[2];
  const candidates = explicit ? [path.resolve(explicit)] : [path.join(__dirname, "config.json")];
  for (const file of candidates) {
    try {
      const raw = await readFile(file, "utf8");
      return { ...JSON.parse(raw), _configPath: file };
    } catch {
      /* try next */
    }
  }
  return { httpPort: 8090, _configPath: null };
}

function bridgeBase(cfg) {
  // PhantomBridge intentionally binds its control plane to loopback. Never
  // let an MCP config redirect the bearer token to an arbitrary host.
  const port = cfg.httpPort || 8090;
  return `http://127.0.0.1:${port}`;
}

async function loadAdminToken(cfg) {
  // Mirror the bridge's own resolution (bridge.js): the admin token is a
  // secret REFERENCE. Reject the legacy tool-owned plaintext file key, resolve
  // vault:/env: through the SAME resolver the bridge uses (secrets.js), and
  // fall back to the operator-injected PHANTOMBRIDGE_ADMIN_TOKEN. Never send
  // a literal "vault:NAME"/"env:VAR" string as the bearer token.
  if (cfg.httpAdminTokenFile !== undefined && cfg.httpAdminTokenFile !== null) {
    throw new Error('HTTP admin token: httpAdminTokenFile (tool-owned plaintext file) is no longer supported — use httpAdminToken: "vault:NAME" (phantombot vault) or "env:VAR", or PHANTOMBRIDGE_ADMIN_TOKEN');
  }
  if (typeof cfg.httpAdminToken === 'string' && cfg.httpAdminToken.trim()) {
    return resolveSecretRef(cfg.httpAdminToken, 'HTTP admin token');
  }
  const env = process.env.PHANTOMBRIDGE_ADMIN_TOKEN?.trim();
  if (env) return env;
  throw new Error('PhantomBridge admin token is not configured (use httpAdminToken: "vault:NAME" or "env:VAR", or PHANTOMBRIDGE_ADMIN_TOKEN)');
}

/** Authenticated HTTP helper for the local bridge API. */
async function bridgeFetch(cfg, method, url, body) {
  const token = await loadAdminToken(cfg);
  const headers = { Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(bridgeBase(cfg) + url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json;
  try {
    json = text ? JSON.parse(text) : {};
  } catch {
    json = { raw: text };
  }
  return { status: res.status, json };
}

async function textResult(payload) {
  return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] };
}

// ─── Jitsi line availability ─────────────────────────────────────────────────
/**
 * True when the Jitsi line is usable: the bridge reports an online XMPP
 * channel. /status includes `xmpp: "online"` (or "connecting"/"offline").
 * If we cannot reach /status at all, we err on the side of NOT exposing
 * join/leave (fail-safe: never advertise a callable that may 500).
 */
async function jitsiLineAvailable(cfg) {
  try {
    const { status, json } = await bridgeFetch(cfg, "GET", "/status");
    if (status !== 200 || !json) return false;
    if (json.xmpp === "online") return true;
    // Fallback: if xmpp field is absent but mode is jitsi/both and there is
    // any room machinery, trust mode only as a last resort.
    const mode = String(json.mode || "").toLowerCase();
    return (mode === "jitsi" || mode === "both") && json.xmpp === undefined && json.ok !== false;
  } catch {
    return false;
  }
}

const server = new McpServer(
  { name: "phantombridge", version: "1.8.0" },
  { capabilities: { tools: {} } },
);

// ─── bridge_status (both lines) ──────────────────────────────────────────────
server.tool(
  "bridge_status",
  "Get the current state of PhantomBridge split by LINE: the Telegram bot<->bot (Nostr DM routing) line and the Jitsi (XMPP rooms) line. Reports joined rooms, nicks, agents, XMPP state (online/offline), per-side paused state, and whether the Jitsi line is currently available. Use this FIRST to see which line is active before calling join/leave.",
  {},
  async () => {
    const cfg = await loadBridgeConfig();
    const { status, json } = await bridgeFetch(cfg, "GET", "/status");
    if (status !== 200) {
      return {
        content: [{ type: "text", text: `bridge /status failed: HTTP ${status} ${JSON.stringify(json)}` }],
        isError: true,
      };
    }
    const jitsiUp = await jitsiLineAvailable(cfg);
    const lines = {
      telegram_bot2bot: { active: String(json.mode || "").toLowerCase() !== "jitsi" },
      jitsi: { active: jitsiUp, xmpp: json.xmpp || "unknown" },
    };
    return await textResult({ bridge: json, lines, available_tools: jitsiUp ? ["bridge_status", "bridge_pause", "bridge_join", "bridge_leave"] : ["bridge_status", "bridge_pause"] });
  },
);

// ─── bridge_pause (both lines) ───────────────────────────────────────────────
server.tool(
  "bridge_pause",
  "Pause or resume PhantomBridge per side (kill-switch). 'nostr' (Telegram bot<->bot) paused = agent DMs silently ignored -> no token burn. 'jitsi' paused = rooms left, room commands answer 'paused'. 'both' pauses both. Returns resulting paused state.",
  {
    side: z.enum(["jitsi", "nostr", "both"]).describe("Which side to pause/resume: jitsi, nostr, or both"),
    paused: z.boolean().describe("true to pause, false to resume"),
  },
  async ({ side, paused }) => {
    const cfg = await loadBridgeConfig();
    const { status, json } = await bridgeFetch(cfg, "POST", "/pause", { side, paused });
    if (status !== 200) {
      return {
        content: [{ type: "text", text: `bridge /pause failed: HTTP ${status} ${JSON.stringify(json)}` }],
        isError: true,
      };
    }
    return await textResult(json);
  },
);

// ─── bridge_join (Jitsi line only) ───────────────────────────────────────────
server.tool(
  "bridge_join",
  "Make the bridge join a Jitsi room (Jitsi line only). Room may be a bare name (roomSuffix appended) or a full JID. Optional nick, password, and auto-leave timeout (minutes). Only usable when the Jitsi line is online — check bridge_status first.",
  {
    room: z.string().describe("Room name to join (bare name or full JID)"),
    nick: z.string().optional().describe("Nick to join with (defaults to bridge nick)"),
    password: z.string().optional().describe("Optional room password"),
    timeout: z.number().optional().describe("Auto-leave timeout in minutes (optional)"),
  },
  async ({ room, nick, password, timeout }) => {
    const cfg = await loadBridgeConfig();
    if (!(await jitsiLineAvailable(cfg))) {
      return {
        content: [{ type: "text", text: "Jitsi line is not available (XMPP not online / Telegram-only mode). Use bridge_status to check. bridge_join is disabled until the Jitsi line is up." }],
        isError: true,
      };
    }
    const body = { room };
    if (nick !== undefined) body.nick = nick;
    if (password !== undefined) body.password = password;
    if (timeout !== undefined) body.timeout = timeout;
    const { status, json } = await bridgeFetch(cfg, "POST", "/join", body);
    if (status !== 200) {
      return {
        content: [{ type: "text", text: `bridge /join failed: HTTP ${status} ${JSON.stringify(json)}` }],
        isError: true,
      };
    }
    return await textResult(json);
  },
);

// ─── bridge_leave (Jitsi line only) ──────────────────────────────────────────
server.tool(
  "bridge_leave",
  "Make the bridge leave a Jitsi room it has joined (Jitsi line only). Pass the same room name used to join. Only usable when the Jitsi line is online — check bridge_status first.",
  {
    room: z.string().describe("Room name to leave"),
  },
  async ({ room }) => {
    const cfg = await loadBridgeConfig();
    if (!(await jitsiLineAvailable(cfg))) {
      return {
        content: [{ type: "text", text: "Jitsi line is not available (XMPP not online / Telegram-only mode). Use bridge_status to check. bridge_leave is disabled until the Jitsi line is up." }],
        isError: true,
      };
    }
    const { status, json } = await bridgeFetch(cfg, "POST", "/leave", { room });
    if (status !== 200) {
      return {
        content: [{ type: "text", text: `bridge /leave failed: HTTP ${status} ${JSON.stringify(json)}` }],
        isError: true,
      };
    }
    return await textResult(json);
  },
);

// ─── run ─────────────────────────────────────────────────────────────────────
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write("[mcp-bridge] PhantomBridge MCP stdio server ready (line-aware)\n");
}

// Only connect the stdio transport when run directly (`node mcp-bridge.mjs`),
// so tests can import loadAdminToken without spawning a server.
const isMain = process.argv[1] &&
  pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url;

if (isMain) {
  main().catch((err) => {
    process.stderr.write(`[mcp-bridge] fatal: ${err && err.stack ? err.stack : err}\n`);
    process.exit(1);
  });
}

export { loadAdminToken, resolveSecretRef };
