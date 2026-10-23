// The long-press check: three tests five seconds apart, an immediate report, and a result page.

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadScript, runScript } from "./qx-env.mjs";
import { CONFIG, HANDSHAKE, OK, START, TOKEN, UNREACHABLE, world } from "./qx-world.mjs";

const UI_SOURCE = loadScript(new URL("../../src/cnsr/assets/qx/qx-check-ui.js", import.meta.url), CONFIG);
const STATE_KEY = "cnsr.state";

function press({ versions = { a1: 1, b2: 1 }, results, params = "a1", state, prefs = {}, now = START, ...rest } = {}) {
  const net = world({ versions, results: results ?? (() => HANDSHAKE), ...rest });
  const saved = state === undefined ? prefs : { ...prefs, [STATE_KEY]: JSON.stringify(state) };
  return runScript(UI_SOURCE, {
    now,
    prefs: saved,
    environment: { params },
    fetch: net.fetch,
    benchmark: net.benchmark,
  }).then((run) => ({ ...run, net }));
}

// The first test fails its handshake, the rest pass (five seconds apart share a minute).
function mixed() {
  let calls = 0;
  return () => ((calls += 1) === 1 ? HANDSHAKE : OK);
}

const stateOf = (run) => JSON.parse(run.prefs[STATE_KEY]);
const text = (run) => `${run.done.title} ${run.done.htmlMessage}`;

function assertShown(run) {
  assert.equal(run.doneCount, 1);
  assert.equal(typeof run.done.title, "string");
  assert.equal(typeof run.done.htmlMessage, "string");
  assert.ok(!text(run).includes(TOKEN));
  for (const line of run.logs) assert.ok(!line.includes(TOKEN));
  assert.deepEqual(run.notifications, []);
}

test("test_three_handshake_failures_report_at_once", async () => {
  const run = await press();
  assertShown(run);
  assert.deepEqual(run.benchmarks.map((b) => b.at - START), [0, 5000, 10000]);
  for (const b of run.benchmarks) assert.deepEqual(b.tags, ["a1"]);
  assert.deepEqual(run.net.reports.map((r) => r.body), [{ alias: "a1", kind: "failed", version: 1 }]);
  assert.deepEqual(stateOf(run).waiting.a1, { version: 1, reason: "reported", since: START + 10000 });
  assert.match(text(run), /已通知服务器/);
});

test("test_a_report_keeps_the_scheduled_scripts_other_state", async () => {
  const state = { next: 5, confirm: {}, passed: { a1: 1 }, notified: { b2: "x" }, waiting: {}, alerts: {}, extra: 7 };
  const run = await press({ state });
  const saved = stateOf(run);
  assert.equal(saved.next, 5);
  assert.deepEqual(saved.notified, { b2: "x" });
  assert.equal(saved.extra, 7);
  assert.equal(saved.passed.a1, 1);
});

test("test_an_old_version_asks_to_update_first", async () => {
  const run = await press({ versions: { a1: 2, b2: 1 }, state: { passed: { a1: 1 } } });
  assertShown(run);
  assert.deepEqual(run.net.reports, []);
  assert.match(text(run), /服务器已有新配置，请长按风车更新外部资源后再检测/);
  assert.equal(stateOf(run).waiting?.a1, undefined);
});

test("test_a_second_press_after_updating_reports", async () => {
  const first = await press({ versions: { a1: 2, b2: 1 }, state: { passed: { a1: 1 } } });
  const second = await press({
    versions: { a1: 2, b2: 1 },
    prefs: first.prefs,
    now: START + 599000,
  });
  assertShown(second);
  // you updated as asked: the control plane takes it even while it waits for a pass
  assert.deepEqual(second.net.reports.map((r) => r.body), [{ alias: "a1", kind: "failed", version: 2, confirmed: true }]);
  const saved = stateOf(second);
  assert.equal(saved.waiting.a1.version, 2);
  assert.equal(saved.waiting.a1.reason, "reported");
  assert.equal(saved.passed.a1, 2);
});

test("test_a_second_press_after_the_window_asks_again", async () => {
  const first = await press({ versions: { a1: 2, b2: 1 }, state: { passed: { a1: 1 } } });
  const late = await press({ versions: { a1: 2, b2: 1 }, prefs: first.prefs, now: START + 601000 });
  assert.deepEqual(late.net.reports, []);
  assert.match(text(late), /请长按风车更新/);
});

test("test_a_node_outside_cnsr_is_left_alone", async () => {
  for (const params of ["manual-1", null, "", {}, { node: "manual-1" }]) {
    const run = await press({ params });
    assertShown(run);
    assert.deepEqual(run.net.reports, []);
    assert.deepEqual(run.benchmarks, []);
    assert.match(text(run), /这不是 cnsr 管理的节点/);
  }
});

