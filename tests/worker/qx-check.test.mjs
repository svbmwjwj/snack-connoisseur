import { test } from "node:test";
import assert from "node:assert/strict";
import { runEveryMinute, runScript } from "./qx-env.mjs";
import {
  CONFIG,
  HANDSHAKE,
  MINUTE,
  OK,
  SOURCE,
  START,
  TOKEN,
  UNREACHABLE,
  UPDATE_LINK,
  allNotifications,
  assertDoneOnce,
  benchmarkMinutes,
  world,
} from "./qx-world.mjs";

test("test_nothing_happens_before_the_next_run", async () => {
  const net = world({ versions: { a1: 1, b2: 1 }, results: () => OK });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 61, ...net });
  assertDoneOnce(runs);
  for (const run of runs.slice(1, 60)) {
    assert.deepEqual(run.requests, [], `minute ${run.minute} made a request`);
    assert.deepEqual(run.benchmarks, []);
    assert.deepEqual(run.prefs, runs[0].prefs, `minute ${run.minute} wrote $prefs`);
  }
  assert.deepEqual(benchmarkMinutes(runs), [0, 60]);
  assert.deepEqual(runs[0].benchmarks[0].tags.sort(), ["a1", "b2"]);
});

test("test_no_domestic_network_means_a_silent_exit", async () => {
  const net = world({ versions: { a1: 1, b2: 1 }, results: () => HANDSHAKE, domestic: false });
  const run = await runScript(SOURCE, { now: START, ...net });
  assert.equal(run.doneCount, 1);
  assert.equal(run.requests.length, 1);
  assert.equal(run.requests[0].url, "https://www.baidu.com");
  assert.deepEqual(run.requests[0].opts, { policy: "direct" });
  assert.deepEqual(run.benchmarks, []);
  assert.deepEqual(run.notifications, []);
});

test("test_a_missing_benchmark_result_is_skipped_not_failed", async () => {
  const net = world({ versions: { a1: 1, b2: 1 }, results: (alias) => (alias === "a1" ? undefined : OK) });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 10, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(benchmarkMinutes(runs), [0]);
  assert.deepEqual(allNotifications(runs), []);
  assert.deepEqual(net.reports, []);
});

test("test_three_handshake_failures_with_another_node_up_are_reported", async () => {
  const net = world({ versions: { a1: 1, b2: 1 }, results: (alias) => (alias === "a1" ? HANDSHAKE : OK) });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 12, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(benchmarkMinutes(runs), [0, 3, 5, 6]);
  assert.equal(net.reports.length, 1);
  const [report] = net.reports;
  assert.equal(report.minute, 6);
  assert.deepEqual(report.body, { alias: "a1", kind: "failed", version: 1 });
  assert.equal(report.request.headers["User-Agent"], "cnsr-qx/1");
  const notes = allNotifications(runs);
  assert.equal(notes.length, 1);
  assert.equal(notes[0].at, START + 6 * MINUTE);
  assert.match(notes[0].body, /节点 a1 的 SNI 可能被封，已通知服务器更换。/);
  for (const request of runs[6].requests.filter((r) => r.url.startsWith("https://c.example.com"))) {
    assert.equal(request.headers["User-Agent"], "cnsr-qx/1");
  }
});

