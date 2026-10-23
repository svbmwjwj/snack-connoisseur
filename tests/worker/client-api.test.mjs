import { test } from "node:test";
import assert from "node:assert/strict";
import { makeHandler } from "../../src/cnsr/assets/worker/handler.js";
import { CONFIRM_AFTER_MS, PAUSE_FALLBACK_MS } from "../../src/cnsr/assets/worker/client-api.js";

const META = { host: "c.example.com", zone: "example.com", client_token: "ct", admin_token: "at" };
const NOW = 1_700_000_000_000;

function record(alias) {
  return {
    alias,
    line: `vless=192.0.2.10:443, obfs-host=old.example.com, tag=${alias}`,
    node_token: `tok-${alias}`,
    xray: { sni: "old.example.com", dest_ip: "192.0.2.10" },
  };
}

function fakeKv(entries) {
  const store = new Map(Object.entries(entries));
  return {
    store,
    async get(key) {
      return store.has(key) ? store.get(key) : null;
    },
    async put(key, value) {
      store.set(key, value);
    },
    async delete(key) {
      store.delete(key);
    },
    lists: 0,
    async list({ prefix = "" } = {}) {
      this.lists += 1;
      const keys = [...store.keys()].filter((name) => name.startsWith(prefix));
      return { keys: keys.map((name) => ({ name })), list_complete: true };
    },
  };
}

function setup(extra = {}) {
  const kv = fakeKv({
    "meta:control": JSON.stringify(META),
    "node:b2": JSON.stringify(record("b2")),
    "node:a1": JSON.stringify(record("a1")),
    ...extra,
  });
  let clock = NOW;
  const scripts = { check: 'const C = __CNSR_CONFIG__;', checkUi: 'ui(__CNSR_CONFIG__);' };
  const handler = makeHandler({ connect: () => {}, scripts, now: () => clock });
  const call = (method, path, body) =>
    handler(
      new Request(`https://c.example.com${path}`, {
        method,
        body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
      }),
      { CNSR: kv },
    );
  const status = async () => (await call("GET", "/ct/status")).json();
  const report = async (body) => {
    const response = await call("POST", "/ct/report", body);
    return { response, body: response.status === 200 ? await response.json() : null };
  };
  const state = (alias) => JSON.parse(kv.store.get(`state:${alias}`));
  return { kv, call, status, report, state, advance: (ms) => (clock += ms) };
}

const paused = (since) => ({ "state:a1": JSON.stringify({ version: 2, sni: "n.example.com", request: null, paused_since: since, exhausted: false, event: null }) });

test("status lists every node with its state", async () => {
  const { status } = setup({
    ...paused(NOW - 1000),
    "state:b2": JSON.stringify({ version: 3, sni: "x.example.com", request: { at: NOW, version: 3 }, paused_since: null, exhausted: true, event: null }),
  });
  assert.deepEqual(await status(), {
    snippet: "https://c.example.com/ct/cnsr.snippet",
    nodes: {
      a1: { version: 2, paused: true, pause_expired: false, pending: false, exhausted: false },
      b2: { version: 3, paused: false, pause_expired: false, pending: true, exhausted: true },
    },
  });
  assert.deepEqual(Object.keys((await status()).nodes), ["a1", "b2"]);
});

test("status reports defaults for a node without state", async () => {
  const { status } = setup();
  assert.deepEqual((await status()).nodes.a1, { version: 1, paused: false, pause_expired: false, pending: false, exhausted: false });
});

test("a failed report creates one request", async () => {
  const { report, state } = setup();
  const first = await report({ alias: "a1", kind: "failed", version: 1 });
  assert.deepEqual(first.body, { accepted: true, reason: null });
  assert.deepEqual(state("a1").request, { at: NOW, version: 1 });
  const second = await report({ alias: "a1", kind: "failed", version: 1 });
  assert.deepEqual(second.body, { accepted: true, reason: "pending" });
  assert.deepEqual(state("a1").request, { at: NOW, version: 1 });
});

