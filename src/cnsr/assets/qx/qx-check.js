// cnsr check for Quantumult X, woken every minute by a cron task.
//
// Once an hour it benchmarks every cnsr node. A handshake failure (TCP up, HTTP down)
// is re-measured after 3, 2 and 1 minutes; when all three confirm it and another cnsr
// node is up, the control plane is asked to rotate that node's SNI. Between due times
// the script only reads $prefs and exits without touching the network.
//
// After a report (or when the device is behind the server's config) the node waits in
// fast mode: status every minute; once a newer config exists the device is told to
// update, reminded every 30 minutes while the node still fails, and a pass on the new
// config is reported back, which ends the fast mode.
//
// The Worker fills CONFIG in with {"status": "<url>", "report": "<url>"}; the placeholder
// must appear only once in this file, because only the first occurrence is replaced.

const CONFIG = __CNSR_CONFIG__;

const HOURLY_MS = 3600000;
const CONFIRM_DELAYS_MS = [180000, 120000, 60000];
const DOMESTIC_URL = "https://www.baidu.com";
const PREFS_KEY = "cnsr.state";
// Cron wakes on the minute but Date.now() jitters; a run this close to a due time counts.
const DUE_SLACK_MS = 30000;
const REMIND_MS = 1800000;
// Fast mode gives up when no new config shows up for this long; the hourly check resumes.
const WAIT_LIMIT_MS = 3600000;
// Reminders to update stop this long after the newer config was first seen.
const UPDATE_WINDOW_MS = 86400000;
const USER_AGENT = "cnsr-qx/1";
const TITLE = "cnsr";

const MESSAGES = {
  reported: (alias) => `节点 ${alias} 的 SNI 可能被封，已通知服务器更换。`,
  unreachable: (alias) => `节点 ${alias} 连不上，可能是 IP 被封，换 SNI 解决不了。`,
  single: (alias) =>
    `节点 ${alias} 握手失败，可能是 SNI 被封。只有一个节点，无法排除你自己网络的问题，` +
    "可以长按节点运行“cnsr 检测”确认。",
  outdated: (alias) => `节点 ${alias} 有新配置，请长按风车更新外部资源。`,
  updated: (alias) => `节点 ${alias} 的 SNI 可能被封，服务器已换用新 SNI，点此更新`,
  exhausted: (alias) => `节点 ${alias} 的备选 SNI 已经用完，或者都没通过换之前的复验。请在电脑上运行 cnsr up ${alias} 重新扫描`,
  pause_expired: (alias) =>
    `节点 ${alias} 换 SNI 后 24 小时内没有设备测通，新 SNI 可能也被封了。` +
    "如果你已经更新过配置仍然连不上，请在策略页长按这个节点，选“cnsr 检测”",
  stalled: (alias) => `节点 ${alias} 的服务器还没有换上新 SNI，可能是节点上的换 SNI 服务没有运行。`,
};
const ALERTS = ["exhausted", "pause_expired"];

// The one-tap update: Quantumult X asks to refresh the existing cnsr resource.
function updateLink(snippet) {
  if (!snippet) return null;
  const line = `${snippet}, tag=cnsr, update-interval=86400, opt-parser=false, enabled=true`;
  const resource = encodeURIComponent(JSON.stringify({ server_remote: [line] }));
  return `quantumult-x:///add-resource?remote-resource=${resource}`;
}

// State in $prefs[PREFS_KEY], one JSON string:
//   next      ms timestamp of the next hourly benchmark
//   confirm   {alias: {step, at}}  pending confirmation: index into CONFIRM_DELAYS_MS, due time
//   passed    {alias: version}     the config version this device is believed to have: the
//                                  status version when the node first showed up (status is
//                                  never behind the device) or when it last measured it up
//   measured  {alias: version}     the last version this device itself measured up (unlike
//                                  `passed`, never taken from status)
//   muted     {alias: version}     a newer config this device was told about for a day and
//                                  never passed on: failures on it stay quiet
//   notified  {alias: key}         last notification per node, so one state is announced once
//   waiting   {alias: {version, reason, since, seen?, remind?, pass?}}  fast mode: waiting for
//                                  a config newer than `version` since `since`; reason
//                                  "reported" or "outdated" picks the message; `seen` is when
//                                  the newer config first showed up, `remind` when the update
//                                  notice is next due, `pass` a measured pass not yet reported
//   alerts    {alias: {exhausted?, pause_expired?, stalled?}}  states already announced;
//                                  `stalled`: the server never rotated while a request is pending
function asMap(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : {};
}

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

