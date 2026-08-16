/**
 * background.js -- the notifier core (spec docs/specs/chrome-extension-
 * control-panel.md, section 5). MV3 service worker, woken only by
 * chrome.alarms (never setInterval/setTimeout for scheduling -- AC-23).
 *
 * The WHOLE network activity of a background tick is two GETs:
 *   GET {base}/api/logs
 *   GET {base}/api/jobs
 * NEVER the sessions-listing endpoint (BACKLOG #11, AC-14 -- that endpoint
 * costs one osascript + one ps PER SESSION, and is the panel's job to poll,
 * gated on visibility, never the background worker's), and NEVER a POST of
 * any kind (AC-43) -- the write surface lives entirely in panel.js.
 *
 * Top-level chrome.* registrations are guarded by `typeof chrome !==
 * "undefined"` so this module can also be `import`ed under plain `node` for
 * AC-15's driver (tests/extension_background_poll_driver.mjs), which needs
 * `pollTick` with an injected fetch but must not crash just importing the
 * file outside a real extension environment.
 */
import { getJson, baseUrl, loadSettings, saveSettings, loadState, saveState } from "./api.js";
import { diffLogEntries, detectJobDrops, planNotifications } from "./notify_logic.js";

export const ALARM_NAME = "itermon-poll";
const GRACE_MS = 120000; // 120s missed-slot grace period (spec 5.4b)
const FETCH_TIMEOUT_MS = 4000; // spec 5.2
const MAX_BACKOFF_MS = 5 * 60 * 1000; // spec 5.6, 5-minute ceiling
const MIN_PERIOD_MINUTES = 0.5; // spec 5.1 floor

/**
 * pollTick(base, opts) -- the entire background-tick network activity, and
 * nothing else: exactly GET /api/logs then GET /api/jobs, in that order.
 * `fetchImpl` is injectable so this is drivable under node with a stub that
 * records every call (AC-15).
 */
export async function pollTick(base, { fetchImpl, timeoutMs = FETCH_TIMEOUT_MS } = {}) {
  const logs = await getJson(`${base}/api/logs`, { timeoutMs, fetchImpl });
  const jobs = await getJson(`${base}/api/jobs`, { timeoutMs, fetchImpl });
  return { logs, jobs };
}

function periodMinutesFor(pollSeconds) {
  return Math.max(MIN_PERIOD_MINUTES, (pollSeconds || 60) / 60);
}

async function ensureAlarm(pollSeconds) {
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: periodMinutesFor(pollSeconds) });
}

async function notify({ id, title, message }) {
  return new Promise((resolve) => {
    try {
      chrome.notifications.create(
        id,
        { type: "basic", iconUrl: "icons/icon128.png", title, message },
        () => resolve()
      );
    } catch {
      resolve();
    }
  });
}

async function updateBadge(state) {
  if (state.paused) {
    await chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
    await chrome.action.setBadgeText({ text: "!" });
    return;
  }
  if (state.unread > 0) {
    await chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
    await chrome.action.setBadgeText({ text: String(Math.min(state.unread, 99)) });
    return;
  }
  await chrome.action.setBadgeText({ text: "" });
}

let ticking = false; // re-entrancy guard (spec 5.1: "must be safe to run concurrently with itself")

/**
 * tick(opts) -- exported, and its fetch boundary is injectable (`opts.
 * fetchImpl`, threaded straight into pollTick()) for the same reason
 * pollTick() itself is: it lets the 403-pause/backoff/notify/badge logic in
 * this function and handleTickFailure() below be driven deterministically
 * under plain node (tests/extension_background_poll_driver.mjs), against a
 * fake in-memory chrome.storage/alarms/notifications/action, rather than
 * being reachable only through a real browser. Real call sites (the
 * chrome.alarms.onAlarm listener below, handleResume()) call tick() with no
 * opts, so `fetchImpl` stays undefined and pollTick() falls back to the
 * global fetch exactly as before this parameter existed -- no behavior
 * change to the shipped logic.
 */
