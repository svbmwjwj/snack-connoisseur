import { test } from "node:test";
import assert from "node:assert/strict";
import { makeHandler } from "../../src/cnsr/assets/worker/handler.js";

const META = {
  host: "c.example.com",
  zone: "example.com",
  client_token: "ct",
  admin_token: "at",
};

function kv(entries, { pageSize = 1000 } = {}) {
  const store = new Map(Object.entries(entries));
  return {
    async put(key, value) {
      store.set(key, value);
    },
    async delete(key) {
      store.delete(key);
    },
    async get(key) {
      return store.has(key) ? store.get(key) : null;
    },
    async list({ prefix = "", cursor } = {}) {
      const names = [...store.keys()].filter((k) => k.startsWith(prefix)).sort();
      const start = cursor === undefined ? 0 : Number(cursor);
      const page = names.slice(start, start + pageSize);
      const end = start + page.length;
      const keys = page.map((name) => ({ name }));
      return end >= names.length
        ? { keys, list_complete: true }
        : { keys, list_complete: false, cursor: String(end) };
    },
  };
}

function node(line) {
  return JSON.stringify({ line });
}

function envWith(extra = {}, meta = META) {
  return { CNSR: kv({ "meta:control": JSON.stringify(meta), ...extra }) };
}

// A connect() stand-in that records calls and hands out sockets whose
// `opened` promise comes from the given factory.
function fakeConnect(makeOpened = () => Promise.resolve({})) {
  const calls = [];
  const sockets = [];
  function connect(options) {
    calls.push(options);
    const socket = {
      closed: 0,
      opened: makeOpened(),
      close() {
        this.closed += 1;
        return Promise.resolve();
      },
    };
    sockets.push(socket);
    return socket;
  }
  return { connect, calls, sockets };
}

function getSnippet(handler, env, token = "ct") {
  return handler(new Request(`https://c.example.com/${token}/cnsr.snippet`), env);
}

function probe(handler, env, body, headers = { authorization: "Bearer at" }) {
  return handler(
    new Request("https://c.example.com/admin/probe", {
      method: "POST",
      headers,
      body: typeof body === "string" ? body : JSON.stringify(body),
    }),
    env,
  );
}

test("503 when meta missing", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const env = { CNSR: kv({}) };
  assert.equal((await getSnippet(handler, env)).status, 503);
  const res = await probe(handler, env, { host: "192.0.2.10", port: 443 });
  assert.equal(res.status, 503);
});

test("503 when meta is unusable", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const bad = [
    "not json",
    "null",
    "[]",
    JSON.stringify({ ...META, client_token: undefined }),
    JSON.stringify({ ...META, admin_token: undefined }),
    JSON.stringify({ ...META, client_token: "" }),
    JSON.stringify({ ...META, admin_token: "" }),
    JSON.stringify({ ...META, client_token: 7 }),
  ];
  for (const raw of bad) {
    const env = { CNSR: kv({ "meta:control": raw }) };
    assert.equal((await getSnippet(handler, env)).status, 503, raw);
    const res = await probe(handler, env, { host: "192.0.2.10", port: 443 });
    assert.equal(res.status, 503, raw);
  }
});

test("snippet lists lines sorted by alias", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const env = envWith({ "node:b": node("B"), "node:a": node("A") });
  const res = await getSnippet(handler, env);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "text/plain; charset=utf-8");
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.equal(await res.text(), "A\nB\n");
});

test("snippet sorts by alias, not by insertion or value", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const env = envWith({
    "node:jp-2": node("z-line"),
    "node:jp-10": node("y-line"),
    "node:jp-1": node("x-line"),
  });
  const res = await getSnippet(handler, env);
  assert.equal(await res.text(), "x-line\ny-line\nz-line\n");
});

test("snippet follows the list cursor until the listing is complete", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const entries = {};
  for (const alias of ["e", "c", "a", "d", "b"]) {
    entries[`node:${alias}`] = node(alias.toUpperCase());
  }
  const env = { CNSR: kv({ "meta:control": JSON.stringify(META), ...entries }, { pageSize: 2 }) };
  const res = await getSnippet(handler, env);
  assert.equal(await res.text(), "A\nB\nC\nD\nE\n");
});

test("snippet ignores keys outside the node prefix and unusable node values", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const env = envWith({
    "node:a": node("A"),
    "node:broken": "not json",
    "node:noline": JSON.stringify({ other: 1 }),
    "node:numeric": JSON.stringify({ line: 5 }),
    "nodes:x": node("X"),
    "other:a": node("O"),
  });
  const res = await getSnippet(handler, env);
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "A\n");
});

test("snippet with no nodes is an empty 200", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const res = await getSnippet(handler, envWith());
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "");
});

