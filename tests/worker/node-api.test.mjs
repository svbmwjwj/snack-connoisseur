import { test } from "node:test";
import assert from "node:assert/strict";
import { makeHandler } from "../../src/cnsr/assets/worker/handler.js";
import { replaceSni } from "../../src/cnsr/assets/worker/store.js";

const META = { host: "c.example.com", zone: "example.com", client_token: "ct", admin_token: "at" };
const NOW = 1_700_000_000_000;

const LINE =
  "vless=192.0.2.10:443, method=none, password=u-1, obfs=over-tls, obfs-host=old.example.com, " +
  "reality-base64-pubkey=pk, reality-hex-shortid=ab, vless-flow=xtls-rprx-vision, udp-relay=true, tag=a1";

function record(alias, token) {
  return {
    alias,
    line: LINE.replace("tag=a1", `tag=${alias}`),
    node_token: token,
    xray: { uuid: "u-1", public_key: "pk", short_id: "ab", sni: "old.example.com", dest_ip: "192.0.2.10" },
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
    async list() {
      return { keys: [...store.keys()].map((name) => ({ name })), list_complete: true };
    },
  };
}

function setup(extra = {}) {
  const kv = fakeKv({
    "meta:control": JSON.stringify(META),
    "node:a1": JSON.stringify(record("a1", "tok-a1")),
    "node:b2": JSON.stringify(record("b2", "tok-b2")),
    ...extra,
  });
  const handler = makeHandler({ connect: () => {}, now: () => NOW });
  const call = (method, path, token, body) =>
    handler(
      new Request(`https://c.example.com${path}`, {
        method,
        headers: token === null ? {} : { authorization: `Bearer ${token}` },
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
      { CNSR: kv },
    );
  return { kv, call };
}

const stateOf = (kv, alias) => JSON.parse(kv.store.get(`state:${alias}`));
const nodeOf = (kv, alias) => JSON.parse(kv.store.get(`node:${alias}`));

test("test_task_needs_the_nodes_own_token", async () => {
  const { call } = setup();
  assert.equal((await call("GET", "/node/a1/task", "tok-b2")).status, 401);
  assert.equal((await call("GET", "/node/a1/task", null)).status, 401);
  assert.equal((await call("GET", "/node/zz/task", "tok-a1")).status, 401);
  const res = await call("GET", "/node/a1/task", "tok-a1");
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await res.json(), { rotate: false, version: 1 });
});

test("task is refused when the record has no token", async () => {
  const bare = { ...record("c3", "x") };
  delete bare.node_token;
  const { call } = setup({ "node:c3": JSON.stringify(bare) });
  assert.equal((await call("GET", "/node/c3/task", "")).status, 401);
  assert.equal((await call("GET", "/node/c3/task", "x")).status, 401);
});

test("test_task_says_rotate_while_a_request_is_pending", async () => {
  const state = { version: 1, sni: "old.example.com", request: { at: 1 }, paused_since: null, exhausted: false, event: null };
  const { call } = setup({ "state:a1": JSON.stringify(state) });
  assert.deepEqual(await (await call("GET", "/node/a1/task", "tok-a1")).json(), { rotate: true, version: 1 });
});

test("test_line_updates_the_record_and_starts_the_pause", async () => {
  const state = { version: 4, sni: "old.example.com", request: { at: 1 }, paused_since: null, exhausted: true, event: null };
  const { kv, call } = setup({ "state:a1": JSON.stringify(state) });
  const res = await call("PUT", "/node/a1/line", "tok-a1", { sni: "new.example.com", dest_ip: "198.51.100.7" });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { version: 5 });
  const node = nodeOf(kv, "a1");
  assert.equal(node.line, LINE.replace("old.example.com", "new.example.com"));
  assert.equal(node.xray.sni, "new.example.com");
  assert.equal(node.xray.dest_ip, "198.51.100.7");
  assert.equal(node.xray.uuid, "u-1");
  assert.equal(node.node_token, "tok-a1");
  assert.deepEqual(stateOf(kv, "a1"), {
    version: 5, sni: "new.example.com", request: null, paused_since: NOW, exhausted: false, event: null,
  });
});

test("line works without a prior state and rejects other tokens", async () => {
  const { kv, call } = setup();
  assert.equal((await call("PUT", "/node/a1/line", "tok-b2", { sni: "n.example.com", dest_ip: "192.0.2.1" })).status, 401);
  assert.equal(kv.store.has("state:a1"), false);
  const res = await call("PUT", "/node/a1/line", "tok-a1", { sni: "n.example.com", dest_ip: "2001:db8::1" });
  assert.deepEqual(await res.json(), { version: 2 });
});

test("test_line_rejects_a_bad_body", async () => {
  const { kv, call } = setup();
  const before = kv.store.get("node:a1");
  const bad = [
    { dest_ip: "192.0.2.1" },
    { sni: "not a domain", dest_ip: "192.0.2.1" },
    { sni: "new.example.com", dest_ip: "example.com" },
    { sni: "new.example.com" },
    { sni: "bad,host.example.com", dest_ip: "192.0.2.1" },
  ];
  for (const body of bad) {
    assert.equal((await call("PUT", "/node/a1/line", "tok-a1", body)).status, 400);
  }
  assert.equal(kv.store.get("node:a1"), before);
  assert.equal(kv.store.has("state:a1"), false);
});

