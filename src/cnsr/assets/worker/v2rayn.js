// The v2rayN subscription: each node's Quantumult X line rewritten as a VLESS REALITY share link,
// so both subscriptions always carry the same SNI. v2rayN runs no scripts: its devices neither
// check the nodes nor report, and pick up a rotated SNI when the subscription next updates.

// The share link must name a TLS fingerprint; Quantumult X lines have none.
const FINGERPRINT = "chrome";

function fields(line) {
  const found = {};
  for (const part of line.split(",")) {
    const at = part.indexOf("=");
    if (at > 0) found[part.slice(0, at).trim()] = part.slice(at + 1).trim();
  }
  return found;
}

// Returns the share link for one line, or null when the line is not a complete VLESS REALITY one.
export function v2raynLink(line) {
  const f = fields(line);
  const required = [f.vless, f.password, f["obfs-host"], f["reality-base64-pubkey"], f["reality-hex-shortid"]];
  if (required.some((value) => !value)) return null;
  const params = new URLSearchParams({
    encryption: "none",
    flow: f["vless-flow"] ?? "",
    security: "reality",
    sni: f["obfs-host"],
    fp: FINGERPRINT,
    pbk: f["reality-base64-pubkey"],
    sid: f["reality-hex-shortid"],
    type: "tcp",
    headerType: "none",
  });
  if (!f["vless-flow"]) params.delete("flow");
  const name = encodeURIComponent(f.tag ?? f.vless);
  return `vless://${encodeURIComponent(f.password)}@${f.vless}?${params}#${name}`;
}

function base64(textValue) {
  let binary = "";
  for (const byte of new TextEncoder().encode(textValue)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

// The subscription body for the given Quantumult X lines: one link per line, Base64 encoded.
export function v2raynBody(lines) {
  const links = lines.map(v2raynLink).filter((link) => link !== null);
  return base64(links.map((link) => `${link}\n`).join(""));
}