// Entries also come from the long-press script; anything malformed is dropped.
function validWaiting(value) {
  const waiting = {};
  for (const [alias, raw] of Object.entries(asMap(value))) {
    const entry = asMap(raw);
    if (!isNumber(entry.version) || !isNumber(entry.since)) continue;
    const reason = entry.reason === "outdated" ? "outdated" : "reported";
    const wait = { version: entry.version, reason, since: entry.since };
    for (const key of ["seen", "remind", "pass"]) if (isNumber(entry[key])) wait[key] = entry[key];
    waiting[alias] = wait;
  }
  return waiting;
}

function validAlerts(value) {
  const alerts = {};
  for (const [alias, raw] of Object.entries(asMap(value))) {
    const seen = {};
    for (const [flag, on] of Object.entries(asMap(raw))) if (on === true) seen[flag] = true;
    if (Object.keys(seen).length > 0) alerts[alias] = seen;
  }
  return alerts;
}

function loadState() {
  let saved = {};
  try {
    saved = asMap(JSON.parse($prefs.valueForKey(PREFS_KEY) || "{}"));
  } catch (error) {
    saved = {};
  }
  return {
    next: typeof saved.next === "number" ? saved.next : 0,
    confirm: asMap(saved.confirm),
    passed: asMap(saved.passed),
    measured: asMap(saved.measured),
    muted: asMap(saved.muted),
    notified: asMap(saved.notified),
    waiting: validWaiting(saved.waiting),
    alerts: validAlerts(saved.alerts),
  };
}

// Keeps fast mode the long-press script started after `since` (this run's start) meanwhile.
function saveState(state, since) {
  const latest = loadState();
  for (const [alias, wait] of Object.entries(latest.waiting)) {
    if (state.waiting[alias] || !(wait.since > since)) continue;
    state.waiting[alias] = wait;
    if (isNumber(latest.passed[alias])) state.passed[alias] = latest.passed[alias];
  }
  $prefs.setValueForKey(JSON.stringify(state), PREFS_KEY);
}

const isDue = (at, now) => at - DUE_SLACK_MS <= now;

// The control-plane URLs carry the client token: errors name the endpoint only, and
// describe() scrubs the token out of anything else that reaches the log.
const CLIENT_TOKEN = String(CONFIG.status).split("/")[3] || "";

function describe(error) {
  let text = String(error);
  if (error && typeof error.error === "string") text = error.error;
  else if (error && typeof error.message === "string") text = error.message;
  return CLIENT_TOKEN ? text.split(CLIENT_TOKEN).join("<token>") : text;
}

async function callControl(name, url, method, body) {
  const request = { url, method, headers: { "User-Agent": USER_AGENT } };
  if (body !== undefined) {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(body);
  }
  const response = await $task.fetch(request);
  if (response.statusCode !== 200) throw new Error(`${name} answered ${response.statusCode}`);
  return JSON.parse(response.body);
}

async function benchmark(tags) {
  if (tags.length === 0) return {};
  const reply = await $configuration.sendMessage({ action: "url_latency_benchmark", content: tags });
  return asMap(reply && reply.ret);
}

// "ok", "handshake" (TCP up, HTTP down), "unreachable" (TCP down) or "none" (no result).
function classify(pair) {
  if (!Array.isArray(pair) || typeof pair[0] !== "number" || typeof pair[1] !== "number") return "none";
  if (pair[0] === -1) return "unreachable";
  if (pair[1] === -1) return "handshake";
  return "ok";
}

// Everything a run needs, or null when the network or the control plane is not there.
// `pick(nodes)` names the nodes to benchmark this run.
async function measure(pick) {
  try {
    await $task.fetch({ url: DOMESTIC_URL, opts: { policy: "direct" } });
    const status = await callControl("status", CONFIG.status, "GET");
    const nodes = asMap(status && status.nodes);
    const tags = pick(nodes);
    const results = await benchmark(tags);
    const kinds = {};
    for (const alias of Object.keys(nodes)) kinds[alias] = classify(results[alias]);
    return { nodes, kinds, tags, snippet: String((status && status.snippet) || "") };
  } catch (error) {
    console.log(`cnsr: skipped this run: ${describe(error)}`);
    return null;
  }
}

