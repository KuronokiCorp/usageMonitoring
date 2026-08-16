/**
 * panel.js -- the control panel (spec docs/specs/chrome-extension-control-
 * panel.md, section 6). Runs both as the popup and as a full tab; same
 * codebase, same DOM ids, in panel.html.
 *
 * The POST allowlist for the whole extension (AC-42) is exactly the five
 * paths below -- every postJson() call in this file targets one of them,
 * built from these named constants so there is exactly one place that
 * spells each path out. The server's single-job creation endpoint is never
 * called (AC-37): job creation goes only through the bulk-create endpoint
 * (API_JOBS_CREATE_BULK below), even for a single job.
 */
import { getJson, postJson, HttpError, baseUrl, loadSettings, saveSettings } from "./api.js";

const API_SEND = "/api/send";
const API_JOBS_TOGGLE = "/api/jobs/toggle";
const API_JOBS_CREATE_BULK = "/api/jobs/create_bulk";
const API_JOBS_DELETE = "/api/jobs/delete";
const API_JOBS_RUN = "/api/jobs/run";

// Sessions polling must stay >= 10000ms and run only while the panel is
// visible AND the Sessions tab is the active in-page tab (spec 6.1 / AC-16
// / BACKLOG #11) -- /api/sessions costs one osascript + one ps PER SESSION.
const SESSIONS_POLL_MS = 10000;
const JOBS_POLL_MS = 30000; // spec 6.2
const LOG_POLL_MS = 5000; // spec 6.3
const ZERO_MATCH_STATUS = "MATCHED 0 SESSIONS — not delivered";

const $ = (id) => document.getElementById(id);

let SETTINGS = null;
let SESSIONS = [];
let activeTab = "sessions";
let lastFailure = null; // { kind: "forbidden"|"network"|"other", status, body }

// --------------------------------------------------------------------- //
// Settings / base URL
// --------------------------------------------------------------------- //
async function refreshSettings() {
  SETTINGS = await loadSettings();
  $("baseUrlDisplay").textContent = baseUrl(SETTINGS);
  $("openAdminLink").href = baseUrl(SETTINGS) + "/";
  $("settingsHost").value = SETTINGS.host;
  $("settingsPort").value = String(SETTINGS.port);
  $("settingsPollSeconds").value = String(SETTINGS.pollSeconds);
  $("settingsNotifyErrors").checked = !!SETTINGS.notifyErrors;
  $("settingsNotifyJobDrops").checked = !!SETTINGS.notifyJobDrops;
}

function base() {
  return baseUrl(SETTINGS);
}

// --------------------------------------------------------------------- //
// Visibility / active-tab gating (spec 6.1, AC-16)
// --------------------------------------------------------------------- //
function isPanelVisible() {
  return document.visibilityState === "visible" && !document.hidden;
}
function isTabActive(name) {
  return activeTab === name;
}

// --------------------------------------------------------------------- //
// Status pill + error banner (spec 6.6)
// --------------------------------------------------------------------- //
function setStatusPill(kind, text) {
  const pill = $("statusPill");
  pill.className = "pill " + kind;
  pill.textContent = text;
}

function hideBanner() {
  $("errorBanner").hidden = true;
}

function showForbiddenBanner() {
  const id = (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.id) || "<id>";
  const port = SETTINGS ? SETTINGS.port : "";
  $("errorBanner").hidden = false;
  $("errorBanner").className = "banner forbidden";
  $("errorBannerMessage").textContent =
    "itermon rejected this extension (HTTP 403). Its origin is not on the server's allowlist.";
  $("errorBannerCommand").hidden = false;
  $("errorBannerCommand").textContent =
    `python3 iterm_web.py --allow-origin chrome-extension://${id} --port ${port}`;
  $("errorBannerCopyBtn").hidden = false;
  setStatusPill("forbidden", "not allowlisted");
}

function showNetworkBanner() {
  $("errorBanner").hidden = false;
  $("errorBanner").className = "banner network";
  $("errorBannerMessage").textContent = `Can't reach ${base()}. Is itermon running? (npm start)`;
  $("errorBannerCommand").hidden = true;
  $("errorBannerCopyBtn").hidden = true;
  setStatusPill("unreachable", "unreachable");
}

function showOtherBanner(status, body) {
  $("errorBanner").hidden = false;
  $("errorBanner").className = "banner network";
  const errText = body && body.error ? `: ${body.error}` : "";
  $("errorBannerMessage").textContent = `Request failed (HTTP ${status})${errText}`;
  $("errorBannerCommand").hidden = true;
  $("errorBannerCopyBtn").hidden = true;
  setStatusPill("unreachable", `error ${status}`);
}

