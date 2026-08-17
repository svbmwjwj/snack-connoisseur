// Unit tests for gateway/worker.js using JavaScriptCore CLI (jsc)

// Minimal Web API mocks for Cloudflare Worker environment
class URL {
  constructor(url) {
    const match = url.match(/^(https?:\/\/[^\/]+)?(\/[^?#]*)?(\?.*)?$/);
    this.pathname = (match && match[2]) || '/';
    this.search = (match && match[3]) || '';
  }
}

class Headers {
  constructor(init) {
    this._map = {};
    if (init) {
      for (const k of Object.keys(init)) {
        this._map[k.toLowerCase()] = init[k];
      }
    }
  }
  get(name) {
    return this._map[name.toLowerCase()] || null;
  }
  set(name, value) {
    this._map[name.toLowerCase()] = value;
  }
}

class Request {
  constructor(url, options = {}) {
    this.url = url;
    this.method = options.method || 'GET';
    this.headers = new Headers(options.headers);
    this._body = options.body || '';
  }
  async json() {
    return JSON.parse(this._body);
  }
  async text() {
    return this._body;
  }
}

class Response {
  constructor(body, options = {}) {
    this.body = body;
    this.status = options.status || 200;
    this.ok = this.status >= 200 && this.status < 300;
    this.statusText = options.statusText || 'OK';
    this.headers = new Headers(options.headers);
  }
  async text() {
    return typeof this.body === 'string' ? this.body : JSON.stringify(this.body);
  }
  async json() {
    return JSON.parse(this.body);
  }
}

let capturedFetches = [];
let mockFetchResponse = { status: 200, body: JSON.stringify({ ok: true }) };

async function fetch(url, options = {}) {
  capturedFetches.push({ url, options });
  return new Response(mockFetchResponse.body, {
    status: mockFetchResponse.status,
    headers: mockFetchResponse.headers || { 'content-type': 'application/json' }
  });
}

// Global polyfills
globalThis.URL = URL;
globalThis.Headers = Headers;
globalThis.Request = Request;
globalThis.Response = Response;
globalThis.fetch = fetch;

import worker from './worker.js';

let passed = 0;
let failed = 0;

function assert(condition, message) {
  if (condition) {
    print(`  ✓ ${message}`);
    passed++;
  } else {
    print(`  ✗ FAIL: ${message}`);
    failed++;
  }
}

async function runAllTests() {
  print("Running Cloudflare Worker Gateway Test Suite...\n");

  const env = {
    GATEWAY_AUTH_KEY: "my-test-auth-key",
    TG_BOT_TOKEN: "123456:TEST_BOT_TOKEN",
    TG_CHAT_ID: "-100123456789",
    GH_TOKEN: "ghp_MockGitHubToken123456789"
  };

  // Test 1: Root status check with valid auth
  {
    capturedFetches = [];
    const req = new Request("https://gateway.example.com/", {
      headers: { "Authorization": "Bearer my-test-auth-key" }
    });
    const res = await worker.fetch(req, env);
    const body = await res.json();
    assert(res.status === 200, "Root endpoint returns HTTP 200");
    assert(body.status === "Snack Gateway Running", "Root endpoint returns expected status message");
  }

  // Test 2: Auth failure when key is wrong
  {
    capturedFetches = [];
    const req = new Request("https://gateway.example.com/", {
      headers: { "Authorization": "Bearer wrong-key" }
    });
    const res = await worker.fetch(req, env);
    const body = await res.json();
    assert(res.status === 401, "Wrong auth token returns HTTP 401");
    assert(body.error === "Unauthorized", "Unauthorized error message returned");
  }

  // Test 3: Auth failure when header is missing
  {
    capturedFetches = [];
    const req = new Request("https://gateway.example.com/");
    const res = await worker.fetch(req, env);
    assert(res.status === 401, "Missing auth token returns HTTP 401");
  }

  // Test 4: When GATEWAY_AUTH_KEY is not set, auth check is skipped
  {
    capturedFetches = [];
    const noAuthEnv = { ...env, GATEWAY_AUTH_KEY: undefined };
    const req = new Request("https://gateway.example.com/");
    const res = await worker.fetch(req, noAuthEnv);
    assert(res.status === 200, "When GATEWAY_AUTH_KEY is unset, requests pass through");
  }

  // Test 5: Telegram notification proxy (/api/tg)
  {
    capturedFetches = [];
    mockFetchResponse = { status: 200, body: JSON.stringify({ ok: true, result: { message_id: 42 } }) };
    const req = new Request("https://gateway.example.com/api/tg", {
      method: "POST",
      headers: {
        "Authorization": "Bearer my-test-auth-key",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        text: "Test Telegram Alert",
        parse_mode: "HTML"
      })
    });
    const res = await worker.fetch(req, env);
    const body = await res.json();

    assert(res.status === 200, "TG endpoint returns HTTP 200");
    assert(body.ok === true, "TG proxy passes through Telegram API response");
    assert(capturedFetches.length === 1, "fetch called exactly once for TG proxy");
    assert(
      capturedFetches[0].url === "https://api.telegram.org/bot123456:TEST_BOT_TOKEN/sendMessage",
      "TG target URL matches bot token format"
    );
    const forwardedBody = JSON.parse(capturedFetches[0].options.body);
    assert(forwardedBody.chat_id === "-100123456789", "TG forwarded chat_id matches env");
    assert(forwardedBody.text === "Test Telegram Alert", "TG forwarded text matches request body");
    assert(forwardedBody.parse_mode === "HTML", "TG forwarded custom parse_mode matches request body");
  }

  // Test 6: Telegram default parse_mode fallback
  {
    capturedFetches = [];
    const req = new Request("https://gateway.example.com/api/tg", {
      method: "POST",
      headers: { "Authorization": "Bearer my-test-auth-key" },
      body: JSON.stringify({ text: "Markdown default text" })
    });
    await worker.fetch(req, env);
    const forwardedBody = JSON.parse(capturedFetches[0].options.body);
    assert(forwardedBody.parse_mode === "Markdown", "TG proxy defaults parse_mode to Markdown");
  }

  // Test 7: GitHub Actions Dispatch proxy (/api/gh-dispatch)
  {
    capturedFetches = [];
    mockFetchResponse = { status: 204, body: "" };
    const payload = { server_alias: "sg_aws", ip: "1.2.3.4", cidr_v4: "1.2.3.0/24" };
    const req = new Request("https://gateway.example.com/api/gh-dispatch", {
      method: "POST",
      headers: { "Authorization": "Bearer my-test-auth-key" },
      body: JSON.stringify(payload)
    });
    const res = await worker.fetch(req, env);

    assert(res.status === 204, "GH dispatch proxy returns upstream status (204)");
    assert(capturedFetches.length === 1, "fetch called exactly once for GH dispatch");
    assert(
      capturedFetches[0].url === "https://api.github.com/repos/svbmwjwj/snack-connoisseur/dispatches",
      "GH dispatch URL targets correct repo"
    );
    const ghOpts = capturedFetches[0].options;
    assert(ghOpts.headers["Accept"] === "application/vnd.github.v3+json", "GH dispatch Accept header is correct");
    assert(ghOpts.headers["Authorization"] === "Bearer ghp_MockGitHubToken123456789", "GH dispatch uses env.GH_TOKEN");
    assert(ghOpts.headers["User-Agent"] === "Snack-Gateway", "GH dispatch sets Snack-Gateway User-Agent");
    const forwardedGhBody = JSON.parse(ghOpts.body);
    assert(forwardedGhBody.event_type === "scan_trigger", "GH dispatch event_type is scan_trigger");
    assert(forwardedGhBody.client_payload.server_alias === "sg_aws", "GH dispatch client_payload preserved");
  }

  // Test 8: Script template raw proxy (/api/raw/:filename)
  {
    capturedFetches = [];
    const templateContent = "#!/bin/bash\necho hello";
    mockFetchResponse = { status: 200, body: templateContent };
    const req = new Request("https://gateway.example.com/api/raw/reality_rotate.template.sh", {
      headers: { "Authorization": "Bearer my-test-auth-key" }
    });
    const res = await worker.fetch(req, env);
    const text = await res.text();

    assert(res.status === 200, "Raw template proxy returns HTTP 200");
    assert(text === templateContent, "Raw template proxy returns file content");
    assert(capturedFetches.length === 1, "fetch called exactly once for raw template");
    assert(
      capturedFetches[0].url === "https://raw.githubusercontent.com/svbmwjwj/snack-connoisseur/main/templates/reality_rotate.template.sh",
      "Raw template URL targets GitHub main branch template"
    );
    assert(
      capturedFetches[0].options.headers["Authorization"] === "token ghp_MockGitHubToken123456789",
      "Raw template fetch includes GitHub token authorization"
    );
    assert(
      capturedFetches[0].options.headers["User-Agent"] === "Snack-Gateway",
      "Raw template fetch sets Snack-Gateway User-Agent"
    );
  }

  // Test 9: Release Asset Proxy (/api/release/:filename)
  {
    capturedFetches = [];
    const mockReleaseData = {
      assets: [
        { name: "1.2.3.4_20260816.csv", url: "https://api.github.com/repos/svbmwjwj/snack-connoisseur/releases/assets/999888" }
      ]
    };
    const mockCsvContent = "ip,port,tls,domain,stars\n1.2.3.4,443,true,apple.com,5";

    // Custom multi-fetch mock
    let fetchCount = 0;
    const origFetch = globalThis.fetch;
    globalThis.fetch = async (url, options = {}) => {
      capturedFetches.push({ url, options });
      fetchCount++;
      if (fetchCount === 1) {
        return new Response(JSON.stringify(mockReleaseData), { status: 200, headers: { 'content-type': 'application/json' } });
      } else {
        return new Response(mockCsvContent, { status: 200, headers: { 'content-type': 'text/csv' } });
      }
    };

    const req = new Request("https://gateway.example.com/api/release/1.2.3.4_20260816.csv", {
      headers: { "Authorization": "Bearer my-test-auth-key" }
    });
    const res = await worker.fetch(req, env);
    const text = await res.text();

    assert(res.status === 200, "Release asset proxy returns HTTP 200");
    assert(text === mockCsvContent, "Release asset proxy returns CSV content");
    assert(capturedFetches.length === 2, "fetch called twice (release metadata + asset download)");
    assert(
      capturedFetches[0].url === "https://api.github.com/repos/svbmwjwj/snack-connoisseur/releases/tags/latest",
      "Release metadata targets latest release"
    );
    assert(
      capturedFetches[1].url === "https://api.github.com/repos/svbmwjwj/snack-connoisseur/releases/assets/999888",
      "Asset download targets asset url with octet-stream"
    );

    globalThis.fetch = origFetch;
  }

  // Test 10: KV Storage PUT & GET (/api/storage/:filename)
  {
    const mockKvStore = {};
    const mockEnvWithKv = {
      ...env,
      SNACK_KV: {
        async put(key, val, options) {
          mockKvStore[key] = val;
        },
        async get(key) {
          return mockKvStore[key] || null;
        }
      }
    };

    const csvData = "ip,port,tls,domain,stars\n1.1.1.1,443,true,cloudflare.com,5";
    
    // Test PUT
    const putReq = new Request("https://gateway.example.com/api/storage/hk_test.csv", {
      method: "PUT",
      headers: {
        "Authorization": "Bearer my-test-auth-key",
        "Content-Type": "text/csv"
      },
      body: csvData
    });
    const putRes = await worker.fetch(putReq, mockEnvWithKv);
    assert(putRes.status === 200, "KV storage PUT returns HTTP 200");
    const putJson = await putRes.json();
    assert(putJson.ok === true, "KV storage PUT returns ok: true");

    // Test GET via /api/storage/
    const getReq = new Request("https://gateway.example.com/api/storage/hk_test.csv", {
      headers: { "Authorization": "Bearer my-test-auth-key" }
    });
    const getRes = await worker.fetch(getReq, mockEnvWithKv);
    assert(getRes.status === 200, "KV storage GET returns HTTP 200");
    const getText = await getRes.text();
    assert(getText === csvData, "KV storage GET returns correct CSV content");

    // Test GET via /api/release/ (Backward compatibility layer)
    const compatReq = new Request("https://gateway.example.com/api/release/hk_test.csv", {
      headers: { "Authorization": "Bearer my-test-auth-key" }
    });
    const compatRes = await worker.fetch(compatReq, mockEnvWithKv);
    assert(compatRes.status === 200, "Release backward compatibility GET returns HTTP 200 from KV");
    const compatText = await compatRes.text();
    assert(compatText === csvData, "Release backward compatibility returns correct CSV content from KV");
  }

  print(`\nResults: ${passed} passed, ${failed} failed.`);
  if (failed > 0) {
    throw new Error(`Test suite failed with ${failed} failure(s).`);
  }
}

runAllTests().catch(err => {
  print(`Error: ${err}`);
  throw err;
});
