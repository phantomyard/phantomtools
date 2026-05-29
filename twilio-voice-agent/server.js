import Fastify from "fastify";
import fastifyWs from "@fastify/websocket";
import fastifyFormBody from "@fastify/formbody";
import OpenAI from "openai";
import twilio from "twilio";
import { readFile, writeFile, mkdir } from "fs/promises";
import { existsSync } from "fs";
import { execFile, spawn } from "child_process";
import dotenv from "dotenv";
dotenv.config();

const fastify = Fastify({ logger: true });
const PORT = process.env.PORT || 8080;
const SERVER_DOMAIN = process.env.SERVER_DOMAIN || "localhost";
const WS_URL = `wss://${SERVER_DOMAIN}/ws`;
const TWIML_URL = `https://${SERVER_DOMAIN}/twiml`;

// ─── Conversation LLM providers ─────────────────────────────────────
// Two OpenAI-compatible upstreams. The primary runs Claude Haiku 4.5 on
// Anthropic's direct API (lowest latency — no OpenRouter routing hop);
// the fallback runs Gemini 3 Flash via OpenRouter. A model spec prefixed
// "anthropic-direct:" routes to Anthropic's API; everything else routes
// through OpenRouter.
const openrouter = new OpenAI({
  apiKey: process.env.OPENROUTER_API_KEY,
  baseURL: "https://openrouter.ai/api/v1",
});
const anthropicDirect = process.env.VOICE_AGENT_ANTHROPIC_API_KEY
  ? new OpenAI({
      apiKey: process.env.VOICE_AGENT_ANTHROPIC_API_KEY,
      baseURL: "https://api.anthropic.com/v1/",
    })
  : null;

const ANTHROPIC_PREFIX = "anthropic-direct:";
// Resolve a model spec to a concrete { client, model, label }. If an
// anthropic-direct spec is requested but the key is missing, degrade
// gracefully to the same model over OpenRouter rather than crash.
function resolveModel(spec) {
  if (spec.startsWith(ANTHROPIC_PREFIX)) {
    const model = spec.slice(ANTHROPIC_PREFIX.length);
    if (!anthropicDirect) {
      fastify.log.warn(
        { spec },
        "anthropic-direct requested but VOICE_AGENT_ANTHROPIC_API_KEY unset — routing via OpenRouter",
      );
      return { client: openrouter, model: `anthropic/${model}`, label: `OpenRouter anthropic/${model}` };
    }
    return { client: anthropicDirect, model, label: `Anthropic-direct ${model}` };
  }
  return { client: openrouter, model: spec, label: `OpenRouter ${spec}` };
}

// Primary: Claude Haiku 4.5 direct. Fallback: Gemini 3 Flash via OpenRouter.
// When the primary upstream returns a 5xx / provider_unavailable, the
// handler retries once with the fallback before the canned hiccup line.
// Set VOICE_AGENT_FALLBACK_MODEL="" to disable the fallback entirely.
const PRIMARY = resolveModel(
  process.env.VOICE_AGENT_MODEL || "anthropic-direct:claude-haiku-4-5",
);
const FALLBACK_SPEC =
  process.env.VOICE_AGENT_FALLBACK_MODEL ?? "google/gemini-3-flash-preview";
const FALLBACK = FALLBACK_SPEC ? resolveModel(FALLBACK_SPEC) : null;
const FALLBACK_DISTINCT =
  !!FALLBACK &&
  !(FALLBACK.client === PRIMARY.client && FALLBACK.model === PRIMARY.model);

// Security: Bearer token for /initiate-call endpoint
const VOICE_AGENT_API_TOKEN = process.env.VOICE_AGENT_API_TOKEN || "";