test("test_a_recovery_during_confirmation_cancels_the_report", async () => {
  const net = world({
    versions: { a1: 1, b2: 1 },
    results: (alias, minute) => (alias === "a1" && minute < 5 ? HANDSHAKE : OK),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 12, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(benchmarkMinutes(runs), [0, 3, 5]);
  assert.deepEqual(net.reports, []);
  assert.deepEqual(allNotifications(runs), []);
});

test("test_all_nodes_failing_is_your_network_and_stays_quiet", async () => {
  const net = world({ versions: { a1: 1, b2: 1 }, results: (alias) => (alias === "a1" ? HANDSHAKE : UNREACHABLE) });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 12, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(benchmarkMinutes(runs), [0]);
  assert.deepEqual(net.reports, []);
  assert.deepEqual(allNotifications(runs), []);
});

test("test_a_single_node_notifies_but_does_not_report", async () => {
  const net = world({ versions: { a1: 1 }, results: () => HANDSHAKE });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 12, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(benchmarkMinutes(runs), [0, 3, 5, 6]);
  assert.deepEqual(net.reports, []);
  const notes = allNotifications(runs);
  assert.equal(notes.length, 1);
  assert.equal(notes[0].at, START + 6 * MINUTE);
  assert.match(notes[0].body, /节点 a1 握手失败，可能是 SNI 被封。只有一个节点/);
  assert.match(notes[0].body, /cnsr 检测/);
});

test("test_a_tcp_failure_notifies_once_and_never_reports", async () => {
  const net = world({ versions: { a1: 1, b2: 1 }, results: (alias) => (alias === "a1" ? UNREACHABLE : OK) });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 125, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(benchmarkMinutes(runs), [0, 60, 120]);
  assert.deepEqual(net.reports, []);
  const notes = allNotifications(runs);
  assert.equal(notes.length, 1);
  assert.equal(notes[0].at, START);
  assert.match(notes[0].body, /节点 a1 连不上，可能是 IP 被封，换 SNI 解决不了。/);
  assert.doesNotMatch(notes[0].body, /tcp|ms|\d{2,}/i);
});

test("test_a_device_on_an_old_version_reminds_instead_of_reporting", async () => {
  // Minute 0: both nodes pass on version 1. Later the server moves a1 to version 2,
  // and the device, still on the old config, fails its handshake.
  const net = world({
    versions: (minute) => ({ a1: minute < 60 ? 1 : 2, b2: 1 }),
    results: (alias, minute) => (alias === "a1" && minute >= 60 ? HANDSHAKE : OK),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 70, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(benchmarkMinutes(runs.slice(0, 67)), [0, 60, 63, 65, 66]);
  // From then on the device waits for the update, benchmarking only a1 every minute.
  for (const run of runs.slice(67)) assert.deepEqual(run.benchmarks.map((b) => b.tags), [["a1"]]);
  assert.deepEqual(net.reports, []);
  const notes = allNotifications(runs);
  assert.equal(notes.length, 1);
  assert.equal(notes[0].at, START + 66 * MINUTE);
  assert.match(notes[0].body, /节点 a1 有新配置，请长按风车更新外部资源。/);
  assert.equal(notes[0].extra["open-url"], UPDATE_LINK);
});

test("test_one_notification_per_run", async () => {
  // a1 is confirmed at minute 6, the same run in which c3 stops answering.
  const net = world({
    versions: { a1: 1, b2: 1, c3: 1 },
    results: (alias, minute) => {
      if (alias === "a1") return HANDSHAKE;
      if (alias === "c3" && minute >= 6) return UNREACHABLE;
      return OK;
    },
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 12, ...net });
  assertDoneOnce(runs);
  assert.equal(net.reports.length, 1);
  const notes = allNotifications(runs);
  assert.equal(notes.length, 1);
  assert.equal(notes[0].at, START + 6 * MINUTE);
  assert.match(notes[0].body, /节点 a1 的 SNI 可能被封/);
  assert.match(notes[0].body, /节点 c3 连不上/);
});

test("test_the_client_token_never_reaches_the_log", async () => {
  const handshakeOnA1 = (alias) => (alias === "a1" ? HANDSHAKE : OK);
  const scenarios = {
    "status answers 500": (request) => (request.url === CONFIG.status ? { statusCode: 500, body: CONFIG.status } : null),
    "report answers 500": (request) => (request.url === CONFIG.report ? { statusCode: 500, body: "" } : null),
    "fetch rejects with the URL": (request) => {
      if (request.url.startsWith("https://c.example.com")) throw new Error(`connect failed: ${request.url}`);
      return null;
    },
  };
  for (const [name, control] of Object.entries(scenarios)) {
    const net = world({ versions: { a1: 1, b2: 1 }, results: handshakeOnA1, control });
    const runs = await runEveryMinute(SOURCE, { start: START, minutes: 8, ...net });
    assertDoneOnce(runs);
    const logs = runs.flatMap((run) => run.logs);
    assert.ok(logs.length > 0, `${name}: nothing was logged`);
    for (const line of logs) {
      assert.ok(!line.includes(TOKEN), `${name}: token in log line ${line}`);
      assert.doesNotMatch(line, /\[object Object\]/, `${name}: unreadable log line`);
    }
    if (name === "fetch rejects with the URL") assert.match(logs.join("\n"), /connect failed/);
    assert.deepEqual(allNotifications(runs), [], `${name}: notified`);
  }
});

test("test_a_pass_records_the_measured_version_and_keeps_the_others", async () => {
  // `measured` is shared with the long-press check: only a version measured up here counts there.
  const net = world({ versions: { a1: 2, b2: 1 }, results: () => OK });
  const prefs = { "cnsr.state": JSON.stringify({ measured: { gone: 1 } }) };
  const run = await runScript(SOURCE, { now: START, prefs, ...net });
  assert.deepEqual(JSON.parse(run.prefs["cnsr.state"]).measured, { a1: 2, b2: 1 });
});
