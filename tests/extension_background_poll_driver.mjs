// tests/extension_background_poll_driver.mjs -- drives extension/
// background.js's exported pollTick() under plain `node`, with a stub
// `fetchImpl` that records every request it is asked to make (AC-15, spec
// docs/specs/chrome-extension-control-panel.md section 10.2: "the polling
// routine must therefore be callable with an injected fetch"). background.js
// itself never calls chrome.* at import time (its top-level registrations
// are guarded by `typeof chrome !== "undefined"`), so it imports cleanly
// here with no browser present.
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = path.join(HERE, "..", "extension");

const { pollTick } = await import(path.join(EXT, "background.js"));

const calls = [];

function stubFetch(url, opts) {
  calls.push({ url, method: (opts && opts.method) || "GET" });
  const path = new URL(url).pathname;
  const body = path === "/api/logs" ? [] : path === "/api/jobs" ? [] : { error: "unexpected path" };
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  });
}

const BASE = "http://127.0.0.1:9"; // never actually connected to -- fetch is fully stubbed

await pollTick(BASE, { fetchImpl: stubFetch, timeoutMs: 4000 });

assert.equal(calls.length, 2, `expected exactly 2 requests, got ${calls.length}`);
assert.equal(calls[0].method, "GET");
assert.equal(new URL(calls[0].url).pathname, "/api/logs", "first request must be GET /api/logs");
assert.equal(calls[1].method, "GET");
assert.equal(new URL(calls[1].url).pathname, "/api/jobs", "second request must be GET /api/jobs");
for (const c of calls) {
  assert.notEqual(new URL(c.url).pathname, "/api/sessions", "background tick must never touch /api/sessions");
}

console.log(JSON.stringify({ total: 1, passed: 1, failed: 0, calls: calls.map((c) => new URL(c.url).pathname) }));
process.exit(0);