// Twilio client for outbound calls
const TWILIO_ACCOUNT_SID = process.env.TWILIO_ACCOUNT_SID;
const TWILIO_AUTH_TOKEN = process.env.TWILIO_AUTH_TOKEN;
const TWILIO_PHONE_US = process.env.TWILIO_PHONE_US || "";
const TWILIO_PHONE_NL = process.env.TWILIO_PHONE_NL || "";
const twilioClient = TWILIO_ACCOUNT_SID && TWILIO_AUTH_TOKEN
  ? twilio(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
  : null;

// Select best phone number based on destination. NL recipients get the
// NL caller-ID when one is configured; everything else falls back to US.
function selectFromNumber(toNumber) {
  if (toNumber.startsWith("+31") && TWILIO_PHONE_NL) {
    return TWILIO_PHONE_NL;
  }
  return TWILIO_PHONE_US || TWILIO_PHONE_NL;
}

// Phantombot CLI — optional integration. When `phantombot` is on PATH
// (or PHANTOMBOT_BIN points at a binary), the agent exposes an
// `ask_<assistant>` tool that shells out to it for queries the inline
// LLM can't answer. Without it, the tool is omitted from the prompt.
const PHANTOMBOT_BIN = process.env.PHANTOMBOT_BIN || "phantombot";
const PHANTOMBOT_ASK_TIMEOUT_MS = Number(process.env.PHANTOMBOT_ASK_TIMEOUT_MS || 60_000);

// Streaming variant — A/B-tested via VOICE_AGENT_STREAM_ASK=1.
// When enabled, the assistant tool's stdout is piped straight into the
// Twilio ConversationRelay socket as text tokens, skipping the second
// LLM round trip. Uses a separate binary so the prod CLI is unaffected.
const PHANTOMBOT_STREAM_BIN =
  process.env.PHANTOMBOT_STREAM_BIN || "phantombot-stream";
const VOICE_AGENT_STREAM_ASK = process.env.VOICE_AGENT_STREAM_ASK === "1";

// Pending outbound calls — stores context before WebSocket connects
const pendingOutboundCalls = new Map();

// Parse a comma-separated env var into a Set of trimmed non-empty values.
function envSet(name) {
  const raw = process.env[name] || "";
  return new Set(
    raw.split(",").map((s) => s.trim()).filter((s) => s.length > 0)
  );
}

// Caller whitelist — only these numbers/SIP URIs can reach the agent.
// Configure via env (comma-separated, E.164):
//   ALLOWED_CALLERS=+447700900123,+31612345678
//   ALLOWED_SIP_USERS=alice,bob
// If both sets are empty, all calls are blocked at the TwiML layer.
const ALLOWED_CALLERS = envSet("ALLOWED_CALLERS");
const ALLOWED_SIP_USERS = envSet("ALLOWED_SIP_USERS");

// The agent's own dialable numbers. SIP calls TO these route to the
// voice bridge; SIP calls TO any other number get bridged out via PSTN.
// Defaults to TWILIO_PHONE_US/NL if AGENT_NUMBERS isn't set explicitly.
const AGENT_NUMBERS = (() => {
  const explicit = envSet("AGENT_NUMBERS");
  if (explicit.size > 0) return explicit;
  const derived = new Set();
  if (TWILIO_PHONE_US) derived.add(TWILIO_PHONE_US);
  if (TWILIO_PHONE_NL) derived.add(TWILIO_PHONE_NL);
  return derived;
})();

// Names used in greetings, system prompt, and transcript labels.
const PRINCIPAL_NAME = process.env.PRINCIPAL_NAME || "the principal";
const ASSISTANT_NAME = process.env.ASSISTANT_NAME || "Voice Assistant";

// Optional memory workspace — when set, MEMORY.md and daily notes from
// this directory are injected into the system prompt for grounded
// answers. Leave empty to disable context loading.
const MEMORY_WORKSPACE = process.env.MEMORY_WORKSPACE || "";
const MEMORY_TIMEZONE = process.env.MEMORY_TIMEZONE || "UTC";

// Voice selection map — different voices for different languages
const VOICE_MAP = {
  en: "1TE7ou3jyxHsyRehUuMB-1.0_0.7_0.8",   // Eastend Steve - English
  nl: "hLnc7y4d152WGG2BQlAY",                  // Jaimie - Amsterdam accent, warm and clear
  es: "BPoDAH7n4gFrnGY27Jkj",                  // Frankie San Juan - Spain neutral accent
  fr: "nPczCjzI2devNBz1zQrb",                  // Brian - placeholder for French
  default: "1TE7ou3jyxHsyRehUuMB-1.0_0.7_0.8"  // Eastend Steve
};

// Build language-appropriate outbound greeting
function buildOutboundGreeting(language, purpose) {
  const purposeSnippet = purpose ? purpose.slice(0, 100) : null;
  const a = ASSISTANT_NAME;
  const p = PRINCIPAL_NAME;
  switch (language) {
    case "nl":
      return purposeSnippet
        ? `Hallo, u spreekt met ${a}, de assistent van ${p}. Ik bel over: ${purposeSnippet}`
        : `Hallo, u spreekt met ${a}, de assistent van ${p}. Hoe gaat het?`;
    case "es":
      return purposeSnippet
        ? `Hola, soy ${a}, el asistente de ${p}. Llamo por: ${purposeSnippet}`
        : `Hola, soy ${a}, el asistente de ${p}. ¿Cómo está?`;
    case "fr":
      return purposeSnippet
        ? `Bonjour, je suis ${a}, l'assistant de ${p}. J'appelle au sujet de: ${purposeSnippet}`
        : `Bonjour, je suis ${a}, l'assistant de ${p}. Comment allez-vous?`;
    case "de":
      return purposeSnippet
        ? `Hallo, hier ist ${a}, der Assistent von ${p}. Ich rufe an wegen: ${purposeSnippet}`
        : `Hallo, hier ist ${a}, der Assistent von ${p}. Wie geht es Ihnen?`;
    default:
      return purposeSnippet
        ? `Hello, this is ${a} calling on behalf of ${p}. I'm calling about: ${purposeSnippet}`
        : `Hello, this is ${a} calling on behalf of ${p}. How are you today?`;
  }
}

function getVoiceForLanguage(language) {
  return VOICE_MAP[language] || VOICE_MAP.default;
}

// Language code to locale mapping for ConversationRelay
const LANGUAGE_LOCALE_MAP = {
  en: "en-GB",
  nl: "nl-NL",
  es: "es-ES",
  fr: "fr-FR",
  de: "de-DE",
  pt: "pt-PT",
  it: "it-IT",
};

function getLocaleForLanguage(language) {
  return LANGUAGE_LOCALE_MAP[language] || LANGUAGE_LOCALE_MAP.en;
}

// Deepgram speech model per language. English stays on `flux` — it's
// the lowest-latency model but English-only — so English calls keep the
// snappiest turn-taking behaviour. Every other language uses
// `nova-3-general`, which is multilingual (flux can't do es/nl/fr), so
// those are a touch less snappy but actually work instead of garbling
// into English. Switching back to English via switch_language returns
// the call to flux because the en-GB <Language> element below is flux too.
const SPEECH_MODEL_MAP = {
  en: "flux",
  nl: "nova-3-general",
  es: "nova-3-general",
  fr: "nova-3-general",
  de: "nova-3-general",
};

function getSpeechModelForLanguage(language) {
  return SPEECH_MODEL_MAP[language] || "nova-3-general";
}

// Build <Language> elements for multi-language support. Each declared
// language becomes switchable mid-call via the ConversationRelay
// `language` message (see the switch_language tool handler).
function buildLanguageElements() {
  return Object.entries(VOICE_MAP)
    .filter(([key]) => key !== "default")
    .map(([lang, voice]) => {
      const locale = getLocaleForLanguage(lang);
      const speechModel = getSpeechModelForLanguage(lang);
      return `      <Language code="${locale}" ttsProvider="ElevenLabs" voice="${voice}" transcriptionProvider="Deepgram" speechModel="${speechModel}"/>`;
    })
    .join("\n");
}

const WELCOME_INBOUND =
  process.env.WELCOME_INBOUND ||
  `Hello! This is ${ASSISTANT_NAME}. How can I help you today?`;

// Voice-specific system prompt. Override the whole thing via the
// VOICE_SYSTEM_PROMPT env var to give the agent a different personality.
// `${ASSISTANT_NAME}` and `${PRINCIPAL_NAME}` are substituted in either
// the env-supplied or default template.
const DEFAULT_VOICE_SYSTEM_PROMPT = `You are \${ASSISTANT_NAME}, \${PRINCIPAL_NAME}'s AI assistant, on a live phone call. Your responses are read aloud via text-to-speech.

PERSONALITY:
- Friendly, warm, and natural — like a capable mate, not a robot.
- Brief acknowledgments are good ("Sure thing", "Got it", "No worries").
- Match the caller's energy — if they're chatty, be warmer. If they're brief, be brief.
- If you don't know, say "I don't know" — don't waffle.

VOICE RULES:
- Keep responses to 1-3 sentences. Don't monologue.
- No markdown, formatting, bullet points, or emojis. Plain natural speech only.
- Spell out numbers and abbreviations naturally (e.g. "three thirty" not "3:30").
- Never proactively ask "anything else?" — just wait in silence.
- After answering, STOP. Don't add follow-up questions.
- Silence is fine. Comfortable silence is better than filler.
- Don't recap what the caller said.

TOOLS:
- Memory and daily notes (when configured) are injected into this prompt. Use this context first.
- Answer directly from injected context when the answer is there.
- Use ask_assistant when you CAN'T answer from injected context alone (older history, web search, home automation, sending messages, calendar lookups, anything requiring real tools).
- When using ask_assistant, narrate naturally: "Let me check that for you..."
- For genuinely complex tasks, say: "That's a bigger one — let me look into it and text you what I find."
- Use end_call when the caller says goodbye or asks to hang up.

LANGUAGE:
- The call starts in English. If the caller asks to speak in Spanish or Dutch, or simply starts speaking it, call switch_language with the matching code ("es" for Spanish, "nl" for Dutch), then reply naturally in that language.
- Switch back to "en" the moment the caller returns to English.
- Don't announce the switch mechanically — just call the tool and keep the conversation flowing in the new language.`;

const VOICE_SYSTEM_PROMPT = (process.env.VOICE_SYSTEM_PROMPT || DEFAULT_VOICE_SYSTEM_PROMPT)
  .replaceAll("${ASSISTANT_NAME}", ASSISTANT_NAME)
  .replaceAll("${PRINCIPAL_NAME}", PRINCIPAL_NAME);

// Tool definitions for OpenAI-compatible API
const TOOLS = [
  {
    type: "function",
    function: {
      name: "ask_assistant",
      description: `Ask ${ASSISTANT_NAME} (the main AI agent) to DO something or fetch real-time data you don't have. Use ONLY when your injected context doesn't have the answer. Good for: older history, web searches, sending messages, home automation, calendar lookups beyond today, or when you're unsure. Do NOT use for things already in your context.`,
      parameters: {
        type: "object",
        properties: {
          message: {
            type: "string",
            description: `The question or request to send to ${ASSISTANT_NAME}. Be specific and include all relevant context from the conversation.`
          }
        },
        required: ["message"]
      }
    }
  },
  {
    type: "function",
    function: {
      name: "end_call",
      description: "End the phone call. Use when the caller says goodbye, asks to hang up, or the conversation is naturally complete.",
      parameters: {
        type: "object",
        properties: {
          reason: {
            type: "string",
            description: "Brief reason for ending the call"
          }
        },
        required: ["reason"]
      }
    }
  },
  {
    type: "function",
    function: {
      name: "switch_language",
      description: "Switch the spoken and transcribed language of the live call. Call this as soon as the caller asks to speak another language, or starts speaking one. After switching, reply to the caller in the new language. English ('en') is the default — switch back to it when the caller returns to English.",
      parameters: {
        type: "object",
        properties: {
          language: {
            type: "string",
            enum: ["en", "es", "nl"],
            description: "Target language code: 'en' English, 'es' Spanish, 'nl' Dutch."
          }
        },
        required: ["language"]
      }
    }
  }
];

// Filler phrases for when ask_assistant is running — language-aware
const TOOL_FILLER_PHRASES = {
  en: [
    "Hang on, let me check.",
    "One sec, let me have a look.",
    "Bear with me a moment.",
    "Let me check that for you.",
    "Give me a sec.",
    "Hold on, just checking.",
    "One moment.",
  ],
  nl: [
    "Even kijken.",
    "Momentje, ik check het even.",
    "Eén seconde.",
    "Even geduld.",
    "Ik kijk het even na.",
    "Momentje.",
  ],
  es: [
    "Un momento, déjame verificar.",
    "Un segundo.",
    "Déjame revisar eso.",
    "Un momentito.",
  ],
  fr: [
    "Un instant, je vérifie.",
    "Une seconde.",
    "Laissez-moi vérifier.",
    "Un moment.",
  ],
  de: [
    "Einen Moment, ich schaue nach.",
    "Eine Sekunde.",
    "Moment bitte.",
  ],
};

function getRandomFiller(language = "en") {
  const phrases = TOOL_FILLER_PHRASES[language] || TOOL_FILLER_PHRASES.en;
  return phrases[Math.floor(Math.random() * phrases.length)];
}

// ─── Security: Request Validation ────────────────────────────────────

// Log auth failures in structured format for fail2ban
function logAuthFailure(req, reason) {
  const ip = req.headers["x-forwarded-for"]?.split(",")[0]?.trim()
    || req.headers["x-real-ip"]
    || req.ip;
  // Structured line for fail2ban filter to match
  fastify.log.warn(`AUTH_FAILURE ip=${ip} path=${req.url} reason=${reason}`);
}

// Validate Twilio request signature (HMAC-SHA1)
function validateTwilioRequest(req) {
  if (!TWILIO_AUTH_TOKEN) return true; // skip if no auth token configured
  const signature = req.headers["x-twilio-signature"];
  if (!signature) return false;

  const url = `https://${SERVER_DOMAIN}${req.url}`;
  const params = req.body || {};
  return twilio.validateRequest(TWILIO_AUTH_TOKEN, signature, url, params);
}

// Bearer token check for /initiate-call
function validateBearerToken(req) {
  if (!VOICE_AGENT_API_TOKEN) return true; // skip if no token configured
  const authHeader = req.headers["authorization"] || "";
  const token = authHeader.startsWith("Bearer ") ? authHeader.slice(7) : "";
  return token === VOICE_AGENT_API_TOKEN;
}

// Active call sessions
const sessions = new Map();

// ─── Context Loading ────────────────────────────────────────────────

async function loadContext() {
  if (!MEMORY_WORKSPACE) return ""; // disabled
  const workspace = MEMORY_WORKSPACE;
  const today = new Date().toLocaleDateString("en-CA", { timeZone: MEMORY_TIMEZONE });
  const parts = [];

  // MEMORY.md — curated long-term memory
  try {
    const memoryPath = `${workspace}/MEMORY.md`;
    if (existsSync(memoryPath)) {
      const memory = await readFile(memoryPath, "utf8");
      if (memory.trim()) {
        parts.push(`MEMORY:\n${memory}`);
      }
    }
  } catch (err) {
    fastify.log.warn({ err }, "Failed to load MEMORY.md");
  }

  // Today's daily file — fresh context
  try {
    const dailyPath = `${workspace}/memory/${today}.md`;
    if (existsSync(dailyPath)) {
      const daily = await readFile(dailyPath, "utf8");
      if (daily.trim()) {
        parts.push(`TODAY (${today}):\n${daily}`);
      }
    }
  } catch (err) {
    fastify.log.warn({ err }, "Failed to load today's daily file");
  }

  // Yesterday's daily file — recent context
  try {
    const yesterday = new Date(Date.now() - 86400000).toLocaleDateString("en-CA", { timeZone: MEMORY_TIMEZONE });
    const yesterdayPath = `${workspace}/memory/${yesterday}.md`;
    if (existsSync(yesterdayPath)) {
      const yesterdayFile = await readFile(yesterdayPath, "utf8");
      if (yesterdayFile.trim()) {
        parts.push(`YESTERDAY (${yesterday}):\n${yesterdayFile}`);
      }
    }
  } catch (err) {
    fastify.log.warn({ err }, "Failed to load yesterday's daily file");
  }

  if (parts.length > 0) {
    return "\n\n" + parts.join("\n\n") + "\n";
  }
  return "";
}

// ─── Phantombot CLI helpers ─────────────────────────────────────────

// Capture-mode: pipe `prompt` to `phantombot ask -` and resolve with
// `{ result }` or `{ error }`. Used for the `ask_assistant` voice tool.
function phantombotAsk(prompt, { timeoutMs = PHANTOMBOT_ASK_TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    let resolved = false;
    const finish = (value) => {
      if (resolved) return;
      resolved = true;
      resolve(value);
    };

    const child = execFile(
      PHANTOMBOT_BIN,
      ["ask", "--", "-"],
      { timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          if (err.killed) {
            fastify.log.warn("phantombot ask timed out");
            return finish({ error: `Request timed out — ${ASSISTANT_NAME} took too long to respond` });
          }
          fastify.log.error(
            { err: { message: err.message, code: err.code }, stderr: (stderr || "").slice(0, 500) },
            "phantombot ask failed",
          );
          return finish({ error: err.message });
        }
        finish({ result: (stdout || "").trim() });
      },
    );

    child.on("error", (err) => {
      fastify.log.error({ err: { message: err.message, code: err.code } }, "phantombot ask spawn error");
      finish({ error: err.message });
    });

    child.stdin.end(prompt);
  });
}

