// Fixture-only DNS: keep transport real while making direct fallback observable.
const dns = require("node:dns");
const fs = require("node:fs");
const lookup = dns.lookup;
if (!process.env.OMNIROUTE_TEST_DNS_LOG) throw new Error("missing fixture DNS log");
dns.lookup = function (hostname, options, callback) {
  if (hostname !== "nvidia.invalid") return lookup.apply(this, arguments);
  if (typeof options === "function") {
    callback = options;
    options = {};
  }
  fs.appendFileSync(process.env.OMNIROUTE_TEST_DNS_LOG, "lookup\n", { mode: 0o600 });
  if ((typeof options === "number" ? options : options?.family) === 6) {
    const error = new Error("fixture has no IPv6 address");
    error.code = "ENOTFOUND";
    process.nextTick(callback, error);
  } else if (options?.all) {
    process.nextTick(callback, null, [{ address: "127.0.0.1", family: 4 }]);
  } else {
    process.nextTick(callback, null, "127.0.0.1", 4);
  }
};
