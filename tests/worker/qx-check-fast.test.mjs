// Fast mode after a report: update notices, reminders, pass reports and alerts.

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
  a1Fails,
  allNotifications,
  assertDoneOnce,
  minutesOf,
  range,
  stateOf,
  statusMinutes,
  world,
} from "./qx-world.mjs";

test("test_fast_mode_polls_every_minute_and_links_the_update", async () => {
  const net = world({ versions: (minute) => ({ a1: minute < 9 ? 1 : 2, b2: 1 }), results: a1Fails });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 12, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(net.reports.map((r) => [r.minute, r.body]), [[6, { alias: "a1", kind: "failed", version: 1 }]]);
  assert.deepEqual(statusMinutes(runs), [0, 3, 5, 6, 7, 8, 9, 10, 11]);
  // While no new config exists the fast mode only asks for status.
  assert.deepEqual(runs[7].benchmarks, []);
  assert.deepEqual(runs[8].benchmarks, []);
  const notes = allNotifications(runs);
  assert.deepEqual(minutesOf(notes), [6, 9]);
  assert.equal(notes[1].body, "节点 a1 的 SNI 可能被封，服务器已换用新 SNI，点此更新");
  assert.equal(notes[1].extra["open-url"], UPDATE_LINK);
  for (const note of notes) assert.ok(!note.body.includes(TOKEN));
  for (const line of runs.flatMap((run) => run.logs)) assert.ok(!line.includes(TOKEN));
});

test("test_still_failing_after_an_update_is_reminded_every_30_minutes", async () => {
  const net = world({ versions: (minute) => ({ a1: minute < 9 ? 1 : 2, b2: 1 }), results: a1Fails });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 75, ...net });
  assertDoneOnce(runs);
  assert.equal(net.reports.length, 1);
  const notes = allNotifications(runs);
  assert.deepEqual(minutesOf(notes), [6, 9, 39, 69]);
  for (const note of notes.slice(1)) {
    assert.equal(note.body, "节点 a1 的 SNI 可能被封，服务器已换用新 SNI，点此更新");
    assert.equal(note.extra["open-url"], UPDATE_LINK);
  }
});

