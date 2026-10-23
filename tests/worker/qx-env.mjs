// A fake Quantumult X environment for running the cnsr scripts under Node.
//
// runScript(source, {now, prefs, fetch, benchmark, environment})
//   -> Promise<{prefs, notifications, requests, benchmarks, logs, done, doneCount, now}>
//
// - now          start of the fake clock in ms; Date.now() and new Date() read it,
//                and setTimeout(fn, ms) advances it by ms before calling fn.
// - prefs        {key: string} the script sees through $prefs; a copy comes back.
// - fetch        (request, now, prefs) -> {statusCode, body} or a promise of it; throwing or
//                rejecting stands for a network failure, which reaches the script the way
//                Quantumult X rejects: an object {error: "<message>"}. Every request is recorded.
//
// Everything the script prints with console.log/warn/error comes back as `logs`.
// - benchmark    (tags, now, prefs) -> {"<tag>": [tcp, http]}; leave a tag out for "no result".
// `prefs` is the live $prefs store, for a test that stands for another script writing to it
// while this one runs.
// - environment  the value of $environment (the long-press script reads params from it).

import { readFileSync } from "node:fs";
import vm from "node:vm";

const SETTLE_TICKS = 5;
const DONE_TIMEOUT_MS = 2000;

const tick = () => new Promise((resolve) => setImmediate(resolve));

export function loadScript(url, config) {
  const source = readFileSync(url, "utf8");
  return source.replace("__CNSR_CONFIG__", () => JSON.stringify(config));
}

function fakeDate(clock) {
  return class FakeDate extends Date {
    constructor(...args) {
      if (args.length === 0) super(clock.now);
      else super(...args);
    }

    static now() {
      return clock.now;
    }
  };
}

export async function runScript(source, options = {}) {
  const clock = { now: options.now ?? 0 };
  const prefs = { ...(options.prefs ?? {}) };
  const fetch = options.fetch ?? (() => Promise.reject(new Error("no network")));
  const logs = [];
  const capture = (...args) => logs.push(args.map(String).join(" "));
  const benchmark = options.benchmark ?? (() => ({}));
  const notifications = [];
  const requests = [];
  const benchmarks = [];
  const doneCalls = [];
  let finish;
  const finished = new Promise((resolve) => {
    finish = resolve;
  });

  const context = {
    console: { log: capture, info: capture, warn: capture, error: capture },
    Date: fakeDate(clock),
    setTimeout(fn, ms = 0) {
      setImmediate(() => {
        clock.now += Math.max(0, ms);
        fn();
      });
      return 0;
    },
    clearTimeout() {},
    $environment: options.environment ?? {},
    $prefs: {
      valueForKey: (key) => (key in prefs ? prefs[key] : undefined),
      setValueForKey(value, key) {
        prefs[key] = String(value);
        return true;
      },
      removeValueForKey(key) {
        delete prefs[key];
        return true;
      },
    },
    $notify(title, subtitle, body, extra) {
      notifications.push({ at: clock.now, title, subtitle, body, extra: structuredClone(extra) });
    },
    $task: {
      async fetch(request) {
        // Cloned into this realm so tests can deepEqual what the script sent.
        const copy = structuredClone(request);
        requests.push({ at: clock.now, ...copy });
        try {
          return await fetch(copy, clock.now, prefs);
        } catch (failure) {
          throw { error: failure instanceof Error ? failure.message : String(failure) };
        }
      },
    },
    $configuration: {
      async sendMessage(message) {
        if (message?.action !== "url_latency_benchmark") throw new Error("unsupported action");
        const tags = structuredClone(message.content);
        benchmarks.push({ at: clock.now, tags });
        return { ret: await benchmark(tags, clock.now, prefs) };
      },
    },
    $done(value) {
      doneCalls.push(structuredClone(value));
      finish();
    },
  };

  vm.runInNewContext(source, context, { filename: "qx-script.js" });
  let timer;
  const hung = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("the script never called $done()")), DONE_TIMEOUT_MS);
  });
  await Promise.race([finished, hung]).finally(() => clearTimeout(timer));
  for (let i = 0; i < SETTLE_TICKS; i += 1) await tick();

  return {
    prefs,
    notifications,
    requests,
    benchmarks,
    logs,
    done: doneCalls[0],
    doneCount: doneCalls.length,
    now: clock.now,
  };
}

// Runs the script once a minute, like the cron task, carrying $prefs between runs.
export async function runEveryMinute(source, { start, minutes, prefs = {}, ...options }) {
  const runs = [];
  let carried = prefs;
  for (let minute = 0; minute < minutes; minute += 1) {
    const result = await runScript(source, { ...options, now: start + minute * 60000, prefs: carried });
    runs.push({ minute, ...result });
    carried = result.prefs;
  }
  return runs;
}