// Fire-and-forget: spawn `phantombot ask -` and don't wait. Used for
// post-call review notifications and failed-call notices, where the
// voice-agent doesn't care about the reply.
function phantombotNotify(prompt) {
  try {
    const child = spawn(PHANTOMBOT_BIN, ["ask", "--", "-"], {
      stdio: ["pipe", "ignore", "pipe"],
    });
    let stderrBuf = "";
    child.stderr?.on("data", (d) => { stderrBuf += d.toString(); });
    child.on("error", (err) => {
      fastify.log.error({ err: { message: err.message, code: err.code } }, "phantombot notify spawn error");
    });
    child.on("close", (code) => {
      if (code !== 0) {
        fastify.log.warn({ code, stderr: stderrBuf.slice(0, 500) }, "phantombot notify exited non-zero");
      }
    });
    child.stdin.end(prompt);
  } catch (err) {
    fastify.log.error({ err }, "phantombot notify failed to spawn");
  }
}

// Streaming variant: spawns `phantombot-stream ask --stream -- -`, feeds
// the prompt to stdin, and invokes `onChunk(text)` for every stdout
// 'data' event. Returns `{ result }` (full concatenated text) when the
// child exits cleanly, or `{ error }` on spawn failure / timeout / non-
// zero exit. The caller's onChunk is what actually pushes tokens at
// Twilio — this helper just plumbs the bytes.
//
// Critical: PHANTOMBOT_LOG_LEVEL=warn is forced so the orchestrator's
// info-level "trying harness" JSON line doesn't leak onto stdout (which
// would otherwise get spoken). Warn/error logs still go to stderr.
function phantombotAskStream(prompt, onChunk, { timeoutMs = PHANTOMBOT_ASK_TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    let resolved = false;
    let fullText = "";
    let stderrBuf = "";
    const finish = (value) => {
      if (resolved) return;
      resolved = true;
      try { clearTimeout(timer); } catch {}
      resolve(value);
    };

    let child;
    try {
      child = spawn(
        PHANTOMBOT_STREAM_BIN,
        ["ask", "--stream", "--", "-"],
        {
          stdio: ["pipe", "pipe", "pipe"],
          env: { ...process.env, PHANTOMBOT_LOG_LEVEL: "warn" },
        },
      );
    } catch (err) {
      fastify.log.error({ err: { message: err.message, code: err.code } }, "phantombot-stream spawn threw");
      return finish({ error: err.message });
    }

    const timer = setTimeout(() => {
      fastify.log.warn("phantombot-stream timed out");
      try { child.kill("SIGKILL"); } catch {}
      finish({ error: `Request timed out — ${ASSISTANT_NAME} took too long to respond` });
    }, timeoutMs);

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      const text = typeof chunk === "string" ? chunk : chunk.toString("utf8");
      fullText += text;
      try {
        onChunk(text);
      } catch (err) {
        fastify.log.warn({ err: { message: err.message } }, "phantombot-stream onChunk threw");
      }
    });

    child.stderr.on("data", (d) => {
      stderrBuf += d.toString();
      if (stderrBuf.length > 4096) stderrBuf = stderrBuf.slice(-4096);
    });

    child.on("error", (err) => {
      fastify.log.error({ err: { message: err.message, code: err.code } }, "phantombot-stream spawn error");
      finish({ error: err.message });
    });

    child.on("close", (code) => {
      if (code === 0) {
        finish({ result: fullText.trim() });
      } else {
        fastify.log.error({ code, stderr: stderrBuf.slice(0, 500) }, "phantombot-stream exited non-zero");
        finish({ error: `phantombot-stream exited with code ${code}` });
      }
    });

    try {
      child.stdin.end(prompt);
    } catch (err) {
      fastify.log.error({ err: { message: err.message } }, "phantombot-stream stdin write failed");
      finish({ error: err.message });
    }
  });
}