test("snippet with wrong token is 404", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const env = envWith({ "node:a": node("A") });
  for (const token of ["nope", "c", "ctx", "at", "CT"]) {
    assert.equal((await getSnippet(handler, env, token)).status, 404, token);
  }
});

test("unknown paths and methods are 404", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const env = envWith({ "node:a": node("A") });
  for (const path of ["/", "/ct", "/ct/", "/ct/other", "/ct/cnsr.snippet/x", "//cnsr.snippet"]) {
    const res = await handler(new Request(`https://c.example.com${path}`), env);
    assert.equal(res.status, 404, path);
  }
  const post = await handler(
    new Request("https://c.example.com/ct/cnsr.snippet", { method: "POST", body: "x" }),
    env,
  );
  assert.equal(post.status, 404);
});

test("probe requires admin bearer", async () => {
  const { connect, calls } = fakeConnect();
  const handler = makeHandler({ connect });
  const env = envWith();
  const body = { host: "192.0.2.10", port: 443 };
  const cases = [
    {},
    { authorization: "Bearer ct" },
    { authorization: "Bearer a" },
    { authorization: "Bearer att" },
    { authorization: "Bearer " },
    { authorization: "at" },
    { authorization: "Basic at" },
  ];
  for (const headers of cases) {
    const res = await probe(handler, env, body, headers);
    assert.equal(res.status, 401, JSON.stringify(headers));
  }
  // Auth is checked before the body is even parsed.
  assert.equal((await probe(handler, env, "not json", {})).status, 401);
  assert.equal(calls.length, 0);
});

test("probe is POST only", async () => {
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const res = await handler(
    new Request("https://c.example.com/admin/probe", {
      headers: { authorization: "Bearer at" },
    }),
    envWith(),
  );
  assert.equal(res.status, 405);
});

test("probe reports open port", async () => {
  const { connect, calls, sockets } = fakeConnect();
  const handler = makeHandler({ connect });
  const res = await probe(handler, envWith(), { host: "192.0.2.10", port: 443 });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type"), /^application\/json/);
  const out = await res.json();
  assert.equal(out.ok, true);
  assert.ok(Number.isInteger(out.ms) && out.ms >= 0);
  assert.equal(out.error, null);
  assert.deepEqual(calls, [{ hostname: "192.0.2.10", port: 443 }]);
  assert.equal(sockets[0].closed, 1);
});

test("probe reports failure", async () => {
  const { connect, sockets } = fakeConnect(() => Promise.reject(new Error("connection refused")));
  const handler = makeHandler({ connect });
  const res = await probe(handler, envWith(), { host: "192.0.2.10", port: 443 });
  assert.equal(res.status, 200);
  const out = await res.json();
  assert.equal(out.ok, false);
  assert.equal(out.ms, null);
  assert.equal(typeof out.error, "string");
  assert.match(out.error, /connection refused/);
  assert.equal(sockets[0].closed, 1);
});

test("probe reports a timeout after 5 seconds and still closes the socket", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let connected;
  const connectedPromise = new Promise((resolve) => {
    connected = resolve;
  });
  const { connect, sockets } = fakeConnect(() => new Promise(() => {}));
  const handler = makeHandler({
    connect(options) {
      const socket = connect(options);
      connected();
      return socket;
    },
  });
  const pending = probe(handler, envWith(), { host: "192.0.2.10", port: 443 });
  await connectedPromise;
  t.mock.timers.tick(4999);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(sockets[0].closed, 0);
  t.mock.timers.tick(1);
  const res = await pending;
  const out = await res.json();
  assert.equal(out.ok, false);
  assert.equal(out.ms, null);
  assert.equal(out.error, "timeout");
  assert.equal(sockets[0].closed, 1);
});

test("probe clears its timeout timer once the socket opens", async (t) => {
  const setSpy = t.mock.method(globalThis, "setTimeout");
  const clearSpy = t.mock.method(globalThis, "clearTimeout");
  const { connect } = fakeConnect();
  const handler = makeHandler({ connect });
  const res = await probe(handler, envWith(), { host: "192.0.2.10", port: 443 });
  assert.equal((await res.json()).ok, true);
  const probeTimers = setSpy.mock.calls.filter((c) => c.arguments[1] === 5000);
  assert.equal(probeTimers.length, 1);
  const timerId = probeTimers[0].result;
  assert.ok(clearSpy.mock.calls.some((c) => c.arguments[0] === timerId));
});

test("probe reports failure when connect throws", async () => {
  const handler = makeHandler({
    connect() {
      throw new Error("address not allowed");
    },
  });
  const res = await probe(handler, envWith(), { host: "192.0.2.10", port: 443 });
  assert.equal(res.status, 200);
  const out = await res.json();
  assert.equal(out.ok, false);
  assert.equal(out.ms, null);
  assert.match(out.error, /address not allowed/);
});