test("a report for an old version is ignored", async () => {
  const { report, state } = setup(paused(null));
  const failed = await report({ alias: "a1", kind: "failed", version: 1 });
  assert.deepEqual(failed.body, { accepted: false, reason: "stale" });
  assert.equal(state("a1").request, null);
  const ok = await report({ alias: "a1", kind: "ok", version: 1 });
  assert.deepEqual(ok.body, { accepted: false, reason: "stale" });
});

test("reports are ignored during the pause", async () => {
  const { report, state } = setup(paused(NOW - 1000));
  const failed = await report({ alias: "a1", kind: "failed", version: 2 });
  assert.deepEqual(failed.body, { accepted: false, reason: "paused" });
  assert.equal(state("a1").request, null);
  assert.equal(state("a1").paused_since, NOW - 1000);
});

test("the pause lifts after 24 hours", async () => {
  const { report, status, state, advance } = setup(paused(NOW));
  assert.equal(PAUSE_FALLBACK_MS, 24 * 60 * 60 * 1000);
  advance(PAUSE_FALLBACK_MS);
  const node = (await status()).nodes.a1;
  assert.equal(node.paused, false);
  assert.equal(node.pause_expired, true);
  const failed = await report({ alias: "a1", kind: "failed", version: 2 });
  assert.deepEqual(failed.body, { accepted: true, reason: null });
  assert.equal(state("a1").paused_since, null);
  assert.deepEqual(state("a1").request, { at: NOW + PAUSE_FALLBACK_MS, version: 2 });
});

test("an ok report with the new version ends the pause", async () => {
  const { report, state } = setup(paused(NOW - 1000));
  const ok = await report({ alias: "a1", kind: "ok", version: 2 });
  assert.deepEqual(ok.body, { accepted: true, reason: null });
  assert.equal(state("a1").paused_since, null);
  const again = await report({ alias: "a1", kind: "ok", version: 2 });
  assert.deepEqual(again.body, { accepted: false, reason: "stale" });
});

test("an exhausted node takes no request", async () => {
  const { report, state } = setup({
    "state:a1": JSON.stringify({ version: 1, sni: "old.example.com", request: null, paused_since: null, exhausted: true, event: null }),
  });
  const failed = await report({ alias: "a1", kind: "failed", version: 1 });
  assert.deepEqual(failed.body, { accepted: false, reason: "exhausted" });
  assert.equal(state("a1").request, null);
});

test("report rejects unknown aliases and bad bodies", async () => {
  const { report, call } = setup();
  assert.equal((await report({ alias: "zz", kind: "failed", version: 1 })).response.status, 404);
  assert.equal((await report({ alias: "a1", kind: "nope", version: 1 })).response.status, 400);
  assert.equal((await report({ alias: "a1", kind: "failed", version: "1" })).response.status, 400);
  assert.equal((await report({ kind: "failed", version: 1 })).response.status, 400);
  assert.equal((await call("POST", "/ct/report", "not json")).status, 400);
  assert.equal((await call("POST", "/ct/report", [1])).status, 400);
});

test("wrong client token is 404 everywhere", async () => {
  const { call } = setup();
  assert.equal((await call("GET", "/bad/status")).status, 404);
  assert.equal((await call("POST", "/bad/report", { alias: "a1", kind: "failed", version: 1 })).status, 404);
  assert.equal((await call("GET", "/bad/cnsr.snippet")).status, 404);
  assert.equal((await call("POST", "/ct/status")).status, 404);
  assert.equal((await call("GET", "/ct/report")).status, 404);
});