export async function tick({ fetchImpl } = {}) {
  if (ticking) return;
  ticking = true;
  try {
    const settings = await loadSettings();
    const state = await loadState();
    if (state.paused) return;

    const base = baseUrl(settings);
    let logs, jobs;
    try {
      ({ logs, jobs } = await pollTick(base, { timeoutMs: FETCH_TIMEOUT_MS, fetchImpl }));
    } catch (err) {
      await handleTickFailure(err, settings, state);
      return;
    }

    const wasReachable = state.reachable;
    const hadBackoff = state.backoffMs > 0;
    state.reachable = true;
    state.backoffMs = 0;
    if (hadBackoff) await ensureAlarm(settings.pollSeconds);
    if (wasReachable === false) {
      await notify({ id: "itermon-conn", title: "itermon", message: `admin server back at ${base}` });
    }

    const { newEntries, nextLogAnchor } = diffLogEntries(logs, state);
    const { drops, nextJobsById } = detectJobDrops(jobs, state.jobs, Date.now(), GRACE_MS);
    const notifications = planNotifications(newEntries, drops, settings);
    for (const n of notifications) await notify(n);

    const errorCount = newEntries.filter((e) => e.kind === "error").length;
    state.lastLogKey = nextLogAnchor.lastLogKey;
    state.lastLogT = nextLogAnchor.lastLogT;
    state.jobs = nextJobsById;
    if (settings.notifyErrors !== false && errorCount > 0) state.unread += errorCount;

    await saveState(state);
    await updateBadge(state);
  } finally {
    ticking = false;
  }
}

async function handleTickFailure(err, settings, state) {
  if (err && err.status === 403) {
    // Not allowlisted -- stop polling entirely (spec 5.6). Every rejected
    // request makes the server write a rate-limited rejection line into
    // activity.log with no size cap; hammering a server that will not have
    // us turns a config mistake into disk growth on an already-tight disk.
    state.paused = true;
    state.pauseReason = "forbidden";
    state.reachable = false;
    await chrome.alarms.clear(ALARM_NAME);
    const id = (chrome.runtime && chrome.runtime.id) || "<extension-id>";
    await notify({
      id: "itermon-forbidden",
      title: "itermon: not allowlisted",
      message: `Restart itermon with --allow-origin chrome-extension://${id}`,
    });
    await saveState(state);
    await updateBadge(state);
    return;
  }

  // Network failure / timeout / any other non-2xx -- back off, notify only
  // on the true/null -> false transition (spec 5.6).
  const wasReachable = state.reachable;
  state.reachable = false;
  if (wasReachable !== false) {
    await notify({
      id: "itermon-conn",
      title: "itermon",
      message: `admin server unreachable at ${baseUrl(settings)}`,
    });
  }
  const periodMs = Math.max((settings.pollSeconds || 60) * 1000, 1000);
  state.backoffMs = state.backoffMs > 0 ? Math.min(state.backoffMs * 2, MAX_BACKOFF_MS) : periodMs;
  await saveState(state);
  await updateBadge(state);
  await ensureAlarm(state.backoffMs / 1000);
}

async function handleAck() {
  const state = await loadState();
  state.unread = 0;
  await saveState(state);
  await updateBadge(state);
}

/** Called after the panel saves settings, or presses Retry (spec 7.1 /
 * 5.6): clear paused/backoff, re-create the alarm at the (possibly new)
 * period, and poll immediately so feedback arrives within a second.
 * Exported, and `opts.fetchImpl` forwards to the immediate tick() the same
 * way tick()'s own fetchImpl does, for the same node-driver reason -- the
 * real onMessage listener below calls this with no opts, unchanged. */
export async function handleResume({ fetchImpl } = {}) {
  const settings = await loadSettings();
  const state = await loadState();
  state.paused = false;
  state.pauseReason = null;
  state.backoffMs = 0;
  await saveState(state);
  await ensureAlarm(settings.pollSeconds);
  await tick({ fetchImpl });
}

if (typeof chrome !== "undefined" && chrome.runtime && chrome.alarms) {
  chrome.runtime.onInstalled.addListener(async () => {
    const settings = await loadSettings();
    await saveSettings(settings); // materializes defaults on first install
    await ensureAlarm(settings.pollSeconds);
  });

  chrome.runtime.onStartup.addListener(async () => {
    const settings = await loadSettings();
    const state = await loadState();
    if (!state.paused) await ensureAlarm(settings.pollSeconds);
  });

  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === ALARM_NAME) tick();
  });

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || typeof msg.type !== "string") return;
    if (msg.type === "ack") handleAck();
    else if (msg.type === "settings-saved" || msg.type === "retry") handleResume();
  });
}
