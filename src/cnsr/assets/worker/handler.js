// Control-plane request handler.
//
// The KV namespace is bound as `CNSR`; its keys are documented in store.js.
//
// `connect` is injected so the handler can run under Node in tests; worker.js
// supplies the real one from "cloudflare:sockets".

import { bearerToken, json, parseIpLiteral, text, tokensMatch } from "./common.js";
import { handleClientApi } from "./client-api.js";
import { handleNodeApi } from "./node-api.js";
import { readControl } from "./store.js";

const PROBE_TIMEOUT_MS = 5000;
// The nodes call the Worker on workers.dev, where the zone's security settings (Bot Fight Mode,
// say) do not challenge them; only the node API answers there.
const NODES_ONLY_SUFFIX = ".workers.dev";

function describeError(error) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return message || "connect failed";
}

function closeQuietly(socket) {
  try {
    Promise.resolve(socket.close()).catch(() => {});
  } catch {
    // Already closed or never opened; nothing to clean up.
  }
}

async function probePort(connect, hostname, port) {
  const started = Date.now();
  let socket;
  let timer;
  try {
    socket = connect({ hostname, port });
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("timeout")), PROBE_TIMEOUT_MS);
    });
    await Promise.race([socket.opened, timeout]);
    return { ok: true, ms: Date.now() - started, error: null };
  } catch (error) {
    return { ok: false, ms: null, error: describeError(error) };
  } finally {
    clearTimeout(timer);
    if (socket) closeQuietly(socket);
  }
}

async function handleProbe(request, control, connect) {
  if (request.method !== "POST") return text(405, "method not allowed", { allow: "POST" });

  const bearer = bearerToken(request);
  if (bearer === null || !tokensMatch(bearer, control.admin_token)) {
    return text(401, "unauthorized", { "www-authenticate": "Bearer" });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return text(400, "invalid json");
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return text(400, "invalid body");
  }

  const hostname = parseIpLiteral(body.host);
  if (hostname === null) return text(400, "host must be an IPv4 or IPv6 literal");
  const { port } = body;
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return text(400, "port must be an integer between 1 and 65535");
  }

  return json(200, await probePort(connect, hostname, port));
}

// `scripts` (the Quantumult X check scripts) and `now` are injectable for tests.
export function makeHandler({ connect, scripts = {}, now = Date.now }) {
  return async function handle(request, env) {
    const control = await readControl(env.CNSR);
    if (control === null) return text(503, "control plane not configured");

    const { hostname, pathname } = new URL(request.url);
    const parts = pathname.split("/");
    if (parts[0] === "" && parts[1] === "node") return handleNodeApi(request, env, parts, now);
    if (hostname.endsWith(NODES_ONLY_SUFFIX)) return text(404, "not found");

    if (pathname === "/admin/probe") return handleProbe(request, control, connect);
    const clientResponse = handleClientApi(request, env, parts, control, now, scripts);
    if (clientResponse !== null) return clientResponse;
    return text(404, "not found");
  };
}