// ─── Ask assistant (voice-call tool relay) ──────────────────────────

async function askAssistant(message) {
  // Inline what was previously a system message — `phantombot ask`
  // takes a single user prompt, and the assistant's harness has its
  // identity baked in, so we only need to add voice-call constraints.
  const VOICE_PREAMBLE =
    "[Voice-call tool relay] Respond in 1–3 sentences. " +
    "No markdown or formatting — your reply will be spoken aloud. " +
    "Give the key facts directly.\n\n";

  return phantombotAsk(VOICE_PREAMBLE + (message || ""));
}

// ─── Anthropic Conversation Handler ─────────────────────────────────

async function handleConversation(userText, session, ws) {
  // Determine call language for fillers and fallbacks. activeLanguage is
  // updated by the switch_language tool, so mid-call switches carry over
  // to fillers/fallbacks on subsequent turns.
  const callLang = session.activeLanguage || session.outboundContext?.language || "en";

  // Build context instruction
  let contextInstruction = "";
  if (session.isOutbound && session.outboundContext) {
    const oc = session.outboundContext;
    const languageNames = { en: "English", nl: "Dutch", es: "Spanish", fr: "French" };
    const langName = languageNames[oc.language] || "English";
    contextInstruction = `\n\nYou are making an outbound call on behalf of ${PRINCIPAL_NAME}. Speak in ${langName}. Purpose: ${oc.purpose || "General inquiry"}. Context: ${oc.context || "None provided."}`;
  }

  const now = new Date().toLocaleString("en-GB", { timeZone: MEMORY_TIMEZONE });
  let callerInfo;
  if (session.isOutbound) {
    callerInfo = `\n\nCall info — Outbound to: ${session.to}. Time: ${now} (Europe/Amsterdam).`;
  } else {
    // Inbound calls have already passed the ALLOWED_CALLERS whitelist, so the
    // caller is a trusted party. We deliberately do NOT put the caller's raw
    // phone number into the prompt: the call is already gated by the
    // allowlist, and exposing the number caused the model to forward it into
    // ask_assistant queries — where the back-end agent rejected it as an
    // unrecognised identity. The model only needs to know it's a trusted call.
    callerInfo = `\n\nCall info — Inbound call. Time: ${now} (Europe/Amsterdam).`;
    callerInfo += ` This inbound call came from a number on ${PRINCIPAL_NAME}'s trusted caller-ID allowlist — treat the caller as ${PRINCIPAL_NAME} (identity verified by the whitelist) unless they explicitly say otherwise.`;
  }

  // Load dynamic context from context.json
  const dynamicContext = await loadContext();

  const systemPrompt = VOICE_SYSTEM_PROMPT + contextInstruction + callerInfo + dynamicContext;

  // Build OpenAI messages from session history + new user message
  const messages = [
    { role: "system", content: systemPrompt },
    ...session.openaiMessages,
    { role: "user", content: userText },
  ];

  let fullResponse = "";
  let endCallRequested = false;
  // When the streaming-ask path takes over, it pushes tokens straight
  // to Twilio and we exit the LLM loop without feeding the tool result
  // back for a second round. This flag breaks us out cleanly and is
  // also returned as the final transcript text.
  let streamedToolResponse = null;

  try {
    let currentMessages = [...messages];
    let loopCount = 0;
    const MAX_LOOPS = 5;

    while (loopCount < MAX_LOOPS) {
      loopCount++;

      // Set up a filler timer for long responses
      let fillerSent = false;
      let firstTokenReceived = false;
      const fillerTimer = setTimeout(() => {
        if (!firstTokenReceived && ws.readyState === 1) {
          const filler = getRandomFiller(callLang);
          ws.send(JSON.stringify({ type: "text", token: filler, last: true }));
          fillerSent = true;
          fastify.log.info({ filler }, "Sent filler phrase");
        }
      }, 5000);

      let responseText = "";
      let toolCalls = [];  // Accumulate tool calls from streaming

      try {
        const stream = await PRIMARY.client.chat.completions.create({
          model: PRIMARY.model,
          max_tokens: 512,
          messages: currentMessages,
          tools: TOOLS,
          stream: true,
        });

        // Accumulate streamed deltas
        for await (const chunk of stream) {
          const delta = chunk.choices?.[0]?.delta;
          if (!delta) continue;

          // Text content
          if (delta.content) {
            responseText += delta.content;
            if (/^NO_?R?E?P?L?Y?$/i.test(responseText.trim())) continue;
            if (!firstTokenReceived) {
              firstTokenReceived = true;
              clearTimeout(fillerTimer);
            }
            if (ws.readyState === 1) {
              ws.send(JSON.stringify({ type: "text", token: delta.content, last: false }));
            }
          }

          // Tool call deltas
          if (delta.tool_calls) {
            for (const tc of delta.tool_calls) {
              const idx = tc.index;
              if (!toolCalls[idx]) {
                toolCalls[idx] = {
                  id: tc.id || "",
                  type: "function",
                  function: { name: tc.function?.name || "", arguments: "" },
                };
              }
              if (tc.id) toolCalls[idx].id = tc.id;
              if (tc.function?.name) toolCalls[idx].function.name = tc.function.name;
              if (tc.function?.arguments) toolCalls[idx].function.arguments += tc.function.arguments;
            }
          }
        }
      } finally {
        clearTimeout(fillerTimer);
      }

      // Send last token marker
      if (responseText && ws.readyState === 1) {
        ws.send(JSON.stringify({ type: "text", token: "", last: true }));
      }

      // No tool calls — we're done
      if (toolCalls.length === 0) {
        fullResponse = responseText;
        session.openaiMessages.push({ role: "user", content: userText });
        if (responseText) {
          session.openaiMessages.push({ role: "assistant", content: responseText });
        }
        break;
      }

      // Handle tool calls
      const assistantMsg = { role: "assistant", content: responseText || null, tool_calls: toolCalls };
      const toolResultMsgs = [];

      for (const tc of toolCalls) {
        let args;
        try {
          args = JSON.parse(tc.function.arguments);
        } catch {
          args = {};
        }

        if (tc.function.name === "ask_assistant") {
          // Keep-alive narration. phantombot can block 15-40s while it
          // runs its own tools and emits nothing until it finishes, so a
          // single filler leaves the caller in dead air. Send one filler
          // now, then a fresh non-repeating filler every 7s until the
          // first real token (streaming path) or the result (blocking).
          let askFirstToken = false;
          let lastAskFiller = "";
          const sendAskFiller = (tag) => {
            if (ws.readyState !== 1) return;
            let filler = getRandomFiller(callLang);
            for (let i = 0; i < 5 && filler === lastAskFiller; i++) {
              filler = getRandomFiller(callLang);
            }
            lastAskFiller = filler;
            ws.send(JSON.stringify({ type: "text", token: filler, last: true }));
            fastify.log.info({ filler, tag }, "Sent filler for ask_assistant");
          };
          if (!fillerSent) sendAskFiller("initial");
          let askFillerCount = 0;
          const askKeepAlive = setInterval(() => {
            // Self-terminate past the phantombot ask timeout so a leaked
            // interval can never outlive the tool call.
            if (askFirstToken || ws.readyState !== 1 || ++askFillerCount > 10) {
              clearInterval(askKeepAlive);
              return;
            }
            sendAskFiller("keepalive");
          }, 9000);

          // Streaming path: forward phantombot stdout → ConversationRelay
          // tokens. We buffer until ~12 chars or sentence-ending
          // punctuation to avoid spamming Twilio with one-character
          // payloads (which the relay can choke on). On any failure or
          // empty response, fall back to the blocking path so the call
          // doesn't break.
          let streamSucceeded = false;
          if (VOICE_AGENT_STREAM_ASK) {
            const VOICE_PREAMBLE =
              "[Voice-call tool relay] Respond in 1–3 sentences. " +
              "No markdown or formatting — your reply will be spoken aloud. " +
              "Give the key facts directly.\n\n";
            const fullPrompt = VOICE_PREAMBLE + (args.message || "");

            let pending = "";
            const flushPending = () => {
              if (pending.length === 0) return;
              if (ws.readyState !== 1) { pending = ""; return; }
              ws.send(JSON.stringify({ type: "text", token: pending, last: false }));
              pending = "";
            };
            const onChunk = (text) => {
              // First real token from phantombot — stop the filler loop.
              if (!askFirstToken) {
                askFirstToken = true;
                clearInterval(askKeepAlive);
              }
              pending += text;
              // Flush if we hit a sentence boundary or a comfortable
              // chunk size. Sentence boundary is preferred — Twilio's
              // TTS sounds noticeably smoother when it gets whole
              // clauses at a time.
              if (/[.!?](\s|$)/.test(pending) || pending.length >= 12) {
                flushPending();
              }
            };

            const streamRes = await phantombotAskStream(fullPrompt, onChunk);
            // Drain any trailing partial chunk before signalling end.
            flushPending();

            if (!streamRes.error && streamRes.result && streamRes.result.length > 0) {
              if (ws.readyState === 1) {
                ws.send(JSON.stringify({ type: "text", token: "", last: true }));
              }
              streamedToolResponse = streamRes.result;
              streamSucceeded = true;
              fastify.log.info(
                { query: args.message, result: streamRes.result.slice(0, 200), streamed: true },
                "ask_assistant streamed",
              );
            } else {
              fastify.log.warn(
                { err: streamRes.error, empty: !streamRes.result },
                "phantombot-stream failed or empty — falling back to blocking ask",
              );
              // Fall through to blocking path below.
            }
          }

          if (!streamSucceeded) {
            const result = await askAssistant(args.message || "");
            toolResultMsgs.push({
              role: "tool",
              tool_call_id: tc.id,
              content: result.error ? `Error: ${result.error}` : result.result,
            });
            fastify.log.info({ query: args.message, result: ((result.result || result.error || "").slice(0, 200)) }, "ask_assistant completed");
          }
          // Definitive cleanup — covers the streaming path that returned
          // zero chunks (onChunk never fired) and the blocking path.
          askFirstToken = true;
          clearInterval(askKeepAlive);
        } else if (tc.function.name === "end_call") {
          endCallRequested = true;
          toolResultMsgs.push({
            role: "tool",
            tool_call_id: tc.id,
            content: "Call ending.",
          });
          fastify.log.info({ reason: args.reason }, "end_call requested");
        } else if (tc.function.name === "switch_language") {
          // Flip the live call's TTS + transcription language via the
          // ConversationRelay `language` control message. The target
          // locale must be declared as a <Language> element in the TwiML
          // (buildLanguageElements) — en/es/nl/fr are.
          const langNames = { en: "English", es: "Spanish", nl: "Dutch" };
          const target = langNames[args.language] ? args.language : null;
          if (!target) {
            toolResultMsgs.push({
              role: "tool",
              tool_call_id: tc.id,
              content: `Unsupported language "${args.language}". Supported: English (en), Spanish (es), Dutch (nl). Staying in the current language.`,
            });
            fastify.log.warn({ requested: args.language }, "switch_language: unsupported language");
          } else {
            const locale = getLocaleForLanguage(target);
            if (ws.readyState === 1) {
              ws.send(JSON.stringify({
                type: "language",
                ttsLanguage: locale,
                transcriptionLanguage: locale,
              }));
            }
            session.activeLanguage = target;
            toolResultMsgs.push({
              role: "tool",
              tool_call_id: tc.id,
              content: `Language switched to ${langNames[target]} (${locale}). Now reply to the caller in ${langNames[target]}.`,
            });
            fastify.log.info(
              { language: target, locale, callSid: session.callSid },
              "switch_language: switched call language",
            );
          }
        }
      }

      // Streaming-ask short-circuit: tokens already went to Twilio,
      // we don't want a second LLM round summarising them. Persist the
      // turn to history (so multi-turn callers stay coherent) and
      // bail out of the conversation loop.
      if (streamedToolResponse !== null) {
        fullResponse = streamedToolResponse;
        session.openaiMessages.push({ role: "user", content: userText });
        session.openaiMessages.push({ role: "assistant", content: streamedToolResponse });
        break;
      }

      // Add assistant + tool results and loop for next response
      currentMessages = [
        ...currentMessages,
        assistantMsg,
        ...toolResultMsgs,
      ];

      fillerSent = false;

      // Bail out if WebSocket closed during tool execution (e.g. IVR hung up)
      if (ws.readyState !== 1) {
        fastify.log.info({ callSid: session.callSid }, "WebSocket closed during tool call — aborting conversation loop");
        return "";
      }
    }

    const cleaned = fullResponse.replace(/\bNO_REPLY\b/g, "").replace(/\bNO_\b/g, "").trim();

    if (endCallRequested) {
      setTimeout(() => {
        if (ws.readyState === 1) {
          ws.close();
        }
      }, 3000);
    }

    return cleaned;

  } catch (err) {
    const isTimeout = err.name === "AbortError" || err.name === "TimeoutError";
    if (isTimeout) {
      fastify.log.warn("LLM response timed out");
      const msg = "That's taking a bit longer than expected. I'll text you the answer instead.";
      if (ws.readyState === 1) {
        ws.send(JSON.stringify({ type: "text", token: msg, last: true }));
      }
      return msg;
    }

    // Upstream LLM failure (e.g. OpenRouter 5xx, provider_unavailable from
    // Inception). Try the fallback model once before falling back to the
    // canned hiccup line. We do a non-tool, non-loop streaming call — the
    // fallback path is for keeping the conversation alive, not for tool use.
    const status = err?.status ?? err?.code;
    const isUpstream =
      err?.error?.metadata?.error_type === "provider_unavailable" ||
      (typeof status === "number" && status >= 500);
    if (isUpstream && FALLBACK_DISTINCT && ws.readyState === 1) {
      fastify.log.warn(
        { primary: PRIMARY.label, fallback: FALLBACK.label, status, errMsg: err?.message },
        "Primary LLM upstream failed — retrying with fallback model"
      );
      try {
        const fbStream = await FALLBACK.client.chat.completions.create({
          model: FALLBACK.model,
          max_tokens: 512,
          messages,
          stream: true,
        });
        let fbText = "";
        for await (const chunk of fbStream) {
          const token = chunk.choices?.[0]?.delta?.content;
          if (!token) continue;
          fbText += token;
          if (ws.readyState === 1) {
            ws.send(JSON.stringify({ type: "text", token, last: false }));
          }
        }
        if (fbText && ws.readyState === 1) {
          ws.send(JSON.stringify({ type: "text", token: "", last: true }));
        }
        if (fbText.trim()) {
          session.openaiMessages.push({ role: "user", content: userText });
          session.openaiMessages.push({ role: "assistant", content: fbText });
          fastify.log.info(
            { fallback: FALLBACK.label, chars: fbText.length },
            "Fallback LLM succeeded"
          );
          return fbText.replace(/\bNO_REPLY\b/g, "").replace(/\bNO_\b/g, "").trim();
        }
      } catch (fbErr) {
        fastify.log.error(
          { err: fbErr, fallback: FALLBACK.label },
          "Fallback LLM also failed"
        );
      }
    }

    fastify.log.error({ err }, "LLM conversation failed");
    const fallback = "I'm sorry, I had a brief hiccup there. Could you repeat that?";
    if (ws.readyState === 1) {
      ws.send(JSON.stringify({ type: "text", token: fallback, last: true }));
    }
    return fallback;
  }
}