// Forgets nodes no longer in status, and takes a node seen for the first time to be on the
// status version.
function trackNodes(state, nodes) {
  for (const map of [state.confirm, state.passed, state.measured, state.muted, state.notified, state.waiting, state.alerts]) {
    for (const alias of Object.keys(map)) if (!(alias in nodes)) delete map[alias];
  }
  for (const [alias, node] of Object.entries(nodes)) {
    if (!isNumber(state.passed[alias]) && isNumber(node.version)) state.passed[alias] = node.version;
  }
}

// Returns the control plane's {accepted, reason}, or null when it could not be asked.
async function report(alias, kind, version) {
  try {
    return asMap(await callControl("report", CONFIG.report, "POST", { alias, kind, version }));
  } catch (error) {
    console.log(`cnsr: ${kind} report for ${alias} failed: ${describe(error)}`);
    return null;
  }
}

function startWaiting(run, alias, version, reason) {
  run.state.waiting[alias] = { version, reason, since: run.now };
}

// A handshake failure survived every confirmation: decide what it means.
async function conclude(run, alias, say) {
  const { state, nodes, kinds } = run;
  const node = nodes[alias];
  const version = node.version;
  const known = state.passed[alias];
  if (version > known) {
    // After an expired pause the device cannot tell an old config from a blocked new one;
    // that announcement already points to the long-press check. A muted version was asked
    // for a day already.
    if (!node.pause_expired && state.muted[alias] !== version) startWaiting(run, alias, known, "outdated");
    return;
  }
  // The server never rotated on the last report; asking again would wait again.
  if (node.exhausted || (node.pending && asMap(state.alerts[alias]).stalled)) return;
  const aliases = Object.keys(nodes);
  if (aliases.length === 1) return say(alias, "single", MESSAGES.single(alias));
  if (!aliases.some((other) => other !== alias && kinds[other] === "ok")) return;
  const answer = await report(alias, "failed", version);
  if (answer === null) return;
  if (answer.accepted) {
    say(alias, `reported:${version}`, MESSAGES.reported(alias));
    startWaiting(run, alias, version, "reported");
  } else if (answer.reason === "exhausted") {
    announce(run, alias, "exhausted");
  } else if (answer.reason === "stale") {
    startWaiting(run, alias, version, "outdated");
  }
  // "paused": the server already rotated and waits for a pass; the hourly check goes on.
}

function announce(run, alias, flag) {
  const seen = asMap(run.state.alerts[alias]);
  if (seen[flag]) return;
  run.state.alerts[alias] = { ...seen, [flag]: true };
  run.notes.lines.push(MESSAGES[flag](alias));
}

// Announces exhausted backups and expired pauses once; forgets them once they clear.
function alertAll(run) {
  for (const alias of Object.keys(run.nodes)) {
    const node = run.nodes[alias];
    const seen = run.state.alerts[alias];
    for (const flag of ALERTS) {
      // A node this device measured up on this version is not one nobody passed on.
      const passedHere = run.kinds[alias] === "ok" || run.state.measured[alias] === node.version;
      if (node[flag] === true && !(flag === "pause_expired" && passedHere)) announce(run, alias, flag);
      else if (seen) delete seen[flag];
    }
    if (seen && node.pending !== true) delete seen.stalled;
    if (Object.keys(asMap(run.state.alerts[alias])).length === 0) delete run.state.alerts[alias];
  }
}

// Reports a measured pass; it stays pending in `wait.pass` until the control plane answers.
async function reportPass(state, alias, wait, version) {
  wait.pass = version;
  if ((await report(alias, "ok", version)) === null) return;
  delete state.waiting[alias];
  state.passed[alias] = version;
  state.measured[alias] = version;
  delete state.notified[alias];
}

// Fast mode for one node: wait for a newer config, ask for the update, report the pass.
async function follow(run, alias, say) {
  const { state, now, nodes, kinds, notes } = run;
  const wait = state.waiting[alias];
  const node = nodes[alias];
  if (isNumber(wait.pass)) return reportPass(state, alias, wait, wait.pass);
  if (node.exhausted || node.pause_expired) return void delete state.waiting[alias];
  if (!(node.version > wait.version)) {
    if (now - wait.since < WAIT_LIMIT_MS) return;
    delete state.waiting[alias];
    if (wait.reason === "reported" && node.pending === true) announce(run, alias, "stalled");
    return;
  }
  if (!isNumber(wait.seen)) wait.seen = now;
  // A day after the update showed up, stop asking. The device never passed on it, so `passed`
  // stays and the version is muted: its failures there are not taken for a blocked SNI.
  if (now - wait.seen >= UPDATE_WINDOW_MS) {
    delete state.waiting[alias];
    state.muted[alias] = node.version;
    return;
  }
  const kind = kinds[alias];
  if (kind === "ok") return reportPass(state, alias, wait, node.version);
  if (kind === "unreachable" && !run.yourNetwork) return say(alias, "unreachable", MESSAGES.unreachable(alias));
  const failing = kind === "handshake" && !run.yourNetwork;
  if (isNumber(wait.remind) && !(failing && isDue(wait.remind, now))) return;
  wait.remind = now + REMIND_MS;
  notes.lines.push(wait.reason === "reported" ? MESSAGES.updated(alias) : MESSAGES.outdated(alias));
  notes.link = updateLink(run.snippet) || notes.link;
}

