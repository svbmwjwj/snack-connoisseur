// KV access for the control plane.
//
//   meta:control   JSON {"host", "zone", "client_token", "admin_token"}
//   node:<alias>   JSON node record written by cnsr; `line` is the client config line
//   state:<alias>  JSON SNI-rotation state owned by the Worker
//   index:nodes    JSON array of every alias, written by cnsr; may name a node already gone

export async function readControl(kv) {
  const raw = await kv.get("meta:control");
  if (raw === null || raw === undefined) return null;
  let meta;
  try {
    meta = JSON.parse(raw);
  } catch {
    return null;
  }
  if (meta === null || typeof meta !== "object") return null;
  for (const key of ["client_token", "admin_token"]) {
    if (typeof meta[key] !== "string" || meta[key] === "") return null;
  }
  return meta;
}

async function readJson(kv, key) {
  const raw = await kv.get(key);
  if (raw === null || raw === undefined) return null;
  try {
    const value = JSON.parse(raw);
    return value !== null && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

export function readNode(kv, alias) {
  return readJson(kv, `node:${alias}`);
}

export function writeNode(kv, alias, record) {
  return kv.put(`node:${alias}`, JSON.stringify(record));
}

export async function readState(kv, alias, node) {
  const stored = await readJson(kv, `state:${alias}`);
  if (stored !== null) return stored;
  return {
    version: 1,
    sni: node?.xray?.sni ?? null,
    request: null,
    paused_since: null,
    exhausted: false,
    event: null,
  };
}

export function writeState(kv, alias, state) {
  return kv.put(`state:${alias}`, JSON.stringify(state));
}

// Swap the `obfs-host=<old>` field of a config line; everything else stays.
export function replaceSni(line, sni) {
  return line.replace(/(^|,\s*)obfs-host=[^,]*/, (_, lead) => `${lead}obfs-host=${sni}`);
}