// ─── Notify assistant of Call End ────────────────────────────────────

async function notifyCallEnd(session) {
  try {
    const duration = Math.round((Date.now() - session.startTime) / 1000);
    const msgCount = session.messages.length;

    const recentMsgs = session.messages.slice(-6);
    const summary = recentMsgs
      .map(m => {
        const content = typeof m.content === "string" ? m.content : "";
        const speaker = m.role === "user" ? (session.isOutbound ? "Recipient" : "Caller") : ASSISTANT_NAME;
        return `${speaker}: ${content.slice(0, 150)}`;
      })
      .filter(s => s.length > 10)
      .join("\n");

    // Send full transcript to the assistant for review / memory updates
    const fullTranscript = session.messages
      .map(m => {
        const content = typeof m.content === "string" ? m.content : "";
        const speaker = m.role === "user" ? (session.isOutbound ? "Recipient" : "Caller") : ASSISTANT_NAME;
        return `${speaker}: ${content}`;
      })
      .filter(s => s.length > 5)
      .join("\n");

    let message;
    if (session.isOutbound) {
      message = `[Outbound Call Completed] Called ${session.to}, duration: ${duration}s, ${msgCount} messages.

**Purpose:** ${session.outboundContext?.purpose || "Not specified"}

**Full transcript:**
${fullTranscript}

Please review this call transcript and update memory with any agreements, appointments, or action items.`;
    } else {
      message = `[Voice Call Ended] Call from ${session.from}, duration: ${duration}s, ${msgCount} messages.

**Full transcript:**
${fullTranscript}

Review the transcript and follow up on any action items. Update memory with anything important.`;
    }

    phantombotNotify(message);

    fastify.log.info({ from: session.from, to: session.to, duration, isOutbound: session.isOutbound }, "Call-end review dispatched");
  } catch (err) {
    fastify.log.error({ err }, "Failed to send call-end review");
  }
}