async function settle(run, alias, say) {
  const { state, now, nodes, kinds, yourNetwork } = run;
  if (state.waiting[alias]) return;
  const pending = state.confirm[alias];
  if (pending && !isDue(pending.at, now)) return;
  const kind = kinds[alias];
  if (kind !== "handshake" || yourNetwork) delete state.confirm[alias];
  if (kind === "ok") {
    const node = nodes[alias];
    state.passed[alias] = state.measured[alias] = node.version;
    delete state.notified[alias];
    delete state.muted[alias];
    // A rotation leaves the old config failing, so a pass in a pause is on the new one: it ends
    // the pause, or after the pause expired, the pause_expired alert.
    if (node.paused === true || node.pause_expired === true) await report(alias, "ok", node.version);
    return;
  }
  if (kind === "none" || yourNetwork) return;
  if (kind === "unreachable") return say(alias, "unreachable", MESSAGES.unreachable(alias));
  const step = pending ? pending.step + 1 : 0;
  if (step < CONFIRM_DELAYS_MS.length) {
    state.confirm[alias] = { step, at: now + CONFIRM_DELAYS_MS[step] };
    return;
  }
  delete state.confirm[alias];
  await conclude(run, alias, say);
}

// Hourly and confirmation runs benchmark every node; a fast-mode run only the waiting
// nodes that already have a newer config.
function picker(state, full) {
  return (nodes) =>
    Object.keys(nodes).filter((alias) => {
      if (full) return true;
      const wait = state.waiting[alias];
      return Boolean(wait) && nodes[alias].version > wait.version;
    });
}

// Runs a due check and returns the notification: {lines, link}.
async function check(state, now, hourly, due) {
  // Saved at once: Quantumult X may stop a slow run, and every minute would count as hourly.
  if (hourly) {
    state.next = now + HOURLY_MS;
    saveState(state, now);
  }
  const notes = { lines: [], link: null };

  const measured = await measure(picker(state, hourly || due.length > 0));
  if (measured === null) {
    // Without a network a confirmation cannot be finished; start over next time.
    for (const alias of due) delete state.confirm[alias];
    return notes;
  }
  const { nodes, kinds, tags, snippet } = measured;
  trackNodes(state, nodes);
  const answered = tags.filter((alias) => kinds[alias] !== "none");
  // With several nodes all down at once, the problem is the device's own network.
  const yourNetwork = tags.length > 1 && answered.length > 0 && answered.every((a) => kinds[a] !== "ok");

  const say = (alias, key, message) => {
    if (state.notified[alias] === key) return;
    state.notified[alias] = key;
    notes.lines.push(message);
  };
  const run = { state, now, nodes, kinds, yourNetwork, snippet, notes };
  alertAll(run);
  for (const alias of Object.keys(nodes)) await settle(run, alias, say);
  for (const alias of Object.keys(state.waiting)) await follow(run, alias, say);
  return notes;
}

async function main() {
  const state = loadState();
  const now = Date.now();
  const hourly = isDue(state.next, now);
  const due = Object.keys(state.confirm).filter((alias) => isDue(state.confirm[alias].at, now));
  if (!hourly && due.length === 0 && Object.keys(state.waiting).length === 0) return;
  let notes = { lines: [], link: null };
  try {
    notes = await check(state, now, hourly, due);
  } finally {
    saveState(state, now);
  }
  // Quantumult X drops a second notification in the same run, so everything goes in one.
  if (notes.lines.length === 0) return;
  if (notes.link) $notify(TITLE, "", notes.lines.join("\n"), { "open-url": notes.link });
  else $notify(TITLE, "", notes.lines.join("\n"));
}

main()
  .catch((error) => console.log(`cnsr: check failed: ${describe(error)}`))
  .finally(() => $done());