test("check scripts get their endpoints filled in", async () => {
  const { call } = setup();
  const config = JSON.stringify({ status: "https://c.example.com/ct/status", report: "https://c.example.com/ct/report" });
  for (const [path, template] of [["check.js", "const C = CONFIG;"], ["check-ui.js", "ui(CONFIG);"]]) {
    const response = await call("GET", `/ct/${path}`);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("content-type"), "application/javascript; charset=utf-8");
    assert.equal(await response.text(), template.replace("CONFIG", config));
  }
  assert.equal((await call("GET", "/wrong/check.js")).status, 404);
});

test("an ok report after the pause expired ends it", async () => {
  const { report, status, state, advance } = setup(paused(NOW));
  advance(PAUSE_FALLBACK_MS + 1000);
  assert.equal((await status()).nodes.a1.pause_expired, true);
  const ok = await report({ alias: "a1", kind: "ok", version: 2 });
  assert.deepEqual(ok.body, { accepted: true, reason: null });
  assert.equal(state("a1").paused_since, null);
  assert.equal((await status()).nodes.a1.pause_expired, false);
});

// The long-press check sends `confirmed` once you updated as it asked and the node still fails:
// the pause only waits for devices on the old config, and this one is not.
test("a confirmed failure report is taken during the pause", async () => {
  const { report, state } = setup(paused(NOW - CONFIRM_AFTER_MS));
  const failed = await report({ alias: "a1", kind: "failed", version: 2, confirmed: true });
  assert.deepEqual(failed.body, { accepted: true, reason: null });
  assert.deepEqual(state("a1").request, { at: NOW, version: 2 });
  assert.equal(state("a1").paused_since, null);
});

test("a confirmed report in the first minutes of a pause waits like any other", async () => {
  // A device may report before it fetched the rotated config; ten minutes give it time to.
  assert.equal(CONFIRM_AFTER_MS, 10 * 60 * 1000);
  const { report, state } = setup(paused(NOW - CONFIRM_AFTER_MS + 1000));
  const failed = await report({ alias: "a1", kind: "failed", version: 2, confirmed: true });
  assert.deepEqual(failed.body, { accepted: false, reason: "paused" });
  assert.equal(state("a1").request, null);
});

test("only a true confirmed counts", async () => {
  const { report, state } = setup(paused(NOW - CONFIRM_AFTER_MS));
  const failed = await report({ alias: "a1", kind: "failed", version: 2, confirmed: "yes" });
  assert.deepEqual(failed.body, { accepted: false, reason: "paused" });
  assert.equal(state("a1").request, null);
});

test("with the node index, status and snippet read it instead of listing", async () => {
  const { kv, call, status } = setup({ "index:nodes": JSON.stringify(["b2", "a1"]) });
  assert.deepEqual(Object.keys((await status()).nodes), ["a1", "b2"]);
  const snippet = await (await call("GET", "/ct/cnsr.snippet")).text();
  assert.equal(snippet, `${record("a1").line}\n${record("b2").line}\n`);
  assert.equal(kv.lists, 0);
});

test("an alias in the index without a node record is skipped", async () => {
  const { kv, call, status } = setup({ "index:nodes": JSON.stringify(["a1", "gone", "b2"]), "state:gone": "{}" });
  assert.deepEqual(Object.keys((await status()).nodes), ["a1", "b2"]);
  const snippet = await (await call("GET", "/ct/cnsr.snippet")).text();
  assert.equal(snippet.split("\n").length, 3);
  assert.equal(kv.lists, 0);
});

test("without a usable node index, status and snippet list the nodes", async () => {
  for (const extra of [{}, { "index:nodes": "garbage" }, { "index:nodes": JSON.stringify({ a1: 1 }) }]) {
    const { kv, call, status } = setup(extra);
    assert.deepEqual(Object.keys((await status()).nodes), ["a1", "b2"]);
    const snippet = await (await call("GET", "/ct/cnsr.snippet")).text();
    assert.equal(snippet, `${record("a1").line}\n${record("b2").line}\n`);
    assert.equal(kv.lists, 2);
  }
});
