import { test } from "node:test";
import assert from "node:assert/strict";
import { makeHandler } from "../../src/cnsr/assets/worker/handler.js";
import { v2raynLink } from "../../src/cnsr/assets/worker/v2rayn.js";

const META = { host: "c.example.com", zone: "example.com", client_token: "ct", admin_token: "at" };
const LINE =
  "vless=n1.example.com:443, method=none, password=0b4c-uuid, obfs=over-tls, obfs-host=sni.example.org, " +
  "reality-base64-pubkey=Ab-c_D, reality-hex-shortid=0123abcd, vless-flow=xtls-rprx-vision, udp-relay=true, tag=jp-1";

test("a Quantumult X line becomes a VLESS REALITY share link", () => {
  const link = new URL(v2raynLink(LINE));
  assert.equal(link.protocol, "vless:");
  assert.equal(link.username, "0b4c-uuid");
  assert.equal(link.host, "n1.example.com:443");
  assert.deepEqual(Object.fromEntries(link.searchParams), {
    encryption: "none",
    flow: "xtls-rprx-vision",
    security: "reality",
    sni: "sni.example.org",
    fp: "chrome",
    pbk: "Ab-c_D",
    sid: "0123abcd",
    type: "tcp",
    headerType: "none",
  });
  assert.equal(link.hash, "#jp-1");
});

test("a line missing a REALITY field gives no link", () => {
  for (const field of ["password", "obfs-host", "reality-base64-pubkey", "reality-hex-shortid"]) {
    const line = LINE.split(", ").filter((part) => !part.startsWith(`${field}=`)).join(", ");
    assert.equal(v2raynLink(line), null, field);
  }
  assert.equal(v2raynLink("trojan=n1.example.com:443, password=x, tag=t"), null);
});

function serve(entries) {
  const store = new Map(Object.entries({ "meta:control": JSON.stringify(META), ...entries }));
  const kv = {
    async get(key) {
      return store.has(key) ? store.get(key) : null;
    },
    async list() {
      throw new Error("no list expected");
    },
  };
  const handler = makeHandler({ connect: () => {}, scripts: {}, now: () => 0 });
  return (path) => handler(new Request(`https://c.example.com${path}`), { CNSR: kv });
}

test("the v2rayN subscription is every node's link, Base64 encoded", async () => {
  const call = serve({
    "index:nodes": JSON.stringify(["jp-1", "us-1", "gone"]),
    "node:jp-1": JSON.stringify({ line: LINE }),
    "node:us-1": JSON.stringify({ line: LINE.replace("tag=jp-1", "tag=us-1") }),
  });
  const response = await call("/ct/v2rayn");
  assert.equal(response.status, 200);
  const links = Buffer.from(await response.text(), "base64").toString("utf8").trim().split("\n");
  assert.deepEqual(links.map((link) => new URL(link).hash), ["#jp-1", "#us-1"]);
});

test("a node without a line is left out of the v2rayN subscription", async () => {
  const call = serve({
    "index:nodes": JSON.stringify(["jp-1", "us-1"]),
    "node:jp-1": JSON.stringify({ line: LINE }),
    "node:us-1": JSON.stringify({ line: null }),
  });
  const body = Buffer.from(await (await call("/ct/v2rayn")).text(), "base64").toString("utf8");
  assert.equal(body, `${v2raynLink(LINE)}\n`);
});

test("the v2rayN subscription needs the client token", async () => {
  const call = serve({ "index:nodes": "[]" });
  assert.equal((await call("/bad/v2rayn")).status, 404);
});
