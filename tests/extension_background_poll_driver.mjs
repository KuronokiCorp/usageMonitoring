// tests/extension_background_poll_driver.mjs -- drives extension/
// background.js under plain `node`, no browser (spec docs/specs/
// chrome-extension-control-panel.md section 10.2: "the polling routine
// must therefore be callable with an injected fetch"). Two things live
// here:
//
//   1. AC-15 -- pollTick() is the entire network surface of a tick: exactly
//      GET /api/logs then GET /api/jobs, in that order, nothing else.
//   2. The 403-pause/backoff/recovery logic in tick()/handleTickFailure()
//      (spec 5.6) -- flagged by Dida's 2026-08-16 verification pass as
//      having ZERO automated coverage (only reachable, until now, through a
//      real Chrome session). Driven here against a minimal in-memory fake
//      chrome.storage/alarms/notifications/action, with an injected
//      fetchImpl standing in for the real network -- background.js's own
//      logic is unchanged beyond making tick()'s and handleResume()'s fetch
//      boundary injectable (see background.js's history for that diff).
//
// background.js's top-level chrome.* registrations are guarded by `typeof
// chrome !== "undefined"`, so it imports cleanly whether or not a fake
// chrome is installed first.
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = path.join(HERE, "..", "extension");

// -- A minimal, in-memory fake chrome.* environment. Only the surface
// background.js actually calls is implemented; everything records what it
// was asked to do so assertions can inspect it afterward.
function installFakeChrome() {
  const store = {};
  const calls = {
    notifications: [],
    badgeText: [],
    badgeColor: [],
    alarmsCreated: [],
    alarmsCleared: [],
  };
  const chrome = {
    storage: {
      local: {
        get: (key) =>
          Promise.resolve(typeof key === "string" ? { [key]: store[key] } : { ...store }),
        set: (obj) => {
          Object.assign(store, obj);
          return Promise.resolve();
        },
      },
    },
    alarms: {
      create: (name, info) => {
        calls.alarmsCreated.push({ name, info });
        return Promise.resolve(true);
      },
      clear: (name) => {
        calls.alarmsCleared.push(name);
        return Promise.resolve(true);
      },
      onAlarm: { addListener: () => {} },
    },
    notifications: {
      create: (id, options, callback) => {
        calls.notifications.push({ id, options });
        if (callback) callback();
      },
    },
    action: {
      setBadgeText: (opts) => {
        calls.badgeText.push(opts.text);
        return Promise.resolve();
      },
      setBadgeBackgroundColor: (opts) => {
        calls.badgeColor.push(opts.color);
        return Promise.resolve();
      },
    },
    runtime: {
      id: "fake-extension-id-for-node-tests",
      onInstalled: { addListener: () => {} },
      onStartup: { addListener: () => {} },
      onMessage: { addListener: () => {} },
    },
  };
  globalThis.chrome = chrome;
  return { store, calls };
}

const fake = installFakeChrome();
const { pollTick, tick, handleResume } = await import(path.join(EXT, "background.js"));

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push({ name, ok: true });
  } catch (err) {
    results.push({ name, ok: false, error: String(err && err.stack ? err.stack : err) });
  }
}
async function checkAsync(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
  } catch (err) {
    results.push({ name, ok: false, error: String(err && err.stack ? err.stack : err) });
  }
}

const BASE = "http://127.0.0.1:9"; // never actually connected to -- fetch is fully stubbed

// -- AC-15: pollTick() is exactly GET /api/logs then GET /api/jobs.
await checkAsync("AC15_pollTick_exactly_two_gets_in_order", async () => {
  const calls = [];
  const stubFetch = (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || "GET" });
    const p = new URL(url).pathname;
    const body = p === "/api/logs" ? [] : p === "/api/jobs" ? [] : { error: "unexpected path" };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  };
  await pollTick(BASE, { fetchImpl: stubFetch, timeoutMs: 4000 });
  assert.equal(calls.length, 2, `expected exactly 2 requests, got ${calls.length}`);
  assert.equal(calls[0].method, "GET");
  assert.equal(new URL(calls[0].url).pathname, "/api/logs", "first request must be GET /api/logs");
  assert.equal(calls[1].method, "GET");
  assert.equal(new URL(calls[1].url).pathname, "/api/jobs", "second request must be GET /api/jobs");
  for (const c of calls) {
    assert.notEqual(new URL(c.url).pathname, "/api/sessions", "background tick must never touch /api/sessions");
  }
});