// ─── Call Transcript ────────────────────────────────────────────────

async function saveCallTranscript(session) {
  // Transcript persistence is opt-in: only writes when MEMORY_WORKSPACE
  // is configured. Otherwise transcripts are still sent to the
  // assistant relay (if any) but not persisted to disk by this process.
  if (!MEMORY_WORKSPACE) return;
  const workspace = MEMORY_WORKSPACE;
  const today = new Date().toLocaleDateString("en-CA", { timeZone: MEMORY_TIMEZONE });
  const filePath = `${workspace}/memory/${today}.md`;

  const now = new Date().toLocaleString("en-GB", { timeZone: MEMORY_TIMEZONE });
  const duration = Math.round((Date.now() - session.startTime) / 1000);

  let transcript = `\n\n## Phone Call — ${now}\n`;

  if (session.isOutbound) {
    transcript += `**Direction:** Outbound\n`;
    transcript += `**To:** ${session.to}\n`;
    transcript += `**Purpose:** ${session.outboundContext?.purpose || "Not specified"}\n`;
  } else {
    transcript += `**From:** ${session.from}\n`;
  }

  transcript += `**Duration:** ${duration}s\n\n`;

  const otherPartyLabel = session.isOutbound ? "Recipient" : "Caller";

  for (const msg of session.messages) {
    if (msg.role === "user") {
      transcript += `**${otherPartyLabel}:** ${msg.content}\n\n`;
    } else if (msg.role === "assistant" && typeof msg.content === "string") {
      transcript += `**${ASSISTANT_NAME}:** ${msg.content}\n\n`;
    }
  }

  try {
    if (existsSync(filePath)) {
      const existing = await readFile(filePath, "utf8");
      await writeFile(filePath, existing + transcript);
    } else {
      await mkdir(`${workspace}/memory`, { recursive: true });
      await writeFile(filePath, `# ${today}\n${transcript}`);
    }
    fastify.log.info({ isOutbound: session.isOutbound }, "Call transcript saved to daily notes");
  } catch (err) {
    fastify.log.error({ err }, "Failed to save transcript");
  }
}

