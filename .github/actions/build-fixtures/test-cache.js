// Run with node; requires the worker's bash, jq and Python 3, no live credentials.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const workflow = fs.readFileSync(path.join(__dirname, "../../workflows/build.yml"), "utf8");
assert.match(workflow, /push-to-cache:[\s\S]*?default: false/);
const publication = workflow.split("      - name: publish successful outputs\n")[1].split("      - name:")[0];
assert.match(publication, /if:.*inputs\.packages != '' && inputs\.push-to-cache/);
const script = publication
  .split("        run: |\n")[1]
  .split("\n")
  .map(line => line.slice(10))
  .join("\n");
assert.equal(spawnSync("bash", ["-n"], { input: script }).status, 0);

for (const scenario of ["success", "push failure", "missing token", "empty outputs", "malformed result"]) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "build-cache-test-"));
  try {
    fs.writeFileSync(
      path.join(root, "build-result.json"),
      scenario === "malformed result"
        ? "{"
        : JSON.stringify(scenario === "empty outputs" ? [] : [{ outputs: { out: "/nix/store/test-package" } }]),
    );
    const env = {
      ...process.env,
      BASH_ENV: "/dev/null",
      ATTIC_SERVER: "https://cache.example.invalid/",
      ATTIC_CACHE: "test-cache",
      ATTIC_TOKEN: scenario === "missing token" ? "" : "synthetic-test-token",
      GITHUB_STEP_SUMMARY: path.join(root, "summary"),
      RECORD: path.join(root, "config-path"),
      PUBLISH_EXIT: scenario === "push failure" ? "1" : "0",
    };
    const stub = `
nix() {
  [ "$*" = "shell .#attic-client -c attic push --stdin test-cache" ] || return 99
  read -r store_path
  [ "$store_path" = "/nix/store/test-package" ] || return 98
  python3 - <<'PY'
import os, stat, tomllib
from pathlib import Path
root = Path(os.environ["XDG_CONFIG_HOME"])
directory = root / "attic"
config = directory / "config.toml"
assert stat.S_IMODE(directory.stat().st_mode) == 0o700
assert stat.S_IMODE(config.stat().st_mode) == 0o600
data = tomllib.loads(config.read_text())
assert data["servers"]["worker"]["token"] == os.environ["ATTIC_TOKEN"]
assert data["servers"]["worker"]["endpoint"] == os.environ["ATTIC_SERVER"]
Path(os.environ["RECORD"]).write_text(str(root))
raise SystemExit(int(os.environ["PUBLISH_EXIT"]))
PY
}
`;
    const result = spawnSync("bash", ["-euo", "pipefail", "-c", stub + script], {
      cwd: root,
      env,
      encoding: "utf8",
      timeout: 10000,
    });
    assert.equal(result.status === 0, scenario === "success", `${scenario}: ${result.stderr}`);
    assert(!result.stdout.includes("synthetic-test-token"));
    assert(!result.stderr.includes("synthetic-test-token"));
    if (fs.existsSync(env.RECORD)) assert(!fs.existsSync(fs.readFileSync(env.RECORD, "utf8")));
    assert.equal(fs.existsSync(env.GITHUB_STEP_SUMMARY), scenario === "success");
    console.log(`PASS: ${scenario}`);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}
