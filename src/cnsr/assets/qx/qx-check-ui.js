// cnsr long-press check for Quantumult X (event-interaction).
//
// Long-pressing a node in the policy page and choosing "cnsr 检测" tests it three times,
// UI_GAP_MS apart. Three handshake failures while the domestic site is reachable are
// reported at once and the node enters the scheduled script's fast mode. When the server
// has a newer config than this device last passed on, the script asks for an update
// first; pressing again within CONFIRM_WINDOW_MS counts as having updated.
//
// The Worker fills CONFIG in with {"status": "<url>", "report": "<url>"}; the placeholder
// must appear only once in this file, because only the first occurrence is replaced.

const CONFIG = __CNSR_CONFIG__;

// Three tests and two gaps must fit in Quantumult X's time limit for a script.
const UI_GAP_MS = 5000;
const TESTS = 3;
const CONFIRM_WINDOW_MS = 600000;
const DOMESTIC_URL = "https://www.baidu.com";
const USER_AGENT = "cnsr-qx/1";
// The scheduled script's state; this script only touches `waiting` and `passed`.
const PREFS_KEY = "cnsr.state";
// Own key: the scheduled script rewrites cnsr.state and would drop unknown fields.
const PROMPT_KEY = "cnsr.ui";
const TITLE = "cnsr 检测";

const MESSAGES = {
  notCnsr: "这不是 cnsr 管理的节点。",
  offline: "你当前的网络连不上国内网站，无法判断节点是否被封，请换个网络再试。",
  noControl: "连不上 cnsr 控制端，请稍后再试。",
  failed: (alias) => `检测出错了，节点 ${alias} 没有得出结论，请稍后再试。`,
  ok: (alias) => `节点 ${alias} 正常。`,
  unreachable: (alias) => `节点 ${alias} 连不上，可能是 IP 被封，换 SNI 解决不了。`,
  unstable: (alias) => `节点 ${alias} 三次检测结果不一致，连接不稳定，暂不处理。过一会儿再检测一次。`,
  none: (alias) => `没有拿到节点 ${alias} 的测速结果，请确认它在当前策略组里后再试。`,
  update: (alias) => `节点 ${alias}：服务器已有新配置，请长按风车更新外部资源后再检测。`,
  reported: (alias) => `节点 ${alias} 的 SNI 很可能被封，已通知服务器更换。换好后会通知你更新。`,
  exhausted: (alias) => `节点 ${alias} 的备选 SNI 已经用完，或者都没通过换之前的复验。请在电脑上运行 cnsr up ${alias} 重新扫描。`,
  paused: (alias) => `节点 ${alias} 的 SNI 刚被服务器换过，正在等新配置测通。请先长按风车更新外部资源，再长按检测一次。`,
  reportLost: (alias) => `节点 ${alias} 握手失败，但没能通知服务器，请稍后再检测一次。`,
};

const asMap = (value) => (value !== null && typeof value === "object" && !Array.isArray(value) ? value : {});
const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The control-plane URLs carry the client token: errors name the endpoint only, and
// describe() scrubs the token out of anything else that reaches the log.
const CLIENT_TOKEN = String(CONFIG.status).split("/")[3] || "";