// ─── Server Setup ───────────────────────────────────────────────────

await fastify.register(fastifyFormBody);
await fastify.register(fastifyWs);

// Health check
fastify.get("/health", async () => ({
  status: "ok",
  activeCalls: sessions.size,
  voiceLLMEnabled: !!(process.env.OPENROUTER_API_KEY || process.env.VOICE_AGENT_ANTHROPIC_API_KEY),
  phantombotBin: PHANTOMBOT_BIN,
  phantombotStreamBin: PHANTOMBOT_STREAM_BIN,
  streamingAskEnabled: VOICE_AGENT_STREAM_ASK,
  outboundEnabled: !!twilioClient,
  phones: {
    us: TWILIO_PHONE_US,
    nl: TWILIO_PHONE_NL,
  },
}));

// Initiate outbound call — bearer token required
fastify.post("/initiate-call", async (req, reply) => {
  // Validate bearer token
  if (!validateBearerToken(req)) {
    logAuthFailure(req, "invalid_bearer_token_initiate_call");
    return reply.status(403).send({ error: "Forbidden" });
  }

  if (!twilioClient) {
    return reply.status(503).send({
      error: "Outbound calls not configured",
      detail: "Twilio credentials not set"
    });
  }

  const { to, purpose, context, greeting, callbackSession, language } = req.body || {};

  if (!to) {
    return reply.status(400).send({ error: "Missing 'to' phone number" });
  }

  const cleanedTo = to.replace(/\s/g, "");
  if (!cleanedTo.match(/^\+[1-9]\d{6,14}$/)) {
    return reply.status(400).send({
      error: "Invalid phone number format",
      detail: "Phone number must be in E.164 format (e.g., +31612345678)"
    });
  }

  // Auto-detect language from context/greeting if not explicitly set
  let callLanguage = language;
  if (!callLanguage) {
    const allText = `${purpose || ""} ${context || ""} ${greeting || ""}`.toLowerCase();
    if (/spanish|español|habla español|en español|solo español/.test(allText)) callLanguage = "es";
    else if (/dutch|nederlands|in het nederlands/.test(allText)) callLanguage = "nl";
    else if (/french|français|en français/.test(allText)) callLanguage = "fr";
    else if (/german|deutsch|auf deutsch/.test(allText)) callLanguage = "de";
    else callLanguage = "en";
  }
  fastify.log.info({ to: cleanedTo, purpose, language: callLanguage }, "Initiating outbound call");

  try {
    const fromNumber = selectFromNumber(cleanedTo);
    fastify.log.info({ from: fromNumber }, "Selected outbound number");

    const call = await twilioClient.calls.create({
      to: cleanedTo,
      from: fromNumber,
      url: TWIML_URL,
      statusCallback: `https://${SERVER_DOMAIN}/call-status`,
      statusCallbackEvent: ["initiated", "ringing", "answered", "completed"],
      statusCallbackMethod: "POST",
    });

    pendingOutboundCalls.set(call.sid, {
      to: cleanedTo,
      purpose: purpose || null,
      context: context || null,
      greeting: greeting || null,
      callbackSession: callbackSession || "main",
      language: callLanguage,
      initiatedAt: Date.now(),
    });

    fastify.log.info({ callSid: call.sid, to: cleanedTo, language: callLanguage }, "Outbound call initiated");

    return {
      success: true,
      callSid: call.sid,
      to: cleanedTo,
      language: callLanguage,
      status: call.status,
    };
  } catch (err) {
    fastify.log.error({ err, to: cleanedTo }, "Failed to initiate outbound call");
    return reply.status(500).send({
      error: "Failed to initiate call",
      detail: err.message,
    });
  }
});

// Call status webhook — Twilio signature validation
fastify.post("/call-status", async (req, reply) => {
  // Validate Twilio signature
  if (!validateTwilioRequest(req)) {
    logAuthFailure(req, "invalid_twilio_signature_call_status");
    return reply.status(403).send("Forbidden");
  }

  const { CallSid, CallStatus, To, From, Duration } = req.body || {};
  fastify.log.info({ CallSid, CallStatus, To, Duration }, "Call status update");

  if (CallStatus === "busy" || CallStatus === "no-answer" || CallStatus === "failed" || CallStatus === "canceled") {
    const pendingCall = pendingOutboundCalls.get(CallSid);
    if (pendingCall) {
      pendingOutboundCalls.delete(CallSid);

      const failedCallMessage = `[Outbound Call Failed] Call to ${To} was not answered (status: ${CallStatus}).${pendingCall.purpose ? `\nPurpose: ${pendingCall.purpose}` : ""}\n\nYou may want to try again later or use an alternative contact method.`;
      phantombotNotify(failedCallMessage);
    }
  }

  reply.send({ received: true });
});