test("probe rejects hostnames", async () => {
  const { connect, calls } = fakeConnect();
  const handler = makeHandler({ connect });
  const res = await probe(handler, envWith(), { host: "example.com", port: 443 });
  assert.equal(res.status, 400);
  assert.equal(calls.length, 0);
});

test("probe rejects anything that is not an IP literal", async () => {
  const { connect, calls } = fakeConnect();
  const handler = makeHandler({ connect });
  const hosts = [
    ...["", " ", "localhost", "192.0.2.10:443", "http://192.0.2.10", "[192.0.2.10]"],
    ...["999.0.2.10", "192.0.2", "192.0.2.10.1", "192.0.2.256", "01.0.2.10", "0x7f.0.0.1"],
    ...["2130706433", "192.0.2.10 ", "192.0.2.10\n", "2001:db8:::1", "2001:db8::g", "12345::1"],
    ...["1:2:3:4:5:6:7:8:9", "[2001:db8::1", "2001:db8::1]", "[]", ":", "fe80::1%eth0"],
    ...["[2001:db8::1]:443", "2001:db8::1/64", "::1]/@x[::2", null, 42, ["192.0.2.10"]],
  ];
  for (const host of hosts) {
    const res = await probe(handler, envWith(), { host, port: 443 });
    assert.equal(res.status, 400, JSON.stringify(host));
  }
  const missing = await probe(handler, envWith(), { port: 443 });
  assert.equal(missing.status, 400);
  assert.equal(calls.length, 0);
});

test("probe accepts IPv4 and IPv6 literals and passes the bare address", async () => {
  const cases = [
    ["192.0.2.10", "192.0.2.10"],
    ["0.0.0.0", "0.0.0.0"],
    ["255.255.255.255", "255.255.255.255"],
    ["2001:db8::1", "2001:db8::1"],
    ["[2001:db8::1]", "2001:db8::1"],
    ["2001:DB8:0:0:0:0:0:1", "2001:DB8:0:0:0:0:0:1"],
    ["::1", "::1"],
    ["::", "::"],
    ["::ffff:192.0.2.10", "::ffff:192.0.2.10"],
  ];
  for (const [host, bare] of cases) {
    const { connect, calls } = fakeConnect();
    const handler = makeHandler({ connect });
    const res = await probe(handler, envWith(), { host, port: 8443 });
    assert.equal(res.status, 200, host);
    assert.equal((await res.json()).ok, true, host);
    assert.deepEqual(calls, [{ hostname: bare, port: 8443 }], host);
  }
});

test("probe rejects ports outside 1-65535 or not integers", async () => {
  const { connect, calls } = fakeConnect();
  const handler = makeHandler({ connect });
  for (const port of [0, -1, 65536, 443.5, "443", null, true, [443], {}, NaN]) {
    const res = await probe(handler, envWith(), { host: "192.0.2.10", port });
    assert.equal(res.status, 400, JSON.stringify(port));
  }
  const missing = await probe(handler, envWith(), { host: "192.0.2.10" });
  assert.equal(missing.status, 400);
  for (const port of [1, 65535]) {
    const res = await probe(handler, envWith(), { host: "192.0.2.10", port });
    assert.equal(res.status, 200, String(port));
  }
  assert.deepEqual(calls.map((c) => c.port), [1, 65535]);
});

test("probe rejects malformed or non-object JSON", async () => {
  const { connect, calls } = fakeConnect();
  const handler = makeHandler({ connect });
  for (const body of ["", "{", "not json", "null", "[]", "7", '"192.0.2.10"']) {
    const res = await probe(handler, envWith(), body);
    assert.equal(res.status, 400, JSON.stringify(body));
  }
  assert.equal(calls.length, 0);
});

// workers.dev is the nodes' way in (zone security settings do not apply there): only the node
// API answers on it; clients and cnsr add use the zone's own host.
test("on workers.dev only the node API answers", async () => {
  const env = envWith({ "node:jp-1": JSON.stringify({ alias: "jp-1", line: "vless=x", node_token: "nt" }) });
  const handler = makeHandler({ connect: fakeConnect().connect });
  const dev = "https://cnsr-control.acct-sub.workers.dev";

  const snippet = await handler(new Request(`${dev}/ct/cnsr.snippet`), env);
  assert.equal(snippet.status, 404);
  const probed = await handler(
    new Request(`${dev}/admin/probe`, {
      method: "POST",
      headers: { authorization: "Bearer at" },
      body: JSON.stringify({ host: "192.0.2.1", port: 443 }),
    }),
    env,
  );
  assert.equal(probed.status, 404);

  const task = await handler(new Request(`${dev}/node/jp-1/task`, { headers: { authorization: "Bearer nt" } }), env);
  assert.equal(task.status, 200);
  assert.deepEqual(await task.json(), { rotate: false, version: 1 });

  // the zone's host keeps every API
  assert.equal((await getSnippet(handler, env)).status, 200);
});