function markConnected() {
  hideBanner();
  setStatusPill("connected", "connected");
  lastFailure = null;
}

async function withErrorHandling(fn) {
  try {
    await fn();
    markConnected();
  } catch (err) {
    if (err instanceof HttpError && err.status === 403) {
      lastFailure = { kind: "forbidden" };
      showForbiddenBanner();
    } else if (err instanceof HttpError) {
      lastFailure = { kind: "other", status: err.status, body: err.body };
      showOtherBanner(err.status, err.body);
    } else {
      lastFailure = { kind: "network" };
      showNetworkBanner();
    }
  }
}

// --------------------------------------------------------------------- //
// Sessions tab
// --------------------------------------------------------------------- //
async function loadSessions() {
  await withErrorHandling(async () => {
    SESSIONS = await getJson(`${base()}/api/sessions`);
    renderSessions();
    renderSendTargetOptions();
    renderJobTargetCheckboxes();
  });
}

function renderSessions() {
  const tbody = $("sessionsTableBody");
  tbody.textContent = "";
  for (const s of SESSIONS) {
    const tr = document.createElement("tr");
    for (const val of [s.index, s.name, s.job || "", s.tty]) {
      const td = document.createElement("td");
      td.textContent = val;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

// --------------------------------------------------------------------- //
// Jobs tab
// --------------------------------------------------------------------- //
let JOBS = [];

async function loadJobs() {
  await withErrorHandling(async () => {
    JOBS = await getJson(`${base()}/api/jobs`);
    renderJobs();
  });
}

function renderJobs() {
  const tbody = $("jobsTableBody");
  tbody.textContent = "";
  for (const j of JOBS) {
    const tr = document.createElement("tr");
    const isZeroMatch = j.last_status === ZERO_MATCH_STATUS;
    if (isZeroMatch) tr.className = "error-row";
    const cells = [j.name, j.target, j.schedule, j.enabled ? "enabled" : "paused", j.next_run || "—", j.last_run || "—", j.last_status || "—"];
    for (const val of cells) {
      const td = document.createElement("td");
      td.textContent = val;
      tr.appendChild(td);
    }
    const actionsTd = document.createElement("td");
    actionsTd.appendChild(makeButton(j.enabled ? "pause" : "resume", () => toggleJob(j)));
    actionsTd.appendChild(makeButton("run", () => confirmRunJob(j)));
    actionsTd.appendChild(makeButton("delete", () => confirmDeleteJob(j)));
    tr.appendChild(actionsTd);
    tbody.appendChild(tr);
  }
}

function makeButton(label, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

// enable/disable is reversible and executes nothing -- no confirm gate
// needed (spec 6.2's table).
async function toggleJob(job) {
  await withErrorHandling(async () => {
    await postJson(`${base()}${API_JOBS_TOGGLE}`, { id: job.id });
    await loadJobs();
  });
}

// -- run-now: the most dangerous control in the panel (spec 6.2). Only the
// confirm dialog's Confirm button ever calls postJson(API_JOBS_RUN, ...) --
// this row handler ONLY opens the dialog (AC-40).
function confirmRunJob(job) {
  const targetName = resolveTargetName(job.target);
  openConfirm({
    title: `Run "${job.name}" now?`,
    lines: [
      `Target: ${job.target} (${targetName})`,
      `Command: ${job.command}`,
      `Enter pressed: ${job.submit ? "yes" : "no"}`,
    ],
    onConfirm: async () => {
      await withErrorHandling(async () => {
        const result = await postJson(`${base()}${API_JOBS_RUN}`, { id: job.id });
        const isFailure = result && (result.status === ZERO_MATCH_STATUS || result.sent === 0);
        showSendLikeResult($("sendResult"), result, isFailure);
        await loadJobs();
      });
    },
  });
}

// -- delete: irreversible on the server. Only the confirm dialog's Confirm
// button ever calls postJson(API_JOBS_DELETE, ...) (AC-40).
function confirmDeleteJob(job) {
  openConfirm({
    title: `Delete "${job.name}"?`,
    lines: [`This cannot be undone.`],
    onConfirm: async () => {
      await withErrorHandling(async () => {
        await postJson(`${base()}${API_JOBS_DELETE}`, { id: job.id });
        await loadJobs();
      });
    },
  });
}

function resolveTargetName(target) {
  for (const s of SESSIONS) {
    if (`index:${s.index}` === target) return s.name;
    if (s.index === target) return s.name;
  }
  return target;
}

// === JOB-TARGET BUILD REGION START (AC-38 / AC-39) ========================
// Everything in this region, down to the matching END marker, is "the
// create path" AC-38 talks about: every job TARGET value built here carries
// the literal prefix "index:", never "id:" (an id: target is BACKLOG #6's
// root cause -- a session UUID that rots the next time the pane it pointed
// at is recreated). tests/run_tests.py's G12 scopes its AC-38 assertion to
// exactly this marked region, so moving code in or out of it is the only
// way to change what that test checks -- it cannot be satisfied by
// accident from an unrelated part of the file (e.g. the Send tab's `{id:
// job.id}` POST bodies, which are a JS object key, not a target prefix, and
// live well outside this region).
//
// buildJobCreationPlan() is a PURE function (no DOM, no chrome.*) -- like
// notify_logic.js, it is drivable under plain node
// (tests/extension_job_plan_driver.mjs), which is how AC-39 ("select all"
// with N sessions produces exactly N index:-prefixed jobs) is verified
// without a browser.
export function buildJobCreationPlan({ sessions, checkedTargets, selectAll, name, command, schedule, submit }) {
  const targets = selectAll
    ? sessions.map((s) => ({ value: `index:${s.index}`, label: `${s.index}  ${s.job || ""}  ${s.name}` }))
    : checkedTargets;

  const multi = targets.length > 1;
  const jobs = targets.map((t) => ({
    name: multi ? `${name} — ${t.label.trim().slice(0, 24)}` : name,
    target: t.value,
    command,
    schedule,
    submit,
  }));
  return { name, command, schedule, targets, jobs };
}

function renderJobTargetCheckboxes() {
  const list = $("jobTargetList");
  const keep = new Set(
    [...list.querySelectorAll("input[type=checkbox]:checked")].map((cb) => cb.value)
  );
  list.textContent = "";
  for (const s of SESSIONS) {
    const value = `index:${s.index}`;
    const label = document.createElement("label");
    label.className = "checkrow";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = value;
    cb.dataset.label = `${s.index}  ${s.job || ""}  ${s.name}`;
    cb.checked = keep.has(value);
    const span = document.createElement("span");
    span.textContent = cb.dataset.label;
    label.appendChild(cb);
    label.appendChild(span);
    list.appendChild(label);
  }
}

function checkedJobTargets() {
  return [...$("jobTargetList").querySelectorAll("input[type=checkbox]:checked")].map((cb) => ({
    value: cb.value,
    label: cb.dataset.label || cb.value,
  }));
}

function planJobCreation() {
  const name = $("jobName").value.trim();
  const command = $("jobCommand").value;
  const schedule = $("jobSchedule").value.trim();
  const submit = $("jobSubmit").checked;
  const selectAll = $("jobSelectAll").checked;
  return buildJobCreationPlan({
    sessions: SESSIONS,
    checkedTargets: checkedJobTargets(),
    selectAll,
    name,
    command,
    schedule,
    submit,
  });
}
// === JOB-TARGET BUILD REGION END (AC-38 / AC-39) ==========================

function validateJobCreation(plan) {
  if (!plan.name) return "Job name is required.";
  if (!plan.targets.length) return "Tick at least one target session, or Select all.";
  if (!plan.command) return "Command is required.";
  if (!plan.schedule) return "Schedule is required.";
  return null;
}

function reviewJobCreation() {
  const err = $("jobCreateFieldError");
  err.hidden = true;
  const plan = planJobCreation();
  const problem = validateJobCreation(plan);
  if (problem) {
    err.textContent = problem;
    err.hidden = false;
    return;
  }
  openConfirm({
    title: `Create ${plan.jobs.length} job(s)?`,
    lines: [],
    reviewRows: plan.jobs.map((j) => `${j.name} → ${j.target} @ ${j.schedule}${j.submit ? " (Enter)" : ""}`),
    onConfirm: async () => {
      await withErrorHandling(async () => {
        try {
          await postJson(`${base()}${API_JOBS_CREATE_BULK}`, { jobs: plan.jobs });
        } catch (e) {
          if (e instanceof HttpError && e.body && e.body.error) {
            err.textContent = e.body.error;
            err.hidden = false;
            return;
          }
          throw e;
        }
        $("jobName").value = "";
        $("jobCommand").value = "";
        $("jobSelectAll").checked = false;
        renderJobTargetCheckboxes();
        await loadJobs();
      });
    },
  });
}

// --------------------------------------------------------------------- //
// Log tab
// --------------------------------------------------------------------- //
let LOGS = [];

async function loadLogs() {
  await withErrorHandling(async () => {
    LOGS = await getJson(`${base()}/api/logs`);
    renderLogs();
  });
}

function renderLogs() {
  const tbody = $("logTableBody");
  const filter = $("logKindFilter").value;
  tbody.textContent = "";
  // newest-first, last 200 entries (spec 6.3). Server returns oldest-first.
  const recent = LOGS.slice(-200).slice().reverse();
  for (const e of recent) {
    if (filter !== "all" && e.kind !== filter) continue;
    const tr = document.createElement("tr");
    if (e.kind === "error") tr.className = "error-row";
    for (const val of [e.t, e.kind, e.message]) {
      const td = document.createElement("td");
      td.textContent = val;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

// --------------------------------------------------------------------- //
// Send tab (spec 6.4)
// --------------------------------------------------------------------- //
function renderSendTargetOptions() {
  const select = $("sendTarget");
  const keep = select.value;
  select.textContent = "";
  for (const s of SESSIONS) {
    const opt = document.createElement("option");
    opt.value = s.index;
    opt.textContent = `${s.index} — ${s.name} (${s.job || ""})`;
    select.appendChild(opt);
  }
  if (keep) select.value = keep;
}

function showSendLikeResult(el, result, forcedFailure) {
  el.hidden = false;
  if (result && result.error) {
    el.className = "result err";
    el.textContent = "Error: " + result.error;
  } else if (forcedFailure || (result && result.matched === 0) || (result && result.status === ZERO_MATCH_STATUS)) {
    el.className = "result warning";
    el.textContent = "matched 0 sessions — nothing was sent";
  } else if (result && result.sent) {
    el.className = "result ok";
    const list = Array.isArray(result.sent)
      ? result.sent.map((h) => `  ${h.index}  ${h.tty || ""}  ${h.name || ""}`).join("\n")
      : String(result.sent);
    el.textContent = "Sent:\n" + list;
  } else {
    el.className = "result ok";
    el.textContent = JSON.stringify(result);
  }
}

function reviewSend() {
  const targetIndex = $("sendTarget").value;
  const command = $("sendCommand").value;
  const submit = $("sendSubmit").checked;
  if (!targetIndex || command === "") return;
  const session = SESSIONS.find((s) => s.index === targetIndex);
  const targetName = session ? session.name : targetIndex;
  openConfirm({
    title: "Send this command?",
    lines: [`Target: ${targetIndex} (${targetName})`, `Command: ${command}`, `Enter pressed: ${submit ? "yes" : "no"}`],
    onConfirm: async () => {
      await withErrorHandling(async () => {
        const result = await postJson(`${base()}${API_SEND}`, {
          target: targetIndex,
          command,
          submit,
        });
        showSendLikeResult($("sendResult"), result, false);
      });
    },
  });
}

// --------------------------------------------------------------------- //
// Settings tab (spec 7)
// --------------------------------------------------------------------- //
function renderServerSetup() {
  const id = (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.id) || "<extension-id>";
  const origin = `chrome-extension://${id}`;
  $("extensionOriginField").value = origin;
  const port = $("settingsPort").value || (SETTINGS ? SETTINGS.port : "");
  $("allowOriginCommand").textContent = `python3 iterm_web.py --allow-origin ${origin} --port ${port}`;
}

async function saveSettingsForm(evt) {
  if (evt) evt.preventDefault();
  const err = $("settingsFieldError");
  err.hidden = true;
  const portVal = Number($("settingsPort").value);
  if (!Number.isInteger(portVal) || portVal < 1 || portVal > 65535) {
    err.textContent = "Port must be an integer between 1 and 65535.";
    err.hidden = false;
    return;
  }
  const next = {
    host: $("settingsHost").value,
    port: portVal,
    pollSeconds: Number($("settingsPollSeconds").value),
    notifyErrors: $("settingsNotifyErrors").checked,
    notifyJobDrops: $("settingsNotifyJobDrops").checked,
  };
  await saveSettings(next);
  SETTINGS = next;
  $("baseUrlDisplay").textContent = base();
  $("openAdminLink").href = base() + "/";
  renderServerSetup();
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
    chrome.runtime.sendMessage({ type: "settings-saved" });
  }
  await loadActiveTabData();
}

async function testConnection() {
  const el = $("testConnectionResult");
  el.textContent = "testing…";
  try {
    await getJson(`${base()}/api/logs`);
    el.textContent = "connected";
  } catch (err) {
    if (err instanceof HttpError && err.status === 403) {
      el.textContent = "403 — not allowlisted yet";
    } else {
      el.textContent = "unreachable";
    }
  }
}

// --------------------------------------------------------------------- //
// Confirm dialog (spec 6.2 / 6.4, AC-40)
// --------------------------------------------------------------------- //
function openConfirm({ title, lines = [], reviewRows = null, onConfirm }) {
  $("confirmTitle").textContent = title;
  const body = $("confirmBody");
  body.textContent = "";
  for (const line of lines) {
    const p = document.createElement("p");
    p.textContent = line;
    body.appendChild(p);
  }
  const reviewList = $("confirmReviewList");
  reviewList.textContent = "";
  if (reviewRows) {
    const ul = document.createElement("ul");
    for (const row of reviewRows) {
      const li = document.createElement("li");
      li.textContent = row;
      ul.appendChild(li);
    }
    reviewList.appendChild(ul);
  }
  const dialog = $("confirmDialog");
  const yesBtn = $("confirmYesBtn");
  const noBtn = $("confirmNoBtn");
  const onYes = async () => {
    cleanup();
    dialog.close();
    await onConfirm();
  };
  const onNo = () => {
    cleanup();
    dialog.close();
  };
  function cleanup() {
    yesBtn.removeEventListener("click", onYes);
    noBtn.removeEventListener("click", onNo);
  }
  yesBtn.addEventListener("click", onYes);
  noBtn.addEventListener("click", onNo);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

// --------------------------------------------------------------------- //
// Tabs
// --------------------------------------------------------------------- //
function setActiveTab(name) {
  activeTab = name;
  for (const btn of document.querySelectorAll(".tabbtn")) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll(".tabpanel")) {
    panel.classList.toggle("active", panel.id === "tab" + capitalize(name));
  }
  loadActiveTabData();
}

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

async function loadActiveTabData() {
  if (activeTab === "sessions") await loadSessions();
  else if (activeTab === "jobs") await loadJobs();
  else if (activeTab === "log") await loadLogs();
  else if (activeTab === "send") await loadSessions();
}

// --------------------------------------------------------------------- //
// Wiring
// --------------------------------------------------------------------- //
function wireEvents() {
  for (const btn of document.querySelectorAll(".tabbtn")) {
    btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
  }
  $("refreshBtn").addEventListener("click", () => loadActiveTabData());
  $("errorBannerRetryBtn").addEventListener("click", async () => {
    if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
      chrome.runtime.sendMessage({ type: "retry" });
    }
    await loadActiveTabData();
  });
  $("errorBannerCopyBtn").addEventListener("click", () => copyText($("errorBannerCommand").textContent));

  $("jobCreateReviewBtn").addEventListener("click", reviewJobCreation);
  $("sendReviewBtn").addEventListener("click", reviewSend);

  $("logKindFilter").addEventListener("change", renderLogs);

  $("settingsForm").addEventListener("submit", saveSettingsForm);
  $("settingsPort").addEventListener("input", renderServerSetup);
  $("copyOriginBtn").addEventListener("click", () => copyText($("extensionOriginField").value));
  $("copyCommandBtn").addEventListener("click", () => copyText($("allowOriginCommand").textContent));
  $("testConnectionBtn").addEventListener("click", testConnection);

  $("openInTabLink").addEventListener("click", (evt) => {
    if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.getURL) {
      evt.preventDefault();
      chrome.tabs && chrome.tabs.create
        ? chrome.tabs.create({ url: chrome.runtime.getURL("panel.html") })
        : window.open(chrome.runtime.getURL("panel.html"), "_blank");
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (isPanelVisible()) loadActiveTabData();
  });

  setInterval(() => {
    if (isPanelVisible() && isTabActive("sessions")) loadSessions();
  }, SESSIONS_POLL_MS);
  setInterval(() => {
    if (isPanelVisible() && isTabActive("jobs")) loadJobs();
  }, JOBS_POLL_MS);
  setInterval(() => {
    if (isPanelVisible() && isTabActive("log")) loadLogs();
  }, LOG_POLL_MS);
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => {});
  }
}

async function init() {
  wireEvents();
  await refreshSettings();
  renderServerSetup();
  setActiveTab("sessions");
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
    chrome.runtime.sendMessage({ type: "ack" });
  }
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
}