// TwiML endpoint — with caller whitelist + Twilio signature validation
fastify.all("/twiml", async (req, reply) => {
  // Validate Twilio signature
  if (!validateTwilioRequest(req)) {
    logAuthFailure(req, "invalid_twilio_signature_twiml");
    return reply.status(403).send("Forbidden");
  }

  const from = req.body?.From || req.query?.From || "";
  const to = req.body?.To || req.query?.To || "";
  const callSid = req.body?.CallSid || req.query?.CallSid || "";
  const direction = req.body?.Direction || req.query?.Direction || "";
  fastify.log.info({ from, to, callSid, direction }, "TwiML request received");

  // Check if this is an outbound call we initiated
  const pendingCall = pendingOutboundCalls.get(callSid);
  const isOutbound = direction.startsWith("outbound") || !!pendingCall;

  if (isOutbound && pendingCall) {
    const greeting = pendingCall.greeting || buildOutboundGreeting(pendingCall.language, pendingCall.purpose);

    const safeGreeting = greeting
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

    const callLang = pendingCall.language || "en";
    const voiceId = getVoiceForLanguage(callLang);
    const locale = getLocaleForLanguage(callLang);
    const languageElements = buildLanguageElements();

    reply.type("text/xml").send(`<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="${WS_URL}?callSid=${callSid}"
      welcomeGreeting="${safeGreeting}"
      ttsProvider="ElevenLabs"
      voice="${voiceId}"
      language="${locale}"
      transcriptionProvider="Deepgram"
      speechModel="flux"
      eotThreshold="0.9"
      ignoreBackchannel="true"
      interruptible="speech"
      interruptSensitivity="low"
      dtmfDetection="true"
    >
${languageElements}
    </ConversationRelay>
  </Connect>
</Response>`);
    return;
  }

  // Inbound call — check whitelist
  const sipUserMatch = from.match(/^sip:(\w+)@/);
  const isSipAllowed = sipUserMatch && ALLOWED_SIP_USERS.has(sipUserMatch[1]);
  const isAllowed = ALLOWED_CALLERS.has(from) || ALLOWED_CALLERS.has(to) || isSipAllowed;
  if (!isAllowed) {
    fastify.log.warn({ from, to }, "Blocked call from unknown number");
    reply.type("text/xml").send(`<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice">Sorry, this number is not accepting calls at the moment. Goodbye.</Say>
  <Hangup/>
</Response>`);
    return;
  }

  // SIP call routing
  if (isSipAllowed) {
    const sipToMatch = to.match(/^sip:(\+?\d+)@/);
    const dialedNumber = sipToMatch ? sipToMatch[1] : null;

    if (dialedNumber && !AGENT_NUMBERS.has(dialedNumber)) {
      const callerId = selectFromNumber(dialedNumber);
      fastify.log.info({ from, dialedNumber, callerId }, "SIP → PSTN call");
      reply.type("text/xml").send(`<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial callerId="${callerId}">
    <Number>${dialedNumber}</Number>
  </Dial>
</Response>`);
      return;
    }
    fastify.log.info({ from, to }, "SIP → voice agent");
  }

  // Inbound calls use English (Eastend Steve) with multi-language support
  const voiceId = getVoiceForLanguage("en");
  const languageElements = buildLanguageElements();

  reply.type("text/xml").send(`<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="${WS_URL}"
      welcomeGreeting="${WELCOME_INBOUND}"
      ttsProvider="ElevenLabs"
      voice="${voiceId}"
      language="en-GB"
      transcriptionProvider="Deepgram"
      speechModel="flux"
      eotThreshold="0.9"
      ignoreBackchannel="true"
      interruptible="speech"
      interruptSensitivity="low"
      dtmfDetection="true"
    >
${languageElements}
    </ConversationRelay>
  </Connect>
</Response>`);
});

// WebSocket endpoint — ConversationRelay
fastify.register(async function (app) {
  app.get("/ws", { websocket: true }, (socket, req) => {
    fastify.log.info("WebSocket connection opened");

    socket.on("message", async (data) => {
      let msg;
      try {
        msg = JSON.parse(data.toString());
      } catch (err) {
        fastify.log.error({ err, data: data.toString() }, "Invalid WebSocket message");
        return;
      }

      switch (msg.type) {
        case "setup": {
          const callSid = msg.callSid;
          const from = msg.from || "unknown";
          const to = msg.to || "unknown";

          const pendingCall = pendingOutboundCalls.get(callSid);
          const isOutbound = !!pendingCall;

          fastify.log.info({ callSid, from, to, isOutbound }, "Call setup");

          sessions.set(callSid, {
            messages: [],            // Simple role/content pairs for transcript
            openaiMessages: [],      // OpenAI API format messages for context
            from,
            to: isOutbound ? to : null,
            startTime: Date.now(),
            isOutbound,
            outboundContext: pendingCall || null,
            callbackSession: pendingCall?.callbackSession || "main",
            // Active call language — flipped by the switch_language tool.
            // Outbound calls may start in a non-English language; inbound
            // always starts in English (matching the TwiML default).
            activeLanguage: pendingCall?.language || "en",
          });

          if (pendingCall) {
            pendingOutboundCalls.delete(callSid);
          }

          socket.callSid = callSid;
          break;
        }

        case "prompt": {
          const callSid = socket.callSid;
          const session = sessions.get(callSid);
          if (!session) {
            fastify.log.warn({ callSid }, "No session found for prompt");
            break;
          }

          const userText = msg.voicePrompt;
          fastify.log.info({ callSid, text: userText }, "Caller spoke");

          if (session.responseInProgress) {
            fastify.log.info({ callSid }, "Response in progress, ignoring prompt");
            break;
          }

          session.responseInProgress = true;

          let response;
          try {
            response = await handleConversation(userText, session, socket);
            // Store simplified messages for transcript
            session.messages.push({ role: "user", content: userText });
            if (response) {
              session.messages.push({ role: "assistant", content: response });
            }
          } finally {
            session.responseInProgress = false;
          }

          fastify.log.info({ callSid, response: (response || "").slice(0, 100) }, "Assistant responded");
          break;
        }

        case "interrupt": {
          const callSid = socket.callSid;
          const session = sessions.get(callSid);
          if (!session) break;

          fastify.log.info(
            { callSid, heardUntil: msg.utteranceUntilInterrupt },
            "Caller interrupted"
          );

          // Update last assistant message in both transcript and Anthropic history
          const msgs = session.messages;
          if (msgs.length > 0 && msgs[msgs.length - 1].role === "assistant") {
            const last = msgs[msgs.length - 1];
            if (typeof last.content === "string") {
              last.content = msg.utteranceUntilInterrupt + " [interrupted]";
            }
          }
          const aMessages = session.openaiMessages;
          if (aMessages.length > 0 && aMessages[aMessages.length - 1].role === "assistant") {
            const last = aMessages[aMessages.length - 1];
            if (typeof last.content === "string") {
              last.content = msg.utteranceUntilInterrupt + " [interrupted]";
            }
          }
          break;
        }

        case "dtmf": {
          fastify.log.info({ digit: msg.digit }, "DTMF received");
          break;
        }

        case "error": {
          fastify.log.error({ error: msg }, "ConversationRelay error");
          break;
        }

        default: {
          fastify.log.info({ type: msg.type }, "Unknown message type");
        }
      }
    });

    socket.on("close", async () => {
      const callSid = socket.callSid;
      fastify.log.info({ callSid }, "WebSocket closed — call ended");

      const session = sessions.get(callSid);
      if (session && session.messages.length > 0) {
        await saveCallTranscript(session);
        await notifyCallEnd(session);
      }
      sessions.delete(callSid);
    });

    socket.on("error", (err) => {
      fastify.log.error({ err }, "WebSocket error");
    });
  });
});

// Start server
try {
  await fastify.listen({ port: PORT, host: "0.0.0.0" });
  fastify.log.info(`Voice agent v2.0 running on port ${PORT}`);
  fastify.log.info(`TwiML endpoint: https://${SERVER_DOMAIN}/twiml`);
  fastify.log.info(`WebSocket endpoint: wss://${SERVER_DOMAIN}/ws`);
  fastify.log.info(`Outbound call endpoint: https://${SERVER_DOMAIN}/initiate-call`);
  fastify.log.info(`AI backend: ${PRIMARY.label}` + (FALLBACK_DISTINCT ? ` (fallback: ${FALLBACK.label})` : ""));
  fastify.log.info(`Phantombot CLI: ${PHANTOMBOT_BIN}`);
  fastify.log.info(`Phantombot stream CLI: ${PHANTOMBOT_STREAM_BIN}`);
  fastify.log.info(`Streaming ask_assistant: ${VOICE_AGENT_STREAM_ASK ? "enabled (VOICE_AGENT_STREAM_ASK=1)" : "disabled"}`);
  fastify.log.info(`Outbound calls: ${twilioClient ? "enabled" : "disabled (no Twilio credentials)"}`);
} catch (err) {
  fastify.log.error(err);
  process.exit(1);
}
