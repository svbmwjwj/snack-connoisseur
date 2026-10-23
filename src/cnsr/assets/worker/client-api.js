// Endpoints Quantumult X calls, authenticated by the client token in the path:
//   GET  /<ct>/cnsr.snippet  -> the node config lines
//   GET  /<ct>/v2rayn        -> the same nodes as a v2rayN subscription (see v2rayn.js)
//   GET  /<ct>/status        -> per-node rotation state
//   GET  /<ct>/check.js, /<ct>/check-ui.js -> the Quantumult X scripts, endpoints filled in
//   POST /<ct>/report        {"alias", "kind": "failed" | "ok", "version", "confirmed"?} -> {"accepted", "reason"}
// "confirmed": true on a failure from a device known to be on that version (you updated as
// the long-press check asked, or it measured the version up before): the pause after a
// rotation only waits for devices still on the old config, so it does not hold this one.

import { json, reply, text, tokensMatch } from "./common.js";
import { readNode, readState, writeState } from "./store.js";
import { v2raynBody } from "./v2rayn.js";

export const PAUSE_FALLBACK_MS = 24 * 60 * 60 * 1000;
// A confirmed failure ends a pause only once it has lasted this long: a device may report before
// it fetched the rotated config. The rotation it asks for starts the next pause, so at most one
// confirmed report gets through per pause.
export const CONFIRM_AFTER_MS = 10 * 60 * 1000;

const NODE_PREFIX = "node:";
// The aliases of every node, kept by cnsr. Listing KV instead is capped at 1,000 calls a day on
// the free plan, and Quantumult X asks for status once a minute while it checks a node often.
const INDEX_KEY = "index:nodes";

async function readIndex(kv) {
  const raw = await kv.get(INDEX_KEY);
  if (raw === null || raw === undefined) return null;
  try {
    const aliases = JSON.parse(raw);
    return Array.isArray(aliases) && aliases.every((alias) => typeof alias === "string") ? aliases : null;
  } catch {
    return null;
  }
}

async function listAliases(kv) {
  const aliases = [];
  let cursor;
  do {
    const page = await kv.list({ prefix: NODE_PREFIX, cursor });
    for (const entry of page.keys) aliases.push(entry.name.slice(NODE_PREFIX.length));
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor !== undefined);
  return aliases;
}

// The index may name a node whose record is already gone: callers skip a missing record.
// Without a usable index (a control plane from an older cnsr) the namespace is listed.
async function nodeAliases(kv) {
  const aliases = (await readIndex(kv)) ?? (await listAliases(kv));
  return [...new Set(aliases)].sort();
}

async function nodeLines(kv) {
  const aliases = await nodeAliases(kv);
  const nodes = await Promise.all(aliases.map((alias) => readNode(kv, alias)));
  return nodes.map((node) => node?.line).filter((line) => typeof line === "string");
}

async function serveSnippet(kv) {
  const lines = await nodeLines(kv);
  return text(200, lines.map((line) => `${line}\n`).join(""));
}

async function serveV2rayn(kv) {
  return text(200, v2raynBody(await nodeLines(kv)));
}

function versionOf(state) {
  return Number.isInteger(state.version) ? state.version : 1;
}

function isPaused(state, now) {
  return typeof state.paused_since === "number" && now - state.paused_since < PAUSE_FALLBACK_MS;
}

function isPauseExpired(state, now) {
  return typeof state.paused_since === "number" && now - state.paused_since >= PAUSE_FALLBACK_MS;
}

async function serveStatus(kv, host, clientToken, now) {
  const nodes = {};
  for (const alias of await nodeAliases(kv)) {
    const node = await readNode(kv, alias);
    if (node === null) continue;
    const state = await readState(kv, alias, node);
    nodes[alias] = {
      version: versionOf(state),
      paused: isPaused(state, now),
      pause_expired: isPauseExpired(state, now),
      pending: Boolean(state.request),
      exhausted: state.exhausted === true,
    };
  }
  return json(200, { snippet: `https://${host}/${clientToken}/cnsr.snippet`, nodes });
}

async function readBody(request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return null;
  }
  return body !== null && typeof body === "object" && !Array.isArray(body) ? body : null;
}

const answer = (accepted, reason = null) => json(200, { accepted, reason });

async function handleReport(request, kv, now) {
  const body = await readBody(request);
  if (body === null) return text(400, "invalid body");
  const { alias, kind, version } = body;
  const confirmed = body.confirmed === true;
  if (typeof alias !== "string" || alias === "") return text(400, "alias is required");
  if (kind !== "failed" && kind !== "ok") return text(400, "kind must be failed or ok");
  if (!Number.isInteger(version)) return text(400, "version must be an integer");

  const node = await readNode(kv, alias);
  if (node === null) return text(404, "unknown node");

  // The single read-modify-write of state:<alias> for this request.
  const state = await readState(kv, alias, node);
  const at = now();
  if (version !== versionOf(state)) return answer(false, "stale");

  if (kind === "ok") {
    // A pass after the pause expired still ends it: pause_expired would stay up otherwise.
    if (typeof state.paused_since !== "number") return answer(false, "stale");
    await writeState(kv, alias, { ...state, paused_since: null });
    return answer(true);
  }

  const confirmedLongEnough = confirmed && at - state.paused_since >= CONFIRM_AFTER_MS;
  if (isPaused(state, at) && !confirmedLongEnough) return answer(false, "paused");
  if (state.exhausted === true) return answer(false, "exhausted");
  if (state.request) return answer(true, "pending");
  await writeState(kv, alias, { ...state, request: { at, version }, paused_since: null });
  return answer(true);
}

const CONFIG_PLACEHOLDER = "__CNSR_CONFIG__";

function serveScript(source, host, clientToken) {
  const config = JSON.stringify({
    status: `https://${host}/${clientToken}/status`,
    report: `https://${host}/${clientToken}/report`,
  });
  // A function replacer keeps "$" sequences in the config from being read as patterns.
  return reply(200, source.replace(CONFIG_PLACEHOLDER, () => config), "application/javascript; charset=utf-8");
}

// Returns a Response for a client-API path, or null when the path is not one.
export function handleClientApi(request, env, parts, control, now, scripts = {}) {
  if (parts.length !== 3 || parts[0] !== "" || !tokensMatch(parts[1], control.client_token)) {
    return null;
  }
  const route = `${request.method} ${parts[2]}`;
  if (route === "GET cnsr.snippet") return serveSnippet(env.CNSR);
  if (route === "GET v2rayn") return serveV2rayn(env.CNSR);
  if (route === "GET status") return serveStatus(env.CNSR, control.host, control.client_token, now());
  if (route === "GET check.js" && typeof scripts.check === "string") {
    return serveScript(scripts.check, control.host, control.client_token);
  }
  if (route === "GET check-ui.js" && typeof scripts.checkUi === "string") {
    return serveScript(scripts.checkUi, control.host, control.client_token);
  }
  if (route === "POST report") return handleReport(request, env.CNSR, now);
  return null;
}
