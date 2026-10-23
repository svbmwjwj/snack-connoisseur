// Endpoints the nodes call, authenticated with each node's own token:
//   GET  /node/<alias>/task[?version=<n>]          -> {"rotate": bool, "version"}
//   PUT  /node/<alias>/line[?version=<n>]  {"sni", "dest_ip"} -> {"version"}
// <n> is the version of the node's last accepted line (see serveTask).
//   POST /node/<alias>/event   {"kind", "detail"} -> 204

import { bearerToken, json, parseIpLiteral, text, tokensMatch } from "./common.js";
import { readNode, readState, replaceSni, writeNode, writeState } from "./store.js";

const DOMAIN = /^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$/;
const EVENT_KINDS = new Set(["exhausted", "rotate_failed"]);
const MAX_DETAIL = 500;

const versionOf = (state) => (Number.isInteger(state.version) ? state.version : 1);

// The node's ?version=<n>, or 0 when it sent none (or something else).
function nodeVersion(request) {
  const given = new URL(request.url).searchParams.get("version");
  return given !== null && /^[1-9][0-9]{0,15}$/.test(given) ? Number(given) : 0;
}

function unauthorized() {
  return text(401, "unauthorized", { "www-authenticate": "Bearer" });
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

async function putLine(request, kv, alias, node, now) {
  const body = await readBody(request);
  if (body === null) return text(400, "invalid body");
  const { sni, dest_ip: destIp } = body;
  if (typeof sni !== "string" || sni.length > 253 || !DOMAIN.test(sni)) {
    return text(400, "sni must be a domain name");
  }
  if (parseIpLiteral(destIp) === null) return text(400, "dest_ip must be an IPv4 or IPv6 literal");
  if (typeof node.line !== "string") return text(409, "node has no config line");

  const state = await readState(kv, alias, node);
  const known = nodeVersion(request);
  // A retry of a PUT that already got through: answer it again without a second bump.
  if (state.sni === sni && !state.request && versionOf(state) >= known) {
    return json(200, { version: versionOf(state) });
  }
  // Above both the state and the node's last line, in case the state read is behind.
  const version = Math.max(versionOf(state), known) + 1;
  const next = {
    ...state,
    version,
    sni,
    request: null,
    paused_since: now(),
    exhausted: false,
  };
  const record = {
    ...node,
    line: replaceSni(node.line, sni),
    xray: { ...node.xray, sni, dest_ip: parseIpLiteral(destIp) },
  };
  // Write the state last: a failed node write must not advance the version.
  await writeNode(kv, alias, record);
  await writeState(kv, alias, next);
  return json(200, { version });
}

async function postEvent(request, kv, alias, node, now) {
  const body = await readBody(request);
  if (body === null) return text(400, "invalid body");
  const { kind, detail } = body;
  if (!EVENT_KINDS.has(kind)) return text(400, "unknown event kind");
  if (typeof detail !== "string") return text(400, "detail must be a string");

  const state = await readState(kv, alias, node);
  const next = { ...state, event: { kind, detail: detail.slice(0, MAX_DETAIL), at: now() } };
  if (kind === "exhausted") {
    next.exhausted = true;
    next.request = null;
  }
  await writeState(kv, alias, next);
  return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
}

// The node sends the version of its last accepted PUT. A state behind it is a stale write (a
// report that read the state from before that PUT): the version is put back and the request it
// carried dropped, as it was about a config the node no longer runs. The PUT had opened a pause,
// which the stale write lost, so the pause starts again. Versions only ever go up.
async function serveTask(request, kv, alias, node, now) {
  const known = nodeVersion(request);
  let state = await readState(kv, alias, node);
  if (versionOf(state) < known) {
    state = { ...state, version: known, request: null, paused_since: now() };
    await writeState(kv, alias, state);
  }
  return json(200, { rotate: Boolean(state.request), version: versionOf(state) });
}

const ROUTES = {
  task: { method: "GET" },
  line: { method: "PUT" },
  event: { method: "POST" },
};

// `parts` is the path split on "/", already known to start with ["", "node"].
export async function handleNodeApi(request, env, parts, now) {
  if (parts.length !== 4 || !Object.hasOwn(ROUTES, parts[3])) return text(404, "not found");
  const route = ROUTES[parts[3]];
  if (request.method !== route.method) {
    return text(405, "method not allowed", { allow: route.method });
  }

  let alias;
  try {
    alias = decodeURIComponent(parts[2]);
  } catch {
    return unauthorized();
  }
  const given = bearerToken(request);
  const node = await readNode(env.CNSR, alias);
  const expected = node?.node_token;
  if (given === null || typeof expected !== "string" || expected === "" || !tokensMatch(given, expected)) {
    return unauthorized();
  }

  if (parts[3] === "task") return serveTask(request, env.CNSR, alias, node, now);
  if (parts[3] === "line") return putLine(request, env.CNSR, alias, node, now);
  return postEvent(request, env.CNSR, alias, node, now);
}