test("test_a_pass_on_the_new_version_is_reported_and_ends_fast_mode", async () => {
  const net = world({
    versions: (minute) => ({ a1: minute < 9 ? 1 : 2, b2: 1 }),
    results: (alias, minute) => (alias === "a1" && minute < 12 ? HANDSHAKE : OK),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 30, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(
    net.reports.map((r) => [r.minute, r.body]),
    [
      [6, { alias: "a1", kind: "failed", version: 1 }],
      [12, { alias: "a1", kind: "ok", version: 2 }],
    ],
  );
  for (const run of runs.slice(13)) assert.deepEqual(run.requests, [], `minute ${run.minute} made a request`);
  assert.equal(JSON.parse(runs[29].prefs["cnsr.state"]).passed.a1, 2);
  assert.deepEqual(minutesOf(allNotifications(runs)), [6, 9]);
});

test("test_exhausted_backups_are_announced_once", async () => {
  const net = world({
    versions: { a1: 1, b2: 1 },
    results: a1Fails,
    flags: (alias, minute) => ({ exhausted: alias === "a1" && minute >= 8 }),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 130, ...net });
  assertDoneOnce(runs);
  // Exhausted backups end the fast mode, and the node is not reported again.
  assert.equal(net.reports.length, 1);
  assert.deepEqual(statusMinutes(runs).filter((m) => m > 8 && m < 60), []);
  const notes = allNotifications(runs);
  assert.deepEqual(minutesOf(notes), [6, 8]);
  assert.equal(notes[1].body, "节点 a1 的备选 SNI 已经用完，或者都没通过换之前的复验。请在电脑上运行 cnsr up a1 重新扫描");
});

test("test_an_expired_pause_is_announced_once", async () => {
  const net = world({
    versions: (minute) => ({ a1: minute < 9 ? 1 : 2, b2: 1 }),
    results: a1Fails,
    flags: (alias, minute) => ({
      paused: alias === "a1" && minute >= 9 && minute < 30,
      pause_expired: alias === "a1" && minute >= 30,
    }),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 130, ...net });
  assertDoneOnce(runs);
  assert.equal(net.reports.length, 1);
  const notes = allNotifications(runs);
  assert.deepEqual(minutesOf(notes), [6, 9, 30]);
  assert.equal(
    notes[2].body,
    "节点 a1 换 SNI 后 24 小时内没有设备测通，新 SNI 可能也被封了。" +
      "如果你已经更新过配置仍然连不上，请在策略页长按这个节点，选“cnsr 检测”",
  );
});

test("test_a_turned_down_report_is_handled_per_reason", async () => {
  const cases = {
    exhausted: {
      flags: (alias, minute) => ({ exhausted: alias === "a1" && minute >= 7 }),
      notes: [[6, "节点 a1 的备选 SNI 已经用完，或者都没通过换之前的复验。请在电脑上运行 cnsr up a1 重新扫描"]],
      statusAfter: [],
    },
    // Paused: the server already rotated and waits for a pass; keep the hourly check going.
    paused: { notes: [], statusAfter: [] },
    stale: {
      versions: (minute) => ({ a1: minute < 7 ? 1 : 2, b2: 1 }),
      notes: [[7, "节点 a1 有新配置，请长按风车更新外部资源。"]],
      statusAfter: [7, 8, 9, 10, 11],
    },
  };
  for (const [reason, expected] of Object.entries(cases)) {
    const net = world({
      versions: expected.versions ?? { a1: 1, b2: 1 },
      results: a1Fails,
      flags: expected.flags,
      answer: () => ({ accepted: false, reason }),
    });
    const runs = await runEveryMinute(SOURCE, { start: START, minutes: 12, ...net });
    assertDoneOnce(runs);
    assert.equal(net.reports.length, 1, reason);
    const notes = allNotifications(runs);
    assert.deepEqual(
      notes.map((note) => [Math.round((note.at - START) / MINUTE), note.body]),
      expected.notes,
      reason,
    );
    assert.deepEqual(statusMinutes(runs).filter((m) => m > 6), expected.statusAfter, reason);
    if (reason === "stale") assert.equal(notes[0].extra["open-url"], UPDATE_LINK);
  }
});

const newerFrom9 = (minute) => ({ a1: minute < 9 ? 1 : 2, b2: 1 });

test("test_a_lost_pass_report_is_retried", async () => {
  const net = world({
    versions: newerFrom9,
    results: (alias, minute) => (alias === "a1" && minute < 12 ? HANDSHAKE : OK),
    control: (request, minute) => {
      if (request.url !== CONFIG.report || minute !== 12) return null;
      throw new Error("network blip");
    },
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 20, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(
    net.reports.map((r) => [r.minute, r.body]),
    [
      [6, { alias: "a1", kind: "failed", version: 1 }],
      [13, { alias: "a1", kind: "ok", version: 2 }],
    ],
  );
  for (const run of runs.slice(14)) assert.deepEqual(run.requests, [], `minute ${run.minute} made a request`);
  assert.deepEqual(stateOf(runs[19]).waiting, {});
});

test("test_fast_mode_ends_a_day_after_the_update_and_does_not_come_back", async () => {
  // Another device passed, so the pause clears and never expires; this device keeps failing.
  const net = world({ versions: newerFrom9, results: a1Fails });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 1510, ...net });
  assertDoneOnce(runs);
  const updates = allNotifications(runs).filter((note) => note.body.includes("点此更新"));
  const reminders = range(0, 48).map((k) => 9 + 30 * k);
  assert.deepEqual(minutesOf(updates), reminders);
  // The update was first seen at minute 9: a day later fast mode ends for good.
  // The device never passed on version 2, so it is still taken to have version 1.
  assert.equal(stateOf(runs[9 + 1440]).passed.a1, 1);
  assert.deepEqual(stateOf(runs[9 + 1440]).waiting, {});
  assert.deepEqual(statusMinutes(runs).filter((m) => m > 9 + 1440 && m < 1500), []);
  // Later hourly failures on the old config stay quiet: no report on version 2, which would
  // rotate away an SNI that works, and no "outdated" fast mode again.
  assert.deepEqual(net.reports.map((r) => [r.minute, r.body.version]), [[6, 1]]);
  assert.deepEqual(minutesOf(allNotifications(runs)).filter((m) => m > 9 + 1440), []);
});

test("test_a_node_first_seen_before_a_rotation_is_not_reported_on_the_new_version", async () => {
  // No result for a1 at minute 0, so it never passed; another device has it rotated at 30.
  const net = world({
    versions: (minute) => ({ a1: minute < 30 ? 1 : 2, b2: 1 }),
    results: (alias, minute) => (alias !== "a1" ? OK : minute === 0 ? undefined : HANDSHAKE),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 70, ...net });
  assertDoneOnce(runs);
  assert.equal(stateOf(runs[0]).passed.a1, 1);
  assert.deepEqual(net.reports, []);
  const notes = allNotifications(runs);
  assert.deepEqual(minutesOf(notes), [66]);
  assert.equal(notes[0].body, "节点 a1 有新配置，请长按风车更新外部资源。");
});

test("test_a_pass_after_an_expired_pause_is_reported", async () => {
  const net = world({
    versions: { a1: 2, b2: 1 },
    results: () => OK,
    flags: (alias, minute) => ({ pause_expired: alias === "a1" && minute < 1 }),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 3, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(net.reports.map((r) => [r.minute, r.body]), [[0, { alias: "a1", kind: "ok", version: 2 }]]);
  // The device just measured it up: no "nobody passed" alert either.
  assert.deepEqual(allNotifications(runs), []);
});

test("test_an_hourly_pass_during_a_pause_ends_it", async () => {
  // A rotation leaves the old config failing, so a pass while paused is on the new one.
  const net = world({
    versions: { a1: 2, b2: 1 },
    results: () => OK,
    flags: (alias, minute) => ({ paused: alias === "a1" && minute < 1 }),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 3, ...net });
  assertDoneOnce(runs);
  assert.deepEqual(net.reports.map((r) => [r.minute, r.body]), [[0, { alias: "a1", kind: "ok", version: 2 }]]);
});

test("test_an_expired_pause_is_not_announced_for_a_version_this_device_measured_up", async () => {
  // A fast-mode run for b2 does not benchmark a1, which this device measured up on version 2.
  const saved = {
    next: START + 30 * MINUTE,
    passed: { a1: 2, b2: 1 },
    measured: { a1: 2 },
    waiting: { b2: { version: 1, reason: "reported", since: START } },
  };
  const net = world({
    versions: { a1: 2, b2: 2 },
    results: () => OK,
    flags: (alias) => ({ pause_expired: alias === "a1" }),
  });
  const run = await runScript(SOURCE, { now: START + MINUTE, prefs: { "cnsr.state": JSON.stringify(saved) }, ...net });
  assert.deepEqual(run.benchmarks.map((b) => b.tags), [["b2"]]);
  assert.deepEqual(run.notifications, []);
});

test("test_fast_mode_the_long_press_starts_during_a_run_survives_its_save", async () => {
  const net = world({ versions: { a1: 1, b2: 1 }, results: () => OK });
  const pressed = { version: 1, reason: "reported", since: START + 1 };
  const benchmark = (tags, now, prefs) => {
    // The long-press script reports a1 and saves its fast mode while this run benchmarks.
    const state = JSON.parse(prefs["cnsr.state"] || "{}");
    prefs["cnsr.state"] = JSON.stringify({ ...state, waiting: { a1: pressed }, passed: { a1: 1 } });
    return net.benchmark(tags, now);
  };
  const run = await runScript(SOURCE, { now: START, ...net, benchmark });
  assert.deepEqual(stateOf(run).waiting.a1, pressed);
});

test("test_the_next_hourly_time_is_saved_before_the_benchmark", async () => {
  // Quantumult X may stop a slow run; it must not leave every minute counting as hourly.
  let savedNext;
  const net = world({ versions: { a1: 1, b2: 1 }, results: () => OK });
  const benchmark = (tags, now, prefs) => {
    savedNext = JSON.parse(prefs["cnsr.state"] || "{}").next;
    return net.benchmark(tags, now);
  };
  await runScript(SOURCE, { now: START, ...net, benchmark });
  assert.equal(savedNext, START + 60 * MINUTE);
});

test("test_a_server_that_never_rotates_is_announced_once", async () => {
  const net = world({
    versions: { a1: 1, b2: 1 },
    results: a1Fails,
    flags: (alias, minute) => ({ pending: alias === "a1" && minute >= 6 }),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 200, ...net });
  assertDoneOnce(runs);
  const notes = allNotifications(runs);
  assert.deepEqual(minutesOf(notes), [6, 66]);
  assert.equal(notes[1].body, "节点 a1 的服务器还没有换上新 SNI，可能是节点上的换 SNI 服务没有运行。");
  // While the request stays pending the node is not reported again and fast mode stays off.
  assert.equal(net.reports.length, 1);
  assert.deepEqual(statusMinutes(runs).filter((m) => m > 66 && m < 120), []);
  assert.deepEqual(statusMinutes(runs).filter((m) => m > 126 && m < 180), []);
});

test("test_an_unreachable_node_in_fast_mode_gets_the_unreachable_notice", async () => {
  const net = world({
    versions: newerFrom9,
    results: (alias, minute) => (alias !== "a1" ? OK : minute < 9 ? HANDSHAKE : UNREACHABLE),
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 45, ...net });
  assertDoneOnce(runs);
  const notes = allNotifications(runs);
  assert.deepEqual(minutesOf(notes), [6, 9]);
  assert.equal(notes[1].body, "节点 a1 连不上，可能是 IP 被封，换 SNI 解决不了。");
  assert.equal(notes[1].extra, undefined);
});

test("test_malformed_waiting_state_is_dropped", async () => {
  const saved = {
    next: START + 30 * MINUTE,
    waiting: { a1: { reason: "reported" }, b2: "junk", c3: { version: "1", since: START } },
    alerts: { a1: "x", b2: { exhausted: "yes" } },
  };
  const net = world({ versions: { a1: 1, b2: 1, c3: 1 }, results: () => OK });
  const runs = await runEveryMinute(SOURCE, {
    start: START,
    minutes: 3,
    prefs: { "cnsr.state": JSON.stringify(saved) },
    ...net,
  });
  assertDoneOnce(runs);
  for (const run of runs) assert.deepEqual(run.requests, [], `minute ${run.minute} made a request`);
  assert.deepEqual(runs[2].logs, []);
});

test("test_an_empty_snippet_sends_the_reminder_without_a_link", async () => {
  const net = world({
    versions: newerFrom9,
    results: a1Fails,
    snippet: "",
  });
  const runs = await runEveryMinute(SOURCE, { start: START, minutes: 12, ...net });
  assertDoneOnce(runs);
  const notes = allNotifications(runs);
  assert.deepEqual(minutesOf(notes), [6, 9]);
  assert.equal(notes[1].body, "节点 a1 的 SNI 可能被封，服务器已换用新 SNI，点此更新");
  assert.equal(notes[1].extra, undefined);
});