function describe(error) {
  let text = String(error);
  if (error && typeof error.error === "string") text = error.error;
  else if (error && typeof error.message === "string") text = error.message;
  return CLIENT_TOKEN ? text.split(CLIENT_TOKEN).join("<token>") : text;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

// The selected node's tag: a string, a JSON string, or an object with node / tag.
function selectedTag() {
  let params = asMap($environment).params;
  if (typeof params === "string" && params.trim().startsWith("{")) {
    try {
      params = JSON.parse(params);
    } catch (error) {
      // Not JSON after all: treated as a plain tag.
    }
  }
  if (typeof params === "string") return params.trim();
  const fields = asMap(params);
  for (const key of ["node", "tag", "name", "nodeName"]) {
    if (typeof fields[key] === "string" && fields[key].trim()) return fields[key].trim();
  }
  return "";
}

function loadJson(key) {
  try {
    return asMap(JSON.parse($prefs.valueForKey(key) || "{}"));
  } catch (error) {
    return {};
  }
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

async function benchmark(tag) {
  const reply = await $configuration.sendMessage({ action: "url_latency_benchmark", content: [tag] });
  return asMap(reply && reply.ret)[tag];
}

// "ok", "handshake" (TCP up, HTTP down), "unreachable" (TCP down) or "none" (no result).
function classify(pair) {
  if (!Array.isArray(pair) || typeof pair[0] !== "number" || typeof pair[1] !== "number") return "none";
  if (pair[0] === -1) return "unreachable";
  if (pair[1] === -1) return "handshake";
  return "ok";
}

async function testThrice(tag) {
  const kinds = [];
  for (let i = 0; i < TESTS; i += 1) {
    if (i > 0) await sleep(UI_GAP_MS);
    kinds.push(classify(await benchmark(tag)));
  }
  return kinds;
}

// Decides whether the device may be taken to have the server's latest config. Returns
// "update" when it has to update first, "confirmed" when it is known to have it (you pressed
// again within CONFIRM_WINDOW_MS of being asked to update, or it measured this version up
// before while the server still waits for a pass), otherwise "go".
function configGate(alias, node, state, now) {
  const known = state.passed[alias];
  const prompts = loadJson(PROMPT_KEY);
  // Only a version this device measured up counts: `passed` may come from first sight in status.
  const measured = asMap(state.measured)[alias] === node.version;
  if (!(node.version > known)) return node.paused === true && measured ? "confirmed" : "go";
  if (isNumber(prompts[alias]) && now - prompts[alias] <= CONFIRM_WINDOW_MS) {
    delete prompts[alias];
    $prefs.setValueForKey(JSON.stringify(prompts), PROMPT_KEY);
    return "confirmed";
  }
  prompts[alias] = now;
  $prefs.setValueForKey(JSON.stringify(prompts), PROMPT_KEY);
  return "update";
}

function clearPrompt(alias) {
  const prompts = loadJson(PROMPT_KEY);
  if (!(alias in prompts)) return;
  delete prompts[alias];
  $prefs.setValueForKey(JSON.stringify(prompts), PROMPT_KEY);
}

// Puts the node into the scheduled script's fast mode, keeping the rest of its state.
function enterFastMode(state, alias, version, reason) {
  state.waiting = asMap(state.waiting);
  state.passed = asMap(state.passed);
  state.waiting[alias] = { version, reason, since: Date.now() };
  state.passed[alias] = version;
  $prefs.setValueForKey(JSON.stringify(state), PREFS_KEY);
}

async function reportFailure(alias, version, state, confirmed) {
  const body = { alias, kind: "failed", version, ...(confirmed ? { confirmed: true } : {}) };
  let answer;
  try {
    answer = asMap(await callControl("report", CONFIG.report, "POST", body));
  } catch (error) {
    console.log(`cnsr: report for ${alias} failed: ${describe(error)}`);
    return MESSAGES.reportLost(alias);
  }
  if (answer.accepted) {
    enterFastMode(state, alias, version, "reported");
    return MESSAGES.reported(alias);
  }
  if (answer.reason === "exhausted") return MESSAGES.exhausted(alias);
  if (answer.reason === "stale") {
    enterFastMode(state, alias, version, "outdated");
    return MESSAGES.update(alias);
  }
  return MESSAGES.paused(alias);
}

// Best effort: the page still says the node is fine when the control plane is not reached.
async function reportPass(alias, version) {
  try {
    await callControl("report", CONFIG.report, "POST", { alias, kind: "ok", version });
  } catch (error) {
    console.log(`cnsr: ok report for ${alias} failed: ${describe(error)}`);
  }
}

// The conclusion to show, as one line of text.
async function inspect() {
  const tag = selectedTag();
  if (!tag) return MESSAGES.notCnsr;
  try {
    await $task.fetch({ url: DOMESTIC_URL, opts: { policy: "direct" } });
  } catch (error) {
    return MESSAGES.offline;
  }
  let status;
  try {
    status = asMap(await callControl("status", CONFIG.status, "GET"));
  } catch (error) {
    console.log(`cnsr: status failed: ${describe(error)}`);
    return MESSAGES.noControl;
  }
  const node = asMap(asMap(status.nodes)[tag]);
  if (!isNumber(node.version)) return MESSAGES.notCnsr;

  const kinds = await testThrice(tag);
  const state = loadJson(PREFS_KEY);
  // First sight of the node: status is never behind the device, so it has this version at most.
  state.passed = asMap(state.passed);
  if (!isNumber(state.passed[tag])) {
    state.passed[tag] = node.version;
    $prefs.setValueForKey(JSON.stringify(state), PREFS_KEY);
  }
  if (kinds.every((kind) => kind === "ok")) {
    // A pass on a config the server has not heard a pass for ends its wait after a rotation.
    const news = node.version > state.passed[tag] || node.paused === true;
    state.passed[tag] = node.version;
    state.measured = { ...asMap(state.measured), [tag]: node.version };
    $prefs.setValueForKey(JSON.stringify(state), PREFS_KEY);
    clearPrompt(tag);
    if (news) await reportPass(tag, node.version);
    return MESSAGES.ok(tag);
  }
  if (kinds.every((kind) => kind === "unreachable")) return MESSAGES.unreachable(tag);
  if (kinds.every((kind) => kind === "none")) return MESSAGES.none(tag);
  if (!kinds.every((kind) => kind === "handshake")) return MESSAGES.unstable(tag);
  const gate = configGate(tag, node, state, Date.now());
  if (gate === "update") return MESSAGES.update(tag);
  return reportFailure(tag, node.version, state, gate === "confirmed");
}

async function main() {
  let message;
  try {
    message = await inspect();
  } catch (error) {
    console.log(`cnsr: long-press check failed: ${describe(error)}`);
    message = MESSAGES.failed(selectedTag() || "?");
  }
  $done({ title: TITLE, htmlMessage: `<p style="font-size:16px">${escapeHtml(message)}</p>` });
}

main();
