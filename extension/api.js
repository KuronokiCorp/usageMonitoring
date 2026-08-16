/**
 * api.js -- fetch helpers, settings load/save, base-URL construction
 * (spec docs/specs/chrome-extension-control-panel.md, section 7).
 *
 * Shared by background.js (GET only -- see AC-43, the background worker
 * never POSTs) and panel.js (GET + the guarded POST allowlist, section 6).
 * No chrome.* calls belong in notify_logic.js, but this file is allowed to
 * use chrome.storage -- it is not part of the pure decision layer.
 */

// itermon's own default admin port (AC-20: this literal number may appear
// at most once across extension/**, and this declaration is that one
// occurrence -- every other file must reference the port only through
// settings, never a second hard-coded copy of the digits).
export const DEFAULT_SETTINGS = Object.freeze({
  host: "127.0.0.1",
  port: 8765,
  pollSeconds: 60,
  notifyErrors: true,
  notifyJobDrops: true,
});

export const DEFAULT_STATE = Object.freeze({
  schemaVersion: 1,
  lastLogKey: null,
  lastLogT: null,
  jobs: {},
  unread: 0,
  reachable: null,
  paused: false,
  pauseReason: null,
  backoffMs: 0,
});

/** Base URL, built once from settings -- never re-derived elsewhere. */
export function baseUrl(settings) {
  return `http://${settings.host}:${settings.port}`;
}

export async function loadSettings() {
  const got = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(got && got.settings ? got.settings : {}) };
}

export async function saveSettings(settings) {
  await chrome.storage.local.set({ settings });
}

export async function loadState() {
  const got = await chrome.storage.local.get("state");
  return { ...DEFAULT_STATE, ...(got && got.state ? got.state : {}) };
}

export async function saveState(state) {
  await chrome.storage.local.set({ state });
}

/** An HTTP error carrying the response status, so callers can branch on
 * 403 ("not allowlisted") vs any other non-2xx without re-parsing text. */
export class HttpError extends Error {
  constructor(status, body) {
    super(`HTTP ${status}`);
    this.name = "HttpError";
    this.status = status;
    this.body = body;
  }
}

async function withTimeout(timeoutMs, fn) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fn(controller.signal);
  } finally {
    clearTimeout(timer);
  }
}

/** GET `url`, parse JSON, throw HttpError on non-2xx. Timeout defaults to
 * 4000ms per spec 5.2 ("a hung server cannot pin the worker awake").
 * `fetchImpl` is injectable (defaults to the global `fetch`) so the polling
 * routine that calls this can be driven under plain `node` with a stub that
 * records every request -- AC-15 / spec section 10.2. */
export async function getJson(url, { timeoutMs = 4000, fetchImpl } = {}) {
  const doFetch = fetchImpl || fetch;
  return withTimeout(timeoutMs, async (signal) => {
    const resp = await doFetch(url, { method: "GET", cache: "no-store", signal });
    const body = await resp.json().catch(() => null);
    if (!resp.ok) throw new HttpError(resp.status, body);
    return body;
  });
}

/** POST `url` with a JSON body, parse JSON, throw HttpError on non-2xx.
 * NEVER called from background.js (AC-43) -- panel.js only, and only for
 * the five-path write allowlist (spec section 6 / AC-42). */
export async function postJson(url, payload, { timeoutMs = 8000, fetchImpl } = {}) {
  const doFetch = fetchImpl || fetch;
  return withTimeout(timeoutMs, async (signal) => {
    const resp = await doFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });
    const body = await resp.json().catch(() => null);
    if (!resp.ok) throw new HttpError(resp.status, body);
    return body;
  });
}
