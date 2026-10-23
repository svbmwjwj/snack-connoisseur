// Helpers shared by the request handler and the node API.

const IPV4 = /^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/;

const encoder = new TextEncoder();

export function reply(status, body, contentType, extra = {}) {
  return new Response(body, {
    status,
    headers: { "content-type": contentType, "cache-control": "no-store", ...extra },
  });
}

export function text(status, body, extra) {
  return reply(status, body, "text/plain; charset=utf-8", extra);
}

export function json(status, value) {
  return reply(status, JSON.stringify(value), "application/json; charset=utf-8");
}

// Compare byte by byte over the longer input, never returning early.
export function tokensMatch(given, expected) {
  const a = encoder.encode(given);
  const b = encoder.encode(expected);
  let diff = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let i = 0; i < length; i += 1) {
    diff |= (a[i] ?? 0) ^ (b[i] ?? 0);
  }
  return diff === 0;
}

function isIpv6(address) {
  if (!address.includes(":") || !/^[0-9a-fA-F:.]+$/.test(address)) return false;
  try {
    new URL(`http://[${address}]/`);
    return true;
  } catch {
    return false;
  }
}

// Returns the bare IPv4/IPv6 literal, or null when `host` is anything else.
export function parseIpLiteral(host) {
  if (typeof host !== "string") return null;
  if (host.startsWith("[") && host.endsWith("]")) {
    const inner = host.slice(1, -1);
    return isIpv6(inner) ? inner : null;
  }
  if (IPV4.test(host) || isIpv6(host)) return host;
  return null;
}

export function bearerToken(request) {
  const match = /^Bearer +(.+)$/i.exec(request.headers.get("authorization") ?? "");
  return match ? match[1] : null;
}