// -- 403 handling: pause, one notification, badge "!", no further fetches
// on subsequent driven ticks while paused (spec 5.6).
function make403Fetch() {
  let n = 0;
  const fn = () => {
    n++;
    return Promise.resolve({
      ok: false,
      status: 403,
      json: () => Promise.resolve({ error: "forbidden" }),
    });
  };
  fn.count = () => n;
  return fn;
}

await checkAsync("forbidden_403_pauses_after_first_tick", async () => {
  const fetch403 = make403Fetch();
  await tick({ fetchImpl: fetch403 });
  assert.equal(fetch403.count(), 1, "a 403 on the first GET must not attempt the second");
  const state = fake.store.state;
  assert.equal(state.paused, true);
  assert.equal(state.pauseReason, "forbidden");
  assert.equal(state.reachable, false);
});

await checkAsync("forbidden_403_fires_exactly_one_notification", () => {
  const forbiddenNotifs = fake.calls.notifications.filter((n) => n.id === "itermon-forbidden");
  assert.equal(forbiddenNotifs.length, 1, `expected exactly 1 "itermon-forbidden" notification, got ${forbiddenNotifs.length}`);
  assert.match(
    forbiddenNotifs[0].options.message,
    /--allow-origin chrome-extension:\/\/fake-extension-id-for-node-tests/,
    "notification must carry the copy-ready --allow-origin command with the live extension id"
  );
});

check("forbidden_403_sets_badge_to_bang", () => {
  assert.equal(fake.calls.badgeText[fake.calls.badgeText.length - 1], "!");
});

check("forbidden_403_clears_the_alarm", () => {
  assert.ok(fake.calls.alarmsCleared.includes("itermon-poll"), "chrome.alarms.clear must be called with the poll alarm's name");
});

await checkAsync("paused_poller_makes_no_further_fetches_on_later_ticks", async () => {
  const fetch403 = make403Fetch(); // a FRESH counter -- must stay at 0
  const notifCountBefore = fake.calls.notifications.length;
  await tick({ fetchImpl: fetch403 });
  await tick({ fetchImpl: fetch403 });
  await tick({ fetchImpl: fetch403 });
  assert.equal(fetch403.count(), 0, "a paused poller must never call fetch again on its own");
  assert.equal(
    fake.calls.notifications.length, notifCountBefore,
    "a paused poller must not raise any further notification on later ticks"
  );
});

// -- Recovery: Retry (handleResume) with a fetchImpl that now succeeds
// resumes polling, clears paused, and the poller actually fetches again.
await checkAsync("retry_with_200_resumes_polling_and_clears_paused", async () => {
  let n = 0;
  const fetch200 = (url) => {
    n++;
    const p = new URL(url).pathname;
    const body = p === "/api/logs" ? [] : p === "/api/jobs" ? [] : { error: "unexpected path" };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  };
  await handleResume({ fetchImpl: fetch200 });
  assert.equal(n, 2, `expected exactly 2 requests (logs, jobs) on the recovering tick, got ${n}`);
  const state = fake.store.state;
  assert.equal(state.paused, false, "paused must be cleared after a successful retry");
  assert.equal(state.pauseReason, null);
  assert.equal(state.reachable, true);
});

check("recovery_clears_the_bang_badge", () => {
  assert.equal(fake.calls.badgeText[fake.calls.badgeText.length - 1], "", 'badge must go back to "" once unpaused with no unread errors');
});

check("recovery_fires_a_back_notification_since_reachable_had_gone_false", () => {
  const backNotifs = fake.calls.notifications.filter((n) => n.id === "itermon-conn");
  assert.ok(backNotifs.length >= 1, "expected at least one itermon-conn notification across the failure->recovery sequence");
  const last = backNotifs[backNotifs.length - 1];
  assert.match(last.options.message, /back/);
});

const failed = results.filter((r) => !r.ok);
const summary = { total: results.length, passed: results.length - failed.length, failed: failed.length };
console.log(JSON.stringify(summary));
if (failed.length > 0) {
  for (const f of failed) console.error(`FAIL ${f.name}: ${f.error}`);
  process.exit(1);
}
process.exit(0);
