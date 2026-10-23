// Shared fixtures for the qx-check tests: a fake network around the check script.

import assert from "node:assert/strict";
import { loadScript } from "./qx-env.mjs";

export const TOKEN = "tok-secret-7f3a";
export const CONFIG = {
  status: `https://c.example.com/${TOKEN}/status`,
  report: `https://c.example.com/${TOKEN}/report`,
};
export const SOURCE = loadScript(new URL("../../src/cnsr/assets/qx/qx-check.js", import.meta.url), CONFIG);
export const START = 1_700_000_000_000;
export const MINUTE = 60000;
export const OK = [80, 200];
export const HANDSHAKE = [80, -1];
export const UNREACHABLE = [-1, -1];
export const SNIPPET = `https://c.example.com/${TOKEN}/cnsr.snippet`;
export const UPDATE_LINK =
  "quantumult-x:///add-resource?remote-resource=" +
  encodeURIComponent(
    JSON.stringify({
      server_remote: [`${SNIPPET}, tag=cnsr, update-interval=86400, opt-parser=false, enabled=true`],
    }),
  );

// A fake network: the domestic site, the control plane and the benchmark.
// `results(alias, minute)` gives the benchmark pair, or undefined for "no result";
// `flags(alias, minute)` overrides paused / pause_expired / pending / exhausted;
// `answer(body, minute)` is the control plane's reply to a report;
// `control(request, minute)` may answer any request first (null lets it through).
export function world({
  versions,
  results,
  domestic = true,
  control = () => null,
  flags = () => ({}),
  answer = () => ({ accepted: true, reason: null }),
  snippet = SNIPPET,
}) {
  const reports = [];
  const minuteOf = (now) => Math.round((now - START) / MINUTE);
  const versionsAt = (now) => (typeof versions === "function" ? versions(minuteOf(now)) : versions);
  const fetch = (request, now) => {
    if (request.url === "https://www.baidu.com") {
      if (!domestic) throw new Error("offline");
      return { statusCode: 200, body: "" };
    }
    const override = control(request, minuteOf(now));
    if (override !== null) return override;
    if (request.url === CONFIG.status) {
      const nodes = {};
      for (const [alias, version] of Object.entries(versionsAt(now))) {
        const base = { version, paused: false, pause_expired: false, pending: false, exhausted: false };
        nodes[alias] = { ...base, ...flags(alias, minuteOf(now)) };
      }
      return { statusCode: 200, body: JSON.stringify({ snippet, nodes }) };
    }
    if (request.url === CONFIG.report && request.method === "POST") {
      const body = JSON.parse(request.body);
      reports.push({ minute: minuteOf(now), body, request });
      return { statusCode: 200, body: JSON.stringify(answer(body, minuteOf(now))) };
    }
    return { statusCode: 404, body: "" };
  };
  const benchmark = (tags, now) => {
    const ret = {};
    for (const tag of tags) {
      const pair = results(tag, minuteOf(now));
      if (pair !== undefined) ret[tag] = pair;
    }
    return ret;
  };
  return { fetch, benchmark, reports };
}

export const benchmarkMinutes = (runs) => runs.filter((run) => run.benchmarks.length > 0).map((run) => run.minute);
export const allNotifications = (runs) => runs.flatMap((run) => run.notifications);
export const minutesOf = (notes) => notes.map((note) => Math.round((note.at - START) / MINUTE));
export const statusMinutes = (runs) =>
  runs.filter((run) => run.requests.some((r) => r.url === CONFIG.status)).map((run) => run.minute);
export const range = (from, to) => Array.from({ length: to - from }, (_, i) => from + i);
// a1 fails its handshake throughout while b2 stays up; the report goes out at minute 6.
export const a1Fails = (alias) => (alias === "a1" ? HANDSHAKE : OK);

export function assertDoneOnce(runs) {
  for (const run of runs) assert.equal(run.doneCount, 1, `minute ${run.minute} must call $done() once`);
}

export const stateOf = (run) => JSON.parse(run.prefs["cnsr.state"]);
