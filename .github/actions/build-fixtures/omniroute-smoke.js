// Test the installed application, not TypeScript source. No external providers.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const net = require("node:net");
const crypto = require("node:crypto");
const { execFile, execSync, spawn } = require("node:child_process");
const { promisify } = require("node:util");
const https = require("node:https");

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const listen = server => new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
function parseSocksConnect(buf) {
  if (buf.length < 4) return null;
  if (buf[0] !== 5 || buf[1] !== 1 || buf[2] !== 0) return { error: true };
  if (buf[3] === 3) {
    if (buf.length < 5) return null;
    const n = buf[4];
    const need = 7 + n;
    if (buf.length < need) return null;
    return { host: buf.subarray(5, 5 + n).toString(), port: buf.readUInt16BE(5 + n), need };
  }
  if (buf[3] === 1) {
    if (buf.length < 10) return null;
    return { host: `${buf[4]}.${buf[5]}.${buf[6]}.${buf[7]}`, port: buf.readUInt16BE(8), need: 10 };
  }
  return { error: true };
}
async function main() {
  const outputs = JSON.parse(fs.readFileSync("build-result.json", "utf8"));
  assert.equal(outputs.length, 1);
  const executable = path.join(outputs[0].outputs.out, "bin/omniroute");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "omniroute-package-test-"));
  fs.chmodSync(root, 0o700);
  const password = crypto.randomBytes(32).toString("hex");
  const upstreamKey = crypto.randomBytes(32).toString("hex");
  const jwtSecret = crypto.randomBytes(32).toString("hex");
  const apiKeySecret = crypto.randomBytes(32).toString("hex");
  const redactions = [password, upstreamKey, jwtSecret, apiKeySecret];
  let tlsPort = 0;
  let tlsTcpHits = 0;
  let tlsHits = 0;
  let tls = null;
  let tlsTrustedPort = 0;
  let tlsTrustedHits = 0;
  let tlsTrustedViaSocks = 0;
  let tlsTrusted = null;
  let childB = null;
  let logB;
  let inferenceCalls = 0;
  let nvidiaCalls = 0;
  let nvidiaViaSocks = 0;
  let nvidiaDirect = 0;
  const downKey = crypto.randomBytes(32).toString("hex");
  redactions.push(downKey);
  const proxiedPorts = new Set();
  execSync(
    `openssl req -x509 -newkey rsa:2048 -nodes -keyout "${path.join(root, "ca-key.pem")}" -out "${path.join(root, "ca-cert.pem")}" -days 1 -subj "/CN=smoke-ca"`,
    { stdio: "ignore" },
  );
  execSync(
    `openssl req -x509 -newkey rsa:2048 -nodes -keyout "${path.join(root, "untrusted-key.pem")}" -out "${path.join(root, "untrusted-cert.pem")}" -days 1 -subj "/CN=nvidia-tls.invalid" -addext "subjectAltName=DNS:nvidia-tls.invalid"`,
    { stdio: "ignore" },
  );
  execSync(
    `openssl req -newkey rsa:2048 -nodes -keyout "${path.join(root, "trusted-key.pem")}" -out "${path.join(root, "trusted.csr")}" -subj "/CN=nvidia-https.invalid"`,
    { stdio: "ignore" },
  );
  fs.writeFileSync(path.join(root, "trusted.ext"), "subjectAltName=DNS:nvidia-https.invalid\n");
  execSync(
    `openssl x509 -req -in "${path.join(root, "trusted.csr")}" -CA "${path.join(root, "ca-cert.pem")}" -CAkey "${path.join(root, "ca-key.pem")}" -CAcreateserial -out "${path.join(root, "trusted-cert.pem")}" -days 1 -extfile "${path.join(root, "trusted.ext")}"`,
    { stdio: "ignore" },
  );
  const mock = http.createServer(async (request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.url === "/v1/models") {
      response.end(JSON.stringify({ object: "list", data: [{ id: "test", object: "model" }] }));
      return;
    }
    const auth = request.headers.authorization;
    if (request.url !== "/v1/chat/completions" || (auth !== `Bearer ${upstreamKey}` && auth !== `Bearer ${downKey}`)) {
      response.writeHead(404).end("{}");
      return;
    }
    if (auth === `Bearer ${downKey}`) {
      response.writeHead(503).end(JSON.stringify({ error: "upstream unavailable" }));
      return;
    }
    let body = "";
    for await (const part of request) body += part;
    const payload = JSON.parse(body);
    if (payload.stream === true) {
      response.writeHead(400).end("{}");
      return;
    }
    if (payload.messages?.[0]?.content === "test" && payload.max_tokens === 1) {
      nvidiaCalls++;
      if (request.socket.viaSocks === undefined) {
        request.socket.viaSocks = proxiedPorts.has(request.socket.remotePort);
      }
      if (request.socket.viaSocks) nvidiaViaSocks++;
      else nvidiaDirect++;
      response.end(
        JSON.stringify({
          id: "nvidia-probe",
          object: "chat.completion",
          created: 1,
          model: payload.model,
          choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        }),
      );
      return;
    }
    if (payload.model !== "test") {
      response.writeHead(400).end("{}");
      return;
    }
    inferenceCalls++;
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
  const mockPort = mock.address().port;
  const tunnels = new Set();
  let socksConnects = 0;
  const socks = net.createServer(socket => {
    tunnels.add(socket);
    socket.setTimeout(5000, () => socket.destroy());
    let buf = Buffer.alloc(0);
    let stage = "greet";
    socket.on("data", chunk => {
      if (stage === "pipe") return;
      buf = Buffer.concat([buf, chunk]);
      if (stage === "greet") {
        if (buf.length < 2) return;
        if (buf[0] !== 5) return socket.destroy();
        const n = buf[1];
        if (buf.length < 2 + n) return;
        if (!buf.subarray(2, 2 + n).includes(0)) return socket.destroy();
        buf = buf.subarray(2 + n);
        socket.write(Buffer.from([5, 0]));
        stage = "req";
      }
      if (stage !== "req") return;
      const parsed = parseSocksConnect(buf);
      if (!parsed) return;
      const allowed =
        (parsed.host === "nvidia.invalid" && parsed.port === mockPort) ||
        (tlsPort !== 0 && parsed.host === "nvidia-tls.invalid" && parsed.port === tlsPort) ||
        (tlsTrustedPort !== 0 && parsed.host === "nvidia-https.invalid" && parsed.port === tlsTrustedPort);
      if (parsed.error || !allowed) return socket.destroy();
      buf = buf.subarray(parsed.need);
      socksConnects++;
      const dest = net.connect(parsed.port, "127.0.0.1");
      tunnels.add(dest);
      dest.on("connect", () => {
        proxiedPorts.add(dest.localPort);
        socket.setTimeout(0);
        socket.write(Buffer.from([5, 0, 0, 1, 127, 0, 0, 1, parsed.port >> 8, parsed.port & 0xff]));
        socket.removeAllListeners("data");
        if (buf.length) dest.write(buf);
        socket.pipe(dest);
        dest.pipe(socket);
        stage = "pipe";
      });
      dest.on("error", () => socket.destroy());
      socket.on("close", () => dest.destroy());
    });
    socket.on("error", () => {});
  });
  const destroySocks = () => {
    for (const tunnel of tunnels) tunnel.destroy();
    tunnels.clear();
    return new Promise(resolve => socks.close(resolve));
  };
  await new Promise(resolve => socks.listen(0, "127.0.0.1", resolve));
  const socksPort = socks.address().port;
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
      JWT_SECRET: jwtSecret,
      API_KEY_SECRET: apiKeySecret,
      PORT: String(port),
      OMNIROUTE_PORT: String(port),
      API_HOST: "127.0.0.1",
      HOST: "127.0.0.1",
      HOSTNAME: "127.0.0.1",
      OMNIROUTE_SERVER_HOST: "127.0.0.1",
      REQUIRE_API_KEY: "true",
      NODE_EXTRA_CA_CERTS: path.join(root, "ca-cert.pem"),
      PROXY_FAIL_OPEN: "false",
      APP_LOG_TO_FILE: "true",
      APP_LOG_FILE_PATH: path.join(root, "app-a.log"),
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
    await json("/api/settings/proxies", "POST", {
      name: "smoke-socks",
      type: "socks5",
      host: "127.0.0.1",
      port: socksPort,
      status: "active",
      source: "manual",
      family: "auto",
      assignment: { scope: "global" },
    });
    const nvidia = await json("/api/providers", "POST", {
      provider: "nvidia",
      name: "NVIDIA probe",
      apiKey: upstreamKey,
      providerSpecificData: { baseUrl: `http://nvidia.invalid:${mockPort}/v1/chat/completions` },
    });
    const nvidiaId = nvidia.connection?.id || nvidia.id;
    assert.equal(typeof nvidiaId, "string");
    const nvidiaDeadline = Date.now() + 15000;
    while ((nvidiaCalls < 1 || socksConnects < 1) && Date.now() < nvidiaDeadline) await delay(100);
    assert(socksConnects >= 1, "NVIDIA must CONNECT nvidia.invalid through SOCKS");
    assert(nvidiaCalls >= 1, "NVIDIA probe must reach the mock through SOCKS");
    const nvidiaBefore = nvidiaCalls;
    const nvidiaProbe = await json(`/api/providers/${nvidiaId}/test`, "POST", {});
    assert.equal(nvidiaProbe.valid, true, "NVIDIA probe must succeed through SOCKS");
    assert.notEqual(nvidiaProbe.skipped, true);
    assert.ok(!nvidiaProbe.warning);
    assert.equal(nvidiaCalls, nvidiaBefore + 1, "explicit NVIDIA probe must hit the mock once");
    const nvidiaRow = (await json("/api/providers")).connections.find(entry => entry.id === nvidiaId);
    assert.equal(nvidiaRow?.isActive, true);
    assert(nvidiaViaSocks >= 1, "NVIDIA probe must arrive through SOCKS");
    assert.equal(nvidiaDirect, 0, "NVIDIA probe must never bypass SOCKS");
    const downRow = await json("/api/providers", "POST", {
      provider: "nvidia",
      name: "NVIDIA outage",
      apiKey: downKey,
      providerSpecificData: { baseUrl: `http://nvidia.invalid:${mockPort}/v1/chat/completions` },
    });
    const downId = downRow.connection?.id || downRow.id;
    assert.equal(typeof downId, "string");
    const downProbe = await request(`/api/providers/${downId}/test`, "POST", {});
    assert.equal(downProbe.status, 200);
    assert.equal((await downProbe.json()).valid, false, "NVIDIA 5xx must fail the probe");
    const downListed = (await json("/api/providers")).connections.find(entry => entry.id === downId);
    assert.equal(downListed?.isActive, false, "failed NVIDIA probe must not activate");
    assert.equal((await json("/api/providers")).connections.find(entry => entry.id === nvidiaId)?.isActive, true);
    console.log("PASS: NVIDIA 5xx fails closed");
    tls = https.createServer(
      {
        key: fs.readFileSync(path.join(root, "untrusted-key.pem")),
        cert: fs.readFileSync(path.join(root, "untrusted-cert.pem")),
      },
      (request, response) => {
        tlsHits++;
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end("{}");
      },
    );
    tls.on("connection", () => {
      tlsTcpHits++;
    });
    await new Promise(resolve => tls.listen(0, "127.0.0.1", resolve));
    tlsPort = tls.address().port;
    tlsTrusted = https.createServer(
      {
        key: fs.readFileSync(path.join(root, "trusted-key.pem")),
        cert: fs.readFileSync(path.join(root, "trusted-cert.pem")),
      },
      async (request, response) => {
        response.setHeader("Content-Type", "application/json");
        let body = "";
        for await (const part of request) body += part;
        const payload = JSON.parse(body);
        if (payload.messages?.[0]?.content === "test" && payload.max_tokens === 1) {
          if (request.socket.viaSocks === undefined) {
            request.socket.viaSocks = proxiedPorts.has(request.socket.remotePort);
          }
          if (request.socket.viaSocks) tlsTrustedViaSocks++;
          tlsTrustedHits++;
          response.end(
            JSON.stringify({
              id: "nvidia-tls-probe",
              object: "chat.completion",
              created: 1,
              model: payload.model,
              choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
              usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
            }),
          );
          return;
        }
        response.writeHead(400).end("{}");
      },
    );
    await new Promise(resolve => tlsTrusted.listen(0, "127.0.0.1", resolve));
    tlsTrustedPort = tlsTrusted.address().port;
    const tlsConn = await json("/api/providers", "POST", {
      provider: "nvidia",
      name: "NVIDIA TLS",
      apiKey: upstreamKey,
      providerSpecificData: { baseUrl: `https://nvidia-tls.invalid:${tlsPort}/v1/chat/completions` },
    });
    const tlsId = tlsConn.connection?.id || tlsConn.id;
    assert.equal(typeof tlsId, "string");
    const tlsProbe = await request(`/api/providers/${tlsId}/test`, "POST", {});
    assert.equal(tlsProbe.status, 200);
    assert.equal((await tlsProbe.json()).valid, false, "self-signed TLS must fail closed");
    assert(tlsTcpHits >= 1, "TLS bytes must reach the mock through SOCKS");
    assert.equal(tlsHits, 0, "rejected TLS must not deliver an HTTP request");
    console.log("PASS: NVIDIA TLS verification fails closed");
    const tlsTrustedConn = await json("/api/providers", "POST", {
      provider: "nvidia",
      name: "NVIDIA trusted TLS",
      apiKey: upstreamKey,
      providerSpecificData: { baseUrl: `https://nvidia-https.invalid:${tlsTrustedPort}/v1/chat/completions` },
    });
    const tlsTrustedId = tlsTrustedConn.connection?.id || tlsTrustedConn.id;
    assert.equal(typeof tlsTrustedId, "string");
    const tlsTrustedProbe = await json(`/api/providers/${tlsTrustedId}/test`, "POST", {});
    assert.equal(tlsTrustedProbe.valid, true, "trusted TLS must succeed through SOCKS");
    assert(tlsTrustedHits >= 1, "trusted TLS probe must reach the mock");
    assert(tlsTrustedViaSocks >= 1, "trusted TLS probe must arrive through SOCKS");
    console.log("PASS: NVIDIA trusted TLS succeeds through SOCKS");
    await destroySocks();
    const downCalls = nvidiaCalls;
    const nvidiaDown = await request(`/api/providers/${nvidiaId}/test`, "POST", {});
    assert.equal(nvidiaDown.status, 200);
    const nvidiaDownBody = await nvidiaDown.json();
    assert.equal(nvidiaDownBody.valid, false, "NVIDIA probe must fail when SOCKS is down");
    assert.equal(nvidiaCalls, downCalls, "downed SOCKS must not reach the mock");
    console.log("PASS: NVIDIA proxy isolation");
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
    redactions.push(key.key);
    assert.equal(key.expiresAt, expiry);
    const stored = (await json("/api/keys")).keys.find(entry => entry.id === key.id);
    assert.equal(stored.expiresAt, expiry);
    await json(`/api/keys/${key.id}`, "PATCH", { scopes: [] });
    const authenticatedWithoutScope = await request("/api/v1/me/status", "GET", undefined, key.key);
    assert.equal(authenticatedWithoutScope.status, 403);
    assert.deepEqual(await authenticatedWithoutScope.json(), { error: "Forbidden" });
    assert.equal((await request("/api/v1/me/status", "GET", undefined, "invalid-probe")).status, 401);
    const beforeCalls = inferenceCalls;
    assert.equal((await chat(key.key)).choices[0].message.content, "local mock response");
    assert.equal(inferenceCalls, beforeCalls + 1, "request must reach only the local mock");
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
    assert.equal(inferenceCalls, beforeCalls + 1, "expired key reached upstream");
    // validateApiKey caches successful authentication for 60 seconds upstream.
    // Chat policy above must reject immediately; the status route must reject
    // once that existing, non-sliding cache expires. Do not conflate the two.
    const statusDeadline = Date.now() + 65000;
    let statusAfterExpiry;
    do {
      statusAfterExpiry = (await request("/api/v1/me/status", "GET", undefined, key.key)).status;
      if (statusAfterExpiry === 401) break;
      assert.equal(statusAfterExpiry, 403, "unexpected status during auth-cache lifetime");
      await delay(500);
    } while (Date.now() < statusDeadline);
    assert.equal(statusAfterExpiry, 401, "status authentication outlived the upstream cache TTL");
    assert.equal(child.exitCode, null, "expiry must work without restarting the gateway");
    console.log("PASS: packaged creation, persistence/readback, inference before expiry, rejection after expiry");

    for (const value of [undefined, null]) {
      const created = await json("/api/keys", "POST", {
        ...policy,
        name: `nonexpiring-${value}`,
        ...(value === undefined ? {} : { expiresAt: value }),
      });
      redactions.push(created.key);
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

    const combo = await json("/api/combos", "POST", {
      name: "smoke-group",
      strategy: "priority",
      models: [model],
    });
    assert.equal(typeof combo.id, "string");
    await json("/api/model-combo-mappings", "POST", {
      pattern: "omniroute/smoke-group",
      comboId: combo.id,
      priority: 100,
      enabled: true,
    });
    const bridgeKey = await json("/api/keys", "POST", {
      name: "bridge-smoke",
      modelAccessMode: "restricted",
      allowedModels: ["smoke-group", "omniroute/smoke-group"],
      allowedCombos: ["smoke-group"],
      scopes: [],
      expiresAt: null,
    });
    redactions.push(bridgeKey.key);
    const catalog = await json("/v1/models", "GET", undefined, bridgeKey.key);
    const catalogIds = catalog.data.map(entry => entry.id);
    assert(catalogIds.includes("smoke-group"), "combo name must appear in discovery");
    assert(!catalogIds.includes(model), "raw provider model must stay undiscoverable");
    const denied = await request(
      "/v1/chat/completions",
      "POST",
      {
        model,
        messages: [{ role: "user", content: "direct" }],
        stream: false,
        max_tokens: 8,
      },
      bridgeKey.key,
    );
    assert.equal(denied.status, 403, "raw provider model must stay denied");
    const bridgeResponse = await json(
      "/v1/chat/completions",
      "POST",
      {
        model: "omniroute/smoke-group",
        messages: [{ role: "user", content: "bridge test" }],
        stream: false,
        max_tokens: 8,
      },
      bridgeKey.key,
    );
    assert.equal(bridgeResponse.choices[0].message.content, "local mock response");
    console.log("PASS: combo-name discovery, mapped inference, raw model denied");
    const reservationB = http.createServer();
    await listen(reservationB);
    const portB = reservationB.address().port;
    await new Promise(resolve => reservationB.close(resolve));
    const baseB = `http://127.0.0.1:${portB}`;
    logB = fs.openSync(path.join(root, "runtime-b.log"), "wx", 0o600);
    const passwordB = crypto.randomBytes(32).toString("hex");
    const jwtSecretB = crypto.randomBytes(32).toString("hex");
    const apiKeySecretB = crypto.randomBytes(32).toString("hex");
    redactions.push(passwordB, jwtSecretB, apiKeySecretB);
    childB = spawn(executable, ["--no-open"], {
      detached: true,
      stdio: ["ignore", logB, logB],
      env: {
        ...process.env,
        DATA_DIR: path.join(root, "state-b"),
        HOME: root,
        INITIAL_PASSWORD: passwordB,
        JWT_SECRET: jwtSecretB,
        API_KEY_SECRET: apiKeySecretB,
        PORT: String(portB),
        OMNIROUTE_PORT: String(portB),
        API_HOST: "127.0.0.1",
        HOST: "127.0.0.1",
        HOSTNAME: "127.0.0.1",
        OMNIROUTE_SERVER_HOST: "127.0.0.1",
        REQUIRE_API_KEY: "true",
        PROXY_FAIL_OPEN: "false",
        APP_LOG_TO_FILE: "true",
        APP_LOG_FILE_PATH: path.join(root, "app-b.log"),
        OMNIROUTE_ENABLE_LIVE_WS: "0",
      },
    });
    childB.on("error", () => {});
    let cookieB;
    async function requestB(route, method = "GET", body, key) {
      return fetch(baseB + route, {
        method,
        signal: AbortSignal.timeout(15000),
        headers: {
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          ...(key ? { Authorization: `Bearer ${key}` } : cookieB ? { Cookie: cookieB } : {}),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    }
    async function jsonB(route, method, body, key) {
      const response = await requestB(route, method, body, key);
      assert(response.ok, `B ${method || "GET"} ${route}: HTTP ${response.status}`);
      return response.json();
    }
    let readyB = false;
    for (let attempt = 0; attempt < 90; attempt++) {
      assert.equal(childB.exitCode, null, "gateway B exited during startup");
      try {
        const response = await fetch(baseB + "/api/health", { signal: AbortSignal.timeout(1000) });
        if (response.ok) {
          readyB = true;
          break;
        }
      } catch {}
      await delay(1000);
    }
    assert(readyB, "gateway B did not become ready within 90 seconds");
    const loginB = await requestB("/api/auth/login", "POST", { password: passwordB });
    assert(loginB.ok, `B login: HTTP ${loginB.status}`);
    cookieB = loginB.headers
      .getSetCookie()
      .map(value => value.split(";")[0])
      .join("; ");
    assert(cookieB);
    const fixture = name => path.join(__dirname, name);
    const helperHash = crypto
      .createHash("sha256")
      .update(fs.readFileSync(fixture("omniroute-provision.sh")))
      .digest("hex");
    assert.equal(helperHash, fs.readFileSync(fixture("omniroute-provision.sha256"), "utf8").trim());
    console.log(`Provisioner SHA-256: ${helperHash}`);
    const testRoot = path.join(root, "provision");
    fs.mkdirSync(testRoot, { mode: 0o700 });
    fs.mkdirSync(path.join(testRoot, "credentials"), { mode: 0o700 });
    fs.writeFileSync(path.join(testRoot, "credentials/upstream"), upstreamKey, { mode: 0o600 });
    fs.writeFileSync(path.join(testRoot, "a.headers"), `Cookie: ${cookie}\n`, { mode: 0o600 });
    fs.writeFileSync(path.join(testRoot, "b.headers"), `Cookie: ${cookieB}\n`, { mode: 0o600 });
    const keyFiles = ["keys-a/can-client-key", "keys-b/dejana-client-key", "keys-b/dejana-bridge-key"];
    async function provision(phase) {
      let result;
      try {
        result = await promisify(execFile)("bash", [fixture("omniroute-provision-test.sh"), phase], {
          env: {
            ...process.env,
            TEST_ROOT: testRoot,
            BASE_A: base,
            BASE_B: baseB,
            MOCK_BASE: `http://127.0.0.1:${mockPort}`,
          },
          timeout: 180000,
          maxBuffer: 1024 * 1024,
        });
      } catch (error) {
        fs.appendFileSync(path.join(root, "provision.log"), error.stderr || "driver failed", { mode: 0o600 });
        throw new Error(`real provisioner ${phase} failed (exit ${error.code})`);
      } finally {
        for (const file of keyFiles) {
          if (fs.existsSync(path.join(testRoot, file)))
            redactions.push(fs.readFileSync(path.join(testRoot, file), "utf8").trim());
        }
      }
      if (phase !== "snapshot") console.log(result.stdout.trim());
      return result.stdout;
    }
    await provision("apply");
    const clientKeyA = fs.readFileSync(path.join(testRoot, keyFiles[0]), "utf8").trim();
    const bridgeKeyB = fs.readFileSync(path.join(testRoot, keyFiles[2]), "utf8").trim();
    assert.equal(
      (await requestB("/api/v1/me/status", "GET", undefined, clientKeyA)).status,
      401,
      "A key must not authenticate at B",
    );
    assert.equal(
      (await request("/api/v1/me/status", "GET", undefined, bridgeKeyB)).status,
      401,
      "B key must not authenticate at A",
    );
    const catalogB = await jsonB("/v1/models", "GET", undefined, bridgeKeyB);
    assert(catalogB.data.some(entry => entry.id === "b-group"));
    const catalogA = await json("/v1/models", "GET", undefined, clientKeyA);
    assert(
      catalogA.data.some(entry => entry.id === "a-group"),
      "A must discover a-group",
    );
    assert(!catalogA.data.some(entry => entry.id === "b-group"), "A must not discover B-local combos");
    async function forward(content) {
      const calls = inferenceCalls;
      const response = await json(
        "/v1/chat/completions",
        "POST",
        {
          model: "omniroute/a-group",
          messages: [{ role: "user", content }],
          stream: false,
          max_tokens: 8,
        },
        clientKeyA,
      );
      assert.equal(response.choices[0].message.content, "local mock response");
      assert.equal(inferenceCalls, calls + 1, "forwarded request must reach the mock exactly once");
    }
    await forward("after first real apply");
    const denialCalls = inferenceCalls;
    const deniedAcross = await request(
      "/v1/chat/completions",
      "POST",
      { model: "dejana/bsmoke/test", messages: [{ role: "user", content: "direct" }], stream: false, max_tokens: 8 },
      clientKeyA,
    );
    assert.equal(deniedAcross.status, 403, "B-local model must stay denied across the bridge");
    const deniedAtB = await requestB(
      "/v1/chat/completions",
      "POST",
      {
        model: "bsmoke/test",
        messages: [{ role: "user", content: "direct at B" }],
        stream: false,
        max_tokens: 8,
      },
      bridgeKeyB,
    );
    assert.equal(deniedAtB.status, 403);
    assert.equal(inferenceCalls, denialCalls, "denied requests must not reach upstream");
    const beforeApply = await provision("snapshot");
    await provision("mismatch");
    assert.equal(await provision("snapshot"), beforeApply, "swapped key must not mutate resources or files");
    await provision("apply");
    assert.equal(
      await provision("snapshot"),
      beforeApply,
      "second real apply must preserve IDs, policy and credential bytes",
    );
    await forward("after second real apply");
    await provision("recovery");
    await forward("through the recovered connection");
    console.log("PASS: real two-instance provisioning, identity, forwarding and cooldown recovery");
  } catch (error) {
    let diagnostic = "";
    for (const file of ["runtime.log", "runtime-b.log", "app-a.log", "app-b.log", "provision.log"]) {
      if (fs.existsSync(path.join(root, file))) {
        diagnostic +=
          `\n--- ${file} ---\n` + fs.readFileSync(path.join(root, file), "utf8").split("\n").slice(-30).join("\n");
      }
    }
    for (const value of redactions) if (value) diagnostic = diagnostic.split(value).join("<redacted>");
    diagnostic = diagnostic.replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, "<redacted-jwt>");
    console.error(diagnostic);
    throw error;
  } finally {
    if (childB && childB.pid) {
      try {
        process.kill(-childB.pid, "SIGTERM");
      } catch {}
      await delay(1000);
      try {
        process.kill(-childB.pid, "SIGKILL");
      } catch {}
    }
    if (child.pid) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {}
      await delay(1000);
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {}
    }
    await destroySocks().catch(() => {});
    for (const server of [tls, tlsTrusted]) {
      if (server) {
        try {
          server.closeAllConnections();
        } catch {}
        await new Promise(resolve => server.close(resolve));
      }
    }
    mock.closeAllConnections();
    await new Promise(resolve => mock.close(resolve));
    fs.closeSync(log);
    if (logB !== undefined) fs.closeSync(logB);
    fs.rmSync(root, { recursive: true, force: true });
  }
}
main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
