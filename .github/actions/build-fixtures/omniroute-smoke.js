// Test the installed application, not TypeScript source. No external providers.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const listen = server => new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
async function main() {
  const outputs = JSON.parse(fs.readFileSync("build-result.json", "utf8"));
  assert.equal(outputs.length, 1);
  const executable = path.join(outputs[0].outputs.out, "bin/omniroute");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "omniroute-package-test-"));
  fs.chmodSync(root, 0o700);
  const password = crypto.randomBytes(32).toString("hex");
  const upstreamKey = crypto.randomBytes(32).toString("hex");
  let calls = 0;
  const mock = http.createServer(async (request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.url === "/v1/models") {
      response.end(JSON.stringify({ object: "list", data: [{ id: "test", object: "model" }] }));
      return;
    }
    if (request.url !== "/v1/chat/completions" || request.headers.authorization !== `Bearer ${upstreamKey}`) {
      response.writeHead(404).end("{}");
      return;
    }
    let body = "";
    for await (const part of request) body += part;
    const payload = JSON.parse(body);
    if (payload.model !== "test" || payload.stream === true) {
      response.writeHead(400).end("{}");
      return;
    }
    calls++;
    response.end(
      JSON.stringify({
        id: "mock",
        object: "chat.completion",
        created: 1,
        model: "test",
        choices: [{ index: 0, message: { role: "assistant", content: "local mock response" }, finish_reason: "stop" }],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      }),
    );
  });
  await listen(mock);
  const reservation = http.createServer();
  await listen(reservation);
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const log = fs.openSync(path.join(root, "runtime.log"), "wx", 0o600);
  const child = spawn(executable, ["--no-open"], {
    detached: true,
    stdio: ["ignore", log, log],
    env: {
      ...process.env,
      DATA_DIR: path.join(root, "state"),
      HOME: root,
      INITIAL_PASSWORD: password,
      JWT_SECRET: crypto.randomBytes(32).toString("hex"),
      API_KEY_SECRET: crypto.randomBytes(32).toString("hex"),
      PORT: String(port),
      OMNIROUTE_PORT: String(port),
      API_HOST: "127.0.0.1",
      HOST: "127.0.0.1",
      HOSTNAME: "127.0.0.1",
      OMNIROUTE_SERVER_HOST: "127.0.0.1",
      REQUIRE_API_KEY: "true",
      OMNIROUTE_ENABLE_LIVE_WS: "0",
    },
  });
  child.on("error", () => {});
  let cookie;
  async function request(route, method = "GET", body, key) {
    return fetch(base + route, {
      method,
      signal: AbortSignal.timeout(15000),
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(key ? { Authorization: `Bearer ${key}` } : cookie ? { Cookie: cookie } : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  }
  async function json(route, method, body, key) {
    const response = await request(route, method, body, key);
    assert(response.ok, `${method || "GET"} ${route}: HTTP ${response.status}`);
    return response.json();
  }
  try {
    let ready = false;
    for (let attempt = 0; attempt < 90; attempt++) {
      assert.equal(child.exitCode, null, "gateway exited during startup");
      try {
        const response = await fetch(base + "/api/health", { signal: AbortSignal.timeout(1000) });
        if (response.ok) {
          ready = true;
          break;
        }
      } catch {}
      await delay(1000);
    }
    assert(ready, "installed gateway did not become ready within 90 seconds");
    const login = await request("/api/auth/login", "POST", { password });
    assert(login.ok, `login: HTTP ${login.status}`);
    cookie = login.headers
      .getSetCookie()
      .map(value => value.split(";")[0])
      .join("; ");
    assert(cookie);
    const { node } = await json("/api/provider-nodes", "POST", {
      name: "Local smoke",
      prefix: "smoke",
      type: "openai-compatible",
      apiType: "chat",
      baseUrl: `http://127.0.0.1:${mock.address().port}/v1`,
    });
    assert.equal(typeof node.id, "string");
    await json("/api/providers", "POST", { provider: node.id, name: "Local smoke", apiKey: upstreamKey });
    await json("/api/provider-models", "POST", { provider: node.id, modelId: "test", apiFormat: "chat-completions" });
    const model = "smoke/test";
    const policy = { modelAccessMode: "restricted", allowedModels: [model], allowedCombos: [], scopes: [] };
    const chat = key =>
      json(
        "/v1/chat/completions",
        "POST",
        {
          model,
          messages: [{ role: "user", content: "hello" }],
          stream: false,
          max_tokens: 8,
        },
        key,
      );
    const expiry = new Date(Date.now() + 12000).toISOString();
    const key = await json("/api/keys", "POST", { ...policy, name: "expiry-smoke", expiresAt: expiry });
    assert.equal(key.expiresAt, expiry);
    const stored = (await json("/api/keys")).keys.find(entry => entry.id === key.id);
    assert.equal(stored.expiresAt, expiry);
    const beforeCalls = calls;
    assert.equal((await chat(key.key)).choices[0].message.content, "local mock response");
    assert.equal(calls, beforeCalls + 1, "request must reach only the local mock");
    await delay(Math.max(0, Date.parse(expiry) - Date.now() + 300));
    const expired = await request(
      "/v1/chat/completions",
      "POST",
      {
        model,
        messages: [{ role: "user", content: "hello" }],
        stream: false,
      },
      key.key,
    );
    assert([401, 403].includes(expired.status), `expired key accepted: HTTP ${expired.status}`);
    assert.equal(calls, beforeCalls + 1, "expired key reached upstream");
    assert.equal(child.exitCode, null, "expiry must work without restarting the gateway");
    console.log("PASS: packaged creation, persistence/readback, inference before expiry, rejection after expiry");

    for (const value of [undefined, null]) {
      const created = await json("/api/keys", "POST", {
        ...policy,
        name: `nonexpiring-${value}`,
        ...(value === undefined ? {} : { expiresAt: value }),
      });
      assert.equal(created.expiresAt, null);
      assert.equal((await chat(created.key)).choices[0].message.content, "local mock response");
    }
    console.log("PASS: omitted/null expiry compatibility");
    const count = (await json("/api/keys")).keys.length;
    for (const value of ["not-a-date", 123, true, {}, []]) {
      const response = await request("/api/keys", "POST", { ...policy, name: "invalid-expiry", expiresAt: value });
      assert.equal(response.status, 400, "invalid expiry must be a validation error");
    }
    assert.equal((await json("/api/keys")).keys.length, count);
    console.log("PASS: malformed expiry creates no key");
  } finally {
    if (child.pid) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {}
      await delay(1000);
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {}
    }
    mock.closeAllConnections();
    await new Promise(resolve => mock.close(resolve));
    fs.closeSync(log);
    fs.rmSync(root, { recursive: true, force: true });
  }
}
main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