test("test_the_selected_node_may_arrive_as_text_or_as_an_object", async () => {
  for (const params of ["a1", { node: "a1" }, { tag: "a1" }, '{"node":"a1"}']) {
    const run = await press({ params });
    assert.equal(run.net.reports.length, 1, JSON.stringify(params));
    assert.deepEqual(run.benchmarks[0].tags, ["a1"]);
  }
});

test("test_the_result_is_shown_in_the_ui", async () => {
  const cases = [
    [() => OK, /正常/],
    [() => UNREACHABLE, /IP 被封/],
    [mixed(), /不稳定/],
    [() => undefined, /没有拿到/],
  ];
  for (const [results, pattern] of cases) {
    const run = await press({ results });
    assertShown(run);
    assert.deepEqual(run.net.reports, []);
    assert.match(text(run), pattern);
  }
});

test("test_an_unreachable_domestic_site_means_your_network", async () => {
  const run = await press({ domestic: false });
  assertShown(run);
  assert.deepEqual(run.benchmarks, []);
  assert.deepEqual(run.net.reports, []);
  assert.match(text(run), /你当前的网络连不上国内网站/);
});

test("test_a_pass_clears_the_prompt_and_records_the_version", async () => {
  const run = await press({ results: () => OK, state: { passed: { a1: 1 } }, versions: { a1: 2, b2: 1 } });
  assert.equal(stateOf(run).passed.a1, 2);
});

test("test_a_pass_on_a_new_config_is_reported", async () => {
  // The control plane waits for one after a rotation before it takes failures again.
  const run = await press({ results: () => OK, state: { passed: { a1: 1 } }, versions: { a1: 2, b2: 1 } });
  assert.deepEqual(run.net.reports.map((r) => r.body), [{ alias: "a1", kind: "ok", version: 2 }]);
  assert.match(text(run), /正常/);
});

test("test_a_pass_while_the_server_waits_for_one_is_reported", async () => {
  const run = await press({ results: () => OK, state: { passed: { a1: 2 } }, versions: { a1: 2, b2: 1 }, flags: () => ({ paused: true }) });
  assert.deepEqual(run.net.reports.map((r) => r.body), [{ alias: "a1", kind: "ok", version: 2 }]);
});

test("test_a_pass_on_a_known_config_reports_nothing", async () => {
  const run = await press({ results: () => OK, state: { passed: { a1: 2 } }, versions: { a1: 2, b2: 1 } });
  assert.deepEqual(run.net.reports, []);
});

test("test_a_failure_on_a_measured_config_during_the_pause_is_confirmed", async () => {
  // This device measured the new config up before (its pass may not have reached the server).
  const run = await press({
    state: { passed: { a1: 2 }, measured: { a1: 2 } },
    versions: { a1: 2, b2: 1 },
    flags: () => ({ paused: true }),
  });
  assert.deepEqual(run.net.reports.map((r) => r.body), [{ alias: "a1", kind: "failed", version: 2, confirmed: true }]);
});

test("test_the_report_answers_are_explained", async () => {
  for (const [reason, pattern] of [
    ["exhausted", /备选 SNI 已经用完，或者都没通过换之前的复验/],
    ["paused", /刚被服务器换过/],
    ["stale", /更新外部资源/],
  ]) {
    const run = await press({ answer: () => ({ accepted: false, reason }) });
    assertShown(run);
    assert.match(text(run), pattern, reason);
  }
});

test("test_errors_still_end_with_one_result", async () => {
  const control = () => ({ statusCode: 500, body: "" });
  const down = await press({ control });
  assertShown(down);
  const lost = await press({ answer: () => { throw new Error(`boom ${TOKEN}`); } });
  assertShown(lost);
  assert.deepEqual(lost.net.reports.length, 1);
  // A lost report enters no fast mode; only the version first seen is kept.
  assert.deepEqual(stateOf(lost), { passed: { a1: 1 } });
});

test("test_a_node_first_seen_is_taken_to_be_on_that_version", async () => {
  // No result the first time: nothing passed, yet the version seen is recorded.
  const first = await press({ results: () => undefined });
  assertShown(first);
  assert.equal(stateOf(first).passed.a1, 1);
  // The server rotated since: the device is asked to update instead of reporting version 2.
  const second = await press({ versions: { a1: 2, b2: 1 }, prefs: first.prefs, now: START + 3600000 });
  assertShown(second);
  assert.deepEqual(second.net.reports, []);
  assert.match(text(second), /服务器已有新配置/);
});


test("test_a_version_only_seen_in_status_is_not_confirmed", async () => {
  // First sight takes the status version as the device's, which it may not have loaded yet.
  const run = await press({ versions: { a1: 2, b2: 1 }, flags: () => ({ paused: true }) });
  assert.deepEqual(run.net.reports.map((r) => r.body), [{ alias: "a1", kind: "failed", version: 2 }]);
});

test("test_a_pass_records_the_measured_version", async () => {
  const run = await press({ results: () => OK, versions: { a1: 2, b2: 1 } });
  assert.equal(stateOf(run).measured.a1, 2);
});