test("test_exhausted_event_marks_the_state", async () => {
  const state = { version: 2, sni: "old.example.com", request: { at: 1 }, paused_since: null, exhausted: false, event: null };
  const { kv, call } = setup({ "state:a1": JSON.stringify(state) });
  const failed = await call("POST", "/node/a1/event", "tok-a1", { kind: "rotate_failed", detail: "handshake" });
  assert.equal(failed.status, 204);
  let s = stateOf(kv, "a1");
  assert.equal(s.exhausted, false);
  assert.deepEqual(s.request, { at: 1 });
  assert.deepEqual(s.event, { kind: "rotate_failed", detail: "handshake", at: NOW });
  const done = await call("POST", "/node/a1/event", "tok-a1", { kind: "exhausted", detail: "no candidates" });
  assert.equal(done.status, 204);
  s = stateOf(kv, "a1");
  assert.equal(s.exhausted, true);
  assert.equal(s.request, null);
  assert.equal(s.event.kind, "exhausted");
  assert.equal((await call("POST", "/node/a1/event", "tok-a1", { kind: "other", detail: "" })).status, 400);
});

test("test_replace_sni_only_touches_obfs_host", () => {
  const out = replaceSni(LINE, "new.example.com");
  assert.equal(out, LINE.replace("obfs-host=old.example.com", "obfs-host=new.example.com"));
  assert.equal(replaceSni("vless=h:443, tag=x", "n.example.com"), "vless=h:443, tag=x");
});

test("test_task_carries_the_state_version", async () => {
  const state = { version: 4, sni: "old.example.com", request: { at: 1 }, paused_since: null, exhausted: false, event: null };
  const { call } = setup({ "state:a1": JSON.stringify(state) });
  assert.deepEqual(await (await call("GET", "/node/a1/task", "tok-a1")).json(), { rotate: true, version: 4 });
  assert.deepEqual(await (await call("GET", "/node/b2/task", "tok-b2")).json(), { rotate: false, version: 1 });
});

test("test_task_repairs_a_state_behind_the_nodes_last_version", async () => {
  // A report that read a stale state wrote v1 + a request over the v3 the node had put.
  const state = { version: 1, sni: "old.example.com", request: { at: 1, version: 1 }, paused_since: null, exhausted: false, event: null };
  const { kv, call } = setup({ "state:a1": JSON.stringify(state) });
  const res = await call("GET", "/node/a1/task?version=3", "tok-a1");
  assert.deepEqual(await res.json(), { rotate: false, version: 3 });
  assert.deepEqual(stateOf(kv, "a1"), { ...state, version: 3, request: null, paused_since: NOW });
});

test("test_task_leaves_a_state_at_or_ahead_of_the_node_alone", async () => {
  const state = { version: 3, sni: "old.example.com", request: { at: 1, version: 3 }, paused_since: null, exhausted: false, event: null };
  const raw = JSON.stringify(state);
  for (const given of ["2", "3", "abc", "-1", "1.5", ""]) {
    const { kv, call } = setup({ "state:a1": raw });
    const res = await call("GET", `/node/a1/task?version=${given}`, "tok-a1");
    assert.deepEqual(await res.json(), { rotate: true, version: 3 }, given);
    assert.equal(kv.store.get("state:a1"), raw, given);
  }
});

test("test_a_retried_line_does_not_bump_the_version_twice", async () => {
  const state = { version: 4, sni: "old.example.com", request: { at: 1 }, paused_since: null, exhausted: false, event: null };
  const { kv, call } = setup({ "state:a1": JSON.stringify(state) });
  const body = { sni: "new.example.com", dest_ip: "198.51.100.7" };
  assert.deepEqual(await (await call("PUT", "/node/a1/line", "tok-a1", body)).json(), { version: 5 });
  const after = kv.store.get("state:a1");
  const again = await call("PUT", "/node/a1/line", "tok-a1", body);
  assert.equal(again.status, 200);
  assert.deepEqual(await again.json(), { version: 5 });
  assert.equal(kv.store.get("state:a1"), after);
});

test("test_a_line_over_a_state_behind_the_node_still_goes_up", async () => {
  // A stale state (v1) under a node that last put v7: the new line is v8, never v2.
  const state = { version: 1, sni: "old.example.com", request: { at: 1 }, paused_since: null, exhausted: false, event: null };
  const { kv, call } = setup({ "state:a1": JSON.stringify(state) });
  const res = await call("PUT", "/node/a1/line?version=7", "tok-a1", { sni: "new.example.com", dest_ip: "198.51.100.7" });
  assert.deepEqual(await res.json(), { version: 8 });
  assert.equal(stateOf(kv, "a1").version, 8);
});
