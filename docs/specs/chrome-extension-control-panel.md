# Spec — itermon Chrome extension: control panel + background notifier

**Status:** APPROVED FOR BUILD. **Stage 3 of the extension program (BACKLOG #13).**
**Author:** Messi (`usagemonitoring-product-manager`, v275001) · **Date:** 2026-08-16
**Origin:** CEO directive 2026-08-16 via Zidane — *"build this project chrome extension"*, i.e.
start the build of #13 now, on the answers already given: **Q1 = B** (full browser control panel,
built **on** the notifier core — "B done well contains A") and **Q2 = B** (Web Store *direction*
approved; the publish click is still the CEO's and **no store material is part of this build**).
**Implements:** BACKLOG #13. **Depends on:** BACKLOG #12 (`Origin`/`Host` allowlist, landed on
`develop` `c96783e`, shipped in npm 1.4.0) — specifically its `--allow-origin` escape hatch.
**Predecessor document:** `docs/specs/chrome-extension-scope-proposal.md` (scope, CORS findings,
the RCE that fell out of it). This spec supersedes its §4 "Notifier first" shape.

**Audience:** Gerrard (`usagemonitoring-developer`) builds it. Dida
(`usagemonitoring-tester`) verifies §10. Ayala (`code-reviewer`) reviews. I accept or request
changes. Each of you has a section addressed to you by name; the safety rules live **in** those
sections rather than in a dispatch, because a rule that has to be re-stated by hand in every
dispatch is a rule that will eventually be skipped (lesson of 2026-08-07).

> `docs/specs/` is deliberately public (BACKLOG #5 design call). Nothing in here is internal
> candour, and nothing in here is an exploit.

---

## 1. What we are building, in one paragraph

An **unpacked Manifest V3 Chrome extension**, living in a new top-level `extension/` directory in
this repo, that does two things. **(A) The notifier core:** an MV3 service worker woken by
`chrome.alarms` polls the two *cheap, in-memory* endpoints of the local itermon admin API —
`GET /api/logs` and `GET /api/jobs` — with **no tab open**, raises a `chrome.notifications`
desktop alert when an `kind:"error"` line appears or a scheduled job silently fails to fire, and
puts an unread count on the toolbar badge. **(B) The control panel:** a popup page (also openable
as a full tab) that lists sessions, lists jobs and enables/disables them, shows the activity log,
and sends a command to **one explicitly-chosen** session behind a confirm step. The extension
talks to the API from the service worker and extension page only — **no content scripts** — so it
is exempt from CORS and **requires zero server code changes**. The one server-side act it needs is
operational, not code: the operator restarts itermon with
`--allow-origin chrome-extension://<the extension's id>`, using the flag #12 already shipped.

**Scope line for #13:** *"itermon in your browser, that also tells you when it broke."*

### 1.1 Why the notifier is the core and not a bolt-on

This product's signature failure mode is **silence**. Two scheduled jobs matched zero sessions for
six days (BACKLOG #6/#7); nothing broke loudly, and what caught it was a human opening
`activity.log` by hand. A control panel that you have to *remember to open* does not fix that. The
alarms + notifications + badge half is the only part of this extension that does something a
localhost bookmark cannot, so it is built first and the panel is built on top of it. If a change
during the build forces a trade-off between the two halves, **the notifier wins**.

### 1.2 v1 feature set

> **AMENDED 2026-08-16 — CEO decision, R1 = B.** I recommended deferring job create/delete/run-now
> to v1.1; the CEO decided directly in session that they go into **v1, before the branch is cut**.
> My recommendation is on the record and the call is his. Folded in below and in §6.2; the write
> surface grows, so the guard discipline grows with it.

v1 delivers: sessions list · jobs list with enable/disable · **job create** · **job delete** ·
**job run-now** · activity-log view · send-to-one-session · settings · background notifier · badge.

**Still deferred to v1.1:** screen preview (`/api/read`). It was not part of the CEO's R1 answer,
it is the one remaining endpoint with no write consequence and no alerting value, and leaving it out
keeps the panel's API surface to exactly the endpoints the admin page's *control* functions use.

**Because the write surface is now the admin page's full job surface, every destructive or
executing action is confirm-gated** (§6.2): delete asks, run-now asks and shows exactly what will be
typed and where, and `__all__` is still not producible by any control in the extension.

---

## 2. Constraints inherited — non-negotiable, do not "improve" these

These are already recorded in BACKLOG #13 and they are acceptance criteria, not advice.

1. **No server code change.** `iterm_web.py`, `iterm_ctl.py`, `iterm_mcp.py`, `start.sh` are not
   touched by this branch. If you believe the extension needs a server change, **stop and raise it
   with me** — that is a spec bug, not a coding decision.
2. **`extension/` never ships on npm.** `package.json`'s `files` array is not edited; the published
   tarball stays the gate-certified **7-file** set. An extension cannot be installed from npm
   anyway, so shipping it there is pure risk for zero reach.
3. **No content scripts, no `tabs`, no `<all_urls>`.** Content scripts are the one extension
   component that *is* subject to CORS; the whole zero-server-change property depends on there
   being none.
4. **`/api/sessions` is touched only while the panel UI is open** — never on a background tick.
   That endpoint costs one `osascript` + one `ps` **per session**. BACKLOG #11 is about this
   product polling itself into swap exhaustion; a background poller that hits it would make #11
   worse, and this extension exists so the 5-second-polling admin tab can be **closed**.
5. **Write surface is guarded.** `submit` defaults **off**; `__all__` must not be reachable from
   any control in the UI; a send requires an explicit confirm step naming the target.
6. **The CEO's live server is untouchable by this build.** PID/port **8765** is a launchd
   `KeepAlive` process serving live Claude Code sessions. Nothing in this branch restarts it,
   reconfigures it, or sends anything to it. Getting the extension talking to *that* server needs a
   `--allow-origin` restart, which is a live-config act on the #6/#7 precedent — a separate,
   later, CEO-gated step, **explicitly not part of this build** (§8, §13 R2).

---

## 3. Directory layout

New top-level directory, no build step, no bundler, no npm dependency (this product is
zero-runtime-dep and stays that way; the extension adds **zero** dev dependencies too).

```
extension/
  manifest.json        MV3 manifest (§4)
  background.js        service worker: alarms, polling, notifications, badge (§5)
  notify_logic.js      PURE functions, no chrome.* calls — the whole decision layer (§5.5)
  api.js               fetch helpers, settings load/save, base-URL construction (§7)
  panel.html           the control panel: popup AND openable as a tab (§6)
  panel.js             panel behaviour
  panel.css            styling
  icons/
    icon16.png  icon32.png  icon48.png  icon128.png
  README.md            load-unpacked instructions + the --allow-origin step (§7.4)
```

Rules:
- `notify_logic.js` exports **pure** functions and imports nothing from `chrome.*`. This is what
  makes the notifier testable without a browser (§10.2). If a decision cannot be expressed as a
  pure function of (server data, previous state) → (notifications, next state), that is a design
  smell — raise it.
- `background.js` and `panel.js` are ES modules (`"type": "module"` in the manifest) so they can
  `import` `notify_logic.js` / `api.js`. No duplicated logic between worker and panel.
- **No inline JavaScript anywhere.** MV3's default CSP forbids inline `<script>` bodies and
  `onclick=` attributes; `panel.html` wires everything through `addEventListener` in `panel.js`.
- Icons: plain flat PNGs, generated however you like (`sips`/ImageMagick/hand-drawn). They must
  exist and be the declared sizes; nobody is grading the art.
- **Nothing else in the repo root changes** except `CHANGELOG.md` (§9, AC-6).

---

## 4. `manifest.json`

```json
{
  "manifest_version": 3,
  "name": "itermon control panel",
  "version": "0.1.0",
  "description": "Control your itermon terminal sessions and get desktop alerts when a scheduled send silently fails.",
  "permissions": ["alarms", "notifications", "storage"],
  "host_permissions": ["http://127.0.0.1/*", "http://localhost/*"],
  "background": { "service_worker": "background.js", "type": "module" },
  "action": {
    "default_popup": "panel.html",
    "default_title": "itermon",
    "default_icon": { "16": "icons/icon16.png", "32": "icons/icon32.png" }
  },
  "icons": { "16": "icons/icon16.png", "32": "icons/icon32.png", "48": "icons/icon48.png", "128": "icons/icon128.png" }
}
```

Notes that are part of the spec, not commentary:

- **`permissions` is exactly those three.** Not `activeTab`, not `tabs`, not `scripting`, not
  `webRequest`, not `background`. Every added permission is a line in the install prompt and, later,
  a justification owed to Google review.
- **`host_permissions` is exactly those two patterns.** Chrome match patterns have **no port
  component**, so `http://127.0.0.1/*` already covers whatever `--port` the user runs — this is why
  the port is a *stored setting*, not a permission concern (§7.1). `localhost` is included because
  users type it and because it is a distinct origin from `127.0.0.1`.
- **No `content_scripts` key at all.** Not an empty array — the key must be absent.
- **The extension's `version` is its own**, starting at `0.1.0`, and is **not** tied to the npm
  package version. Do not sync them; they release on different clocks.
- No `"key"` field in v1. The unpacked extension's ID is derived by Chrome from the absolute path
  of the loaded directory, so it is stable while the folder stays put and changes if it moves — the
  panel surfaces the live ID rather than assuming one (§7.3). Pinning an ID with a `"key"` is a
  Web-Store-stage decision (stage 4), not this build's.
- `minimum_chrome_version` is deliberately omitted; nothing here needs a very recent API.

---

## 5. The notifier core (the A-half)

### 5.1 Scheduling

- One alarm, name `itermon-poll`, created in `chrome.runtime.onInstalled` **and** in
  `chrome.runtime.onStartup` (a service worker is not persistent; the alarm is what wakes it).
- Default period **60 seconds** (`periodInMinutes: 1`). User-settable to **30 s / 1 min / 5 min /
  15 min** and nothing else — a free-text field invites a 1-second poller. Floor is
  `periodInMinutes: 0.5`; never use `setInterval`/`setTimeout` for polling (an MV3 worker is killed
  when idle and the timer dies with it).
- The tick handler is `chrome.alarms.onAlarm`. It must be safe to run concurrently with itself
  (a re-entrancy guard flag in memory is enough).

### 5.2 What a tick does — exactly

On each tick, if not paused (§5.6) and past any backoff deadline:

1. `GET {base}/api/logs`
2. `GET {base}/api/jobs`

**That is the whole network activity of a background tick: two GETs, both to endpoints that read
in-process memory / a small JSON file, and zero subprocess spawns on the server.** It must **never**
request `/api/sessions`, `/api/read`, or any `POST` endpoint. Requests use `AbortController` with a
**4000 ms** timeout so a hung server cannot pin the worker awake.

### 5.3 State

All persisted state lives in `chrome.storage.local` under two keys.

`settings` (§7.1):
```js
{ host: "127.0.0.1", port: 8765, pollSeconds: 60, notifyErrors: true, notifyJobDrops: true }
```

`state`:
```js
{
  schemaVersion: 1,
  lastLogKey: null,      // dedup anchor: `${t}\u0000${kind}\u0000${message}` of the newest processed entry
  lastLogT:   null,      // that entry's `t`, the fallback anchor if the ring buffer rolled
  jobs: {},              // { [jobId]: { last_run, next_run, dropReportedFor } }
  unread: 0,             // unacknowledged error count -> badge
  reachable: null,       // null = never contacted, true, false
  paused: false,         // §5.6
  pauseReason: null,     // null | "forbidden"
  backoffMs: 0           // current backoff, 0 when healthy
}
```

Settings and state are separate keys so that writing state on every tick cannot race a settings
save from the panel.

### 5.4 What raises a notification

**(a) New error log lines.** Entries from `/api/logs` with `kind === "error"` that have not been
seen before. This is what a 0-match send emits, from both the manual path and the scheduler
(`matched 0 sessions — NOT DELIVERED`), and it is the exact silence that bit us in #6/#7.

**(b) A job that did not fire at all.** Detected from `/api/jobs` alone by comparing this tick's
jobs against the previous tick's: for an **enabled** job, if the previously recorded `next_run` is
now more than a **120-second grace period** in the past and `last_run` is **unchanged**, the job
missed its slot. Report **once per missed slot** — record `dropReportedFor = <that next_run>` so
the same slot is never re-reported on subsequent ticks.

> The other silent-drop shape — the job *did* fire but matched zero sessions — is already covered
> by rule (a), because `run_job()` logs it as `kind:"error"`. Do not build a second detector for it.

**(c) Reachability transitions.** `reachable` going `true → false` ("admin server unreachable at
`{base}`") and `false → true` ("admin server back"). **On the transition only** — never repeated
while the state persists. `null → false` on the very first contact also notifies once, so a user
who installs the extension with the server down finds out.

**(d) Not allowlisted (HTTP 403).** §5.6. One notification, ever, per pause.

**Notification style.** `chrome.notifications.create` with `type: "basic"`, `iconUrl:
"icons/icon128.png"`. One tick produces **at most one** notification for rule (a) and at most one
for rule (b): if a tick yields several errors, collapse them —
title `itermon: 3 errors`, message = the newest message plus `+2 more`. Message text is truncated
to 180 characters. Notification IDs are deterministic (`itermon-error-<hash>`, `itermon-jobdrop-<id>`,
`itermon-conn`) so a re-created notification replaces rather than stacks.

**Clicking a notification** clears it and resets the badge counter to zero. It does **not** open a
tab: `chrome.tabs.create()` would work without the `tabs` permission, but v1 opens nothing on its
own — the user clicks the toolbar icon. Do not reach for `tabs`.

### 5.5 `notify_logic.js` — the pure decision layer

These functions contain the whole of §5.4 and must not call `chrome.*`, `fetch`, or `Date.now()`
implicitly (time is passed in). This is what Dida tests without a browser.

```js
export function logKey(entry)                          // `${t}\u0000${kind}\u0000${message}`
export function diffLogEntries(entries, state)         // -> { newEntries, nextLogAnchor }
export function detectJobDrops(jobs, prevJobsById, nowMs, graceMs)
                                                       // -> { drops, nextJobsById }
export function planNotifications(newEntries, drops, settings)
                                                       // -> [ {id, title, message}, ... ]
export function parseServerTime(s)                     // "YYYY-MM-DD HH:MM" -> epoch ms, local
```

`diffLogEntries` algorithm, specified because getting it wrong produces either a notification storm
or silence — both worse than no extension:

1. If `state.lastLogKey === null` (first ever poll): **seed and notify nothing.** Return
   `newEntries: []` and an anchor pointing at the newest entry. *Installing the extension must not
   fire a notification for every historical error in the buffer.*
2. Otherwise find the **last** index whose `logKey` equals `state.lastLogKey`; `newEntries` is
   everything after it.
3. If the anchor is **not found** (the server's 1000-line ring buffer rolled, or the server
   restarted), fall back to `entries.filter(e => e.t > state.lastLogT)` — the `t` format
   `YYYY-MM-DD HH:MM:SS` is lexicographically ordered, so string comparison is correct and
   timezone-free. Cap the result at **20** entries and let `planNotifications` collapse them.
4. The next anchor is always the newest entry present, even when `newEntries` is empty.

`parseServerTime` must **not** rely on `new Date(string)` parsing `"2026-08-16 14:30"` — split on
non-digits and use `new Date(y, m-1, d, hh, mm)`. Engine-dependent date parsing is not a thing to
gamble on.

### 5.6 Failure handling — the part that protects the server

- **Network failure / timeout:** set `reachable = false`, notify once (rule c), and back off —
  double `backoffMs` from the configured period up to a **5-minute** ceiling, re-creating the alarm
  at the longer period. On the next success, restore the configured period and notify recovery.
- **HTTP 403 — not allowlisted:** **stop polling entirely.** Set `paused = true`,
  `pauseReason = "forbidden"`, clear the `itermon-poll` alarm, raise **one** notification telling
  the user to restart itermon with `--allow-origin chrome-extension://<id>`, and set the badge to
  `!`. Polling resumes only when the user saves settings or presses **Retry** in the panel.
  **This is not a nicety.** Every rejected request makes the server write a rate-limited rejection
  line into `activity.log`, which has no size cap; a 60-second poller that keeps hammering a server
  that will not have it turns a configuration mistake into disk growth on a machine that has
  already hit 96 % disk.
- **Any other non-2xx (404, 500, …):** treat as a reachability failure for badge/notification
  purposes (one transition notification, then quiet) and back off as above. Never notify per tick.

### 5.7 Badge

- `unread > 0` → text = `String(Math.min(unread, 99))`, background `#c0392b`.
- `paused` → text `!`, background `#c0392b`.
- otherwise → text `""`.
- The panel sends `{type: "ack"}` on load; the worker zeroes `unread` and clears the badge.

### 5.8 The load claim, written so it can be tested

> **Claim:** with the admin tab closed, the extension's steady-state cost on the itermon server is
> **2 HTTP GETs per poll period against in-memory endpoints and zero subprocess spawns**. The admin
> page it replaces costs **12 `/api/sessions` requests per minute**, each spawning one `osascript`
> plus one `ps` **per session**. Therefore replacing one open admin tab with the extension is a
> strict reduction in load, and the extension is only a net win if the tab actually gets closed.

Mechanically checkable as AC-14, AC-15 and AC-33 — a static assertion that `/api/sessions` appears
nowhere in the background path, a request-log assertion that a background tick produces exactly
`{GET /api/logs, GET /api/jobs}` and nothing else, and a real-Chrome observation over ≥3 ticks with
the panel closed.

---

## 6. The control panel (the B-half)

`panel.html` is the `action.default_popup` **and** is openable as a full tab via an "Open in tab"
link (`chrome.runtime.getURL("panel.html")`) for when 800×600 is too small. One page, one
codebase, two presentations.

**Header (always visible):** a status pill — `connected` / `unreachable` / `not allowlisted` — the
current base URL, a **Refresh** button, and an `<a target="_blank">` link to the full admin page at
the configured base URL.

**Five in-page tabs** (plain buttons swapping a section's visibility; not Chrome tabs):

### 6.1 Sessions
`GET /api/sessions` → table of **index · name · job · tty**. Fetched when the Sessions tab becomes
active, then auto-refreshed every **10 seconds**, and **only** while
`document.visibilityState === "visible"` **and** the Sessions tab is the active in-page tab. Switch
to any other tab, or hide the window, and the `/api/sessions` polling stops. A **Refresh** button
covers the manual case. (The admin page polls this every 5 s with no `document.hidden` check; the
panel must not repeat that mistake — this is the §2.4 constraint made concrete.)

### 6.2 Jobs
`GET /api/jobs` → table of **name · target · schedule · enabled · next_run · last_run ·
last_status**, refreshed on tab activation and every 30 s while active. A job whose `last_status`
is the 0-match string is rendered in the error colour.

**Writes on this tab (amended per R1 = B).** Endpoints and payloads below were read off
`iterm_web.py` on `develop`, not assumed — the admin page's own JavaScript is the reference
implementation and the extension must not invent a different contract.

| Action | Request | Guard |
|---|---|---|
| enable / disable | `POST /api/jobs/toggle` `{id}` | none — reversible, executes nothing |
| run now | `POST /api/jobs/run` `{id}` | **confirm step** naming target, command, and whether Enter is pressed |
| delete | `POST /api/jobs/delete` `{id}` | **confirm step** naming the job |
| create | `POST /api/jobs/create_bulk` `{jobs: [...]}` | validation + explicit review of what will be created |

Each write is followed by a re-fetch of `/api/jobs`.

**Create — the rules that matter, and why.**

- **Use `/api/jobs/create_bulk` only.** `/api/jobs/create` (single) exists on the server but the
  admin page does not use it; one creation path means one set of behaviours to test. The string
  `/api/jobs/create` must not appear in `extension/` at all (AC-37) — note `create_bulk` contains
  it as a prefix, so implement that assertion as an exact API-path-set comparison, not a naive
  substring search.
- **Job targets are `index:<n>`, never `id:<uuid>`.** This is not style. `id:` is a session UUID
  that iTerm2 mints fresh on every session recreation, so an `id:`-targeted *scheduled* job rots the
  next time that pane is recreated — that is the literal root cause of BACKLOG #6/#7, the two jobs
  that delivered nothing for six days. `index:` is positional and survives recreation. (Manual sends
  are different and may keep `id:` — a manual send happens seconds after the list was fetched and
  has no window in which to rot.) AC-38 exists solely to keep this from regressing.
- **A "select all sessions" convenience is expanded client-side into one job per session**, exactly
  as the admin page does — `SESSIONS.map(s => ({target: 'index:' + s.index, ...}))` — producing
  individually manageable jobs rather than one opaque all-sessions job. **The literal `__all__` is
  never constructed, never sent, and must not appear anywhere in `extension/`** (AC-19 still binds).
  Where more than one job is created at once, disambiguate the names the way the admin does
  (`"<name> — <session label>"`).
- **Form fields:** name (required, non-empty), target(s) (checkbox list built only from the sessions
  array — no free text), command (required), schedule (5-field cron string, required), and a
  **`submit` checkbox that defaults off** with the same "this executes the command" labelling as the
  send form (AC-41).
- **Schedule validation is the server's.** A bad cron returns `400` with
  `{"error": "bad schedule: ..."}`; render that message verbatim next to the schedule field and
  create nothing. Do not reimplement cron parsing in the extension.
- **Review before submit:** show the exact list of jobs about to be created (name → target →
  schedule → submit yes/no) and create only when the user confirms.

**Run now — the most dangerous control in the panel.** It types into a live terminal immediately.
The confirm dialog must show the job's name, its resolved target, the exact command text, and
whether Enter will be pressed. Only Confirm issues the request. The response's `status` field is
rendered as-is, and the 0-match status string (`MATCHED 0 SESSIONS — not delivered`) is rendered as
a **failure**, not a success — same rule as §6.4.

**Delete** is irreversible on the server (the job is filtered out of the jobs file with no undo), so
it is confirm-gated and names the job being deleted.

### 6.3 Log
`GET /api/logs` → newest-first list of the last **200** entries, `t` · `kind` · `message`, with a
kind filter (all / error / send / job) and error rows highlighted. Auto-refresh every 5 s while
this tab is active and visible — this endpoint is cheap; that is the whole reason it may be polled
faster than sessions.

### 6.4 Send — the guarded write surface
- **Target:** a `<select>` whose options are built **only** from the sessions array, value =
  `session.index`, label = `index — name (job)`. **There is no free-text target input**, so
  `__all__` cannot be typed, pasted, or produced by any control. The string `__all__` must not
  appear anywhere in `extension/` (AC-19).
- **Command:** a `<textarea>`.
- **`submit` checkbox, unchecked by default**, labelled *"press Enter after typing (this executes
  the command)"*. It must have no `checked` attribute in the HTML and the request must send
  `submit: false` when unchecked.
- **Confirm step:** pressing Send opens an in-page confirmation showing the resolved target
  (index **and** name), the exact command text, and whether Enter will be pressed. Only the
  **Confirm** button issues `POST /api/send`.
- **Result:** render the response. `{"sent": [...]}` → success with the session list.
  `{"matched": 0}` → a prominent **warning**, not a success — a 0-match send is a failure and the
  panel must say so, exactly as the server's own log does.

### 6.5 Settings
See §7.

### 6.6 Panel error states
- **403 on any request:** replace the tab content with a red banner: *"itermon rejected this
  extension (HTTP 403). Its origin is not on the server's allowlist."* plus the copy-ready command
  (§7.3) and a **Retry** button. Distinct from, and never conflated with, "unreachable".
- **Network error:** amber banner — *"Can't reach {base}. Is itermon running? (`npm start`)"* with
  Retry.
- **Non-2xx other:** show the status code and the server's `error` field if present.

---

## 7. Configuration and the one-time server-side step

### 7.1 Settings
Stored in `chrome.storage.local` (machine-specific: a port on *this* machine — not
`storage.sync`).

| Setting | Default | Control | Validation |
|---|---|---|---|
| `host` | `127.0.0.1` | select: `127.0.0.1` \| `localhost` | fixed choices only |
| `port` | `8765` | number input | integer 1–65535; reject anything else with an inline message |
| `pollSeconds` | `60` | select: 30 / 60 / 300 / 900 | fixed choices only |
| `notifyErrors` | `true` | checkbox | — |
| `notifyJobDrops` | `true` | checkbox | — |

Base URL is built **once, in `api.js`**, as `` `http://${host}:${port}` ``. The literal `8765` may
appear in `extension/` **exactly once**, as the default in the settings declaration (AC-20) — the
lesson of #12's AC-10 is that a hard-coded port silently defeats every `--port` user.

Saving settings: persist, re-create the alarm at the new period, clear `paused`/`backoffMs`, and
run a poll immediately so the user gets feedback within a second rather than a minute.

### 7.2 Why `host_permissions` needs no port
Chrome match patterns have no port component. `http://127.0.0.1/*` grants every port on that host.
So a user running `--port 9000` changes **one setting** and nothing else — no manifest edit, no
reload, no new permission prompt. Put this sentence in `extension/README.md`; it is the kind of
thing a user will otherwise assume wrong.

### 7.3 Surfacing the extension's own ID — exact UX

The Settings tab contains a section headed **"Server setup (one time)"** with:

1. A read-only field labelled **"This extension's origin"** containing
   `chrome-extension://${chrome.runtime.id}` and a **Copy** button.
2. A read-only, monospaced, copy-ready command block:
   ```
   python3 iterm_web.py --allow-origin chrome-extension://<id> --port <port>
   ```
   with the live id and the configured port substituted, and its own **Copy** button.
3. This explanatory text, verbatim in substance:
   > itermon only accepts requests from origins it has been told about. Restart itermon with the
   > command above once. Scripts and `curl` are unaffected — this changes nothing else.
4. A warning line:
   > If you move or rename the extension folder, Chrome gives it a **new ID** and you must re-run
   > this command with the new value.
5. A **Test connection** button that issues `GET /api/logs` and reports one of: **connected** /
   **403 — not allowlisted yet** / **unreachable**.

The same "Server setup" block is what the 403 banner (§6.6) renders, so a user who never opened
Settings still gets the exact command at the moment it matters.

### 7.4 `extension/README.md`
Must contain, in this order: what the extension does; how to load unpacked
(`chrome://extensions` → Developer mode → Load unpacked → pick `extension/`); where to find the ID;
the `--allow-origin` command; the port setting and the no-port-in-match-patterns note; the
permission list with **one line of justification each**; and a plain statement that the extension
is **not** part of the npm package and is not on the Chrome Web Store.

---

## 8. Explicitly out of scope

Doing any of these makes the branch a REQUEST CHANGES, regardless of how good the code is.

1. **Any change to server code** — `iterm_web.py`, `iterm_ctl.py`, `iterm_mcp.py`, `start.sh`.
2. **Any change to a file inside `package.json`'s `files`** (`iterm_ctl.py`, `iterm_web.py`,
   `iterm_mcp.py`, `start.sh`, `README.md`, `LICENSE`) or to `package.json` itself — including a
   version bump, including adding `extension/` to `files`, including a README section about the
   extension. The published tarball stays **byte-identical**.
3. **Any Chrome Web Store material** — no developer account, no listing copy, no store icons/
   screenshots, no privacy policy, no `key` pinning, no packed `.crx`/`.zip`. Q2 = B authorised the
   *direction*; the materials are stage 4 and are **held**, and the store listing additionally
   depends on BACKLOG #5 landing.
4. **Any restart, reconfiguration, or use of the CEO's live server on port 8765** — including
   "just testing against it once". Every test uses a throwaway server on an ephemeral port.
5. **Any content script, any page injection, any permission beyond the three in §4.**
6. **Any npm dependency**, runtime or dev — no bundler, no test framework, no TypeScript build.
7. **Firefox/Safari ports**, `/api/read` screen preview, auth tokens. *(Job create/delete/run-now
   moved OUT of this list into v1 by the CEO's R1 = B decision, 2026-08-16 — see §1.2/§6.2.)*
8. **Any push to `origin`** (§12).

---

## 9. Acceptance criteria

Numbered continuously; **numbers are stable — the R1 = B amendment appends AC-37…AC-50 rather than
renumbering**, and supersedes AC-21 in place.

- **Suite tests** (new group **`G12`** in `tests/run_tests.py`): **AC-1 … AC-20, AC-22 … AC-24,
  AC-37 … AC-43**. *(AC-21 is superseded by AC-42 — do not implement it.)*
- **Counter-test mutations** (`tests/counter_test.py`): **AC-25 … AC-29 (M11–M15)** and
  **AC-44 … AC-46 (M16–M18)**.
- **Dida's real-Chrome verifications** (§10.3), evidenced in her worklog: **AC-30 … AC-36** and
  **AC-47 … AC-50**.

Gerrard writes the suite ACs alongside the code (they are structural and cheap). Dida owns the
counter-test and real-Chrome ACs and independently re-runs everything.

### Packaging and repo hygiene

- **AC-1 — the npm file list is unchanged.** `npm pack --dry-run --json` at the repo root yields
  exactly the 7 paths `LICENSE, README.md, iterm_ctl.py, iterm_mcp.py, iterm_web.py, package.json,
  start.sh`. **No path beginning `extension/` appears.** *(Guard the test with
  `shutil.which("npm")` **and** `os.path.exists(REPO_ROOT/"LICENSE")` — `counter_test.py` runs the
  suite against a partial copy of the tree, and an unguarded `npm pack` assertion would go red on
  the baseline for a reason unrelated to any mutation. A red baseline makes the whole counter-test
  harness meaningless.)*
- **AC-2 — `files` is untouched.** `package.json`'s `files` array equals
  `["iterm_ctl.py","iterm_web.py","iterm_mcp.py","start.sh","README.md","LICENSE"]`.
- **AC-3 — no version bump.** `package.json`'s `version` is unchanged by this branch.
- **AC-4 — shipped files are byte-identical.** For each of the six `files` entries plus
  `package.json`, the SHA-256 on the feature branch equals the SHA-256 on `develop` at the branch
  point. *(Implement as a test that shells `git diff --name-only <merge-base> HEAD` and asserts none
  of those seven paths appears; skip cleanly if not in a git work-tree.)*
- **AC-5 — no dependencies added.** `package.json` has no `dependencies` or `devDependencies` key,
  and there is no `extension/package.json`, no lockfile, no `node_modules/` committed.
- **AC-6 — CHANGELOG entry exists.** `CHANGELOG.md`'s `## [Unreleased]` section has an `### Added`
  entry naming the Chrome extension and stating explicitly that it is **not part of the npm
  package**.

### Manifest

- **AC-7 — MV3.** `extension/manifest.json` parses as JSON and `manifest_version === 3`.
- **AC-8 — permissions are exactly three.** `set(manifest["permissions"]) == {"alarms",
  "notifications", "storage"}` — equality, not containment.
- **AC-9 — host permissions are exactly two.** `manifest["host_permissions"] ==
  ["http://127.0.0.1/*", "http://localhost/*"]`.
- **AC-10 — no content scripts.** The key `content_scripts` is **absent** from the manifest (not
  present-and-empty).
- **AC-11 — no forbidden strings in the manifest.** None of `tabs`, `activeTab`, `scripting`,
  `webRequest`, `<all_urls>`, `*://*/*` appears anywhere in `manifest.json`.
- **AC-12 — every declared file exists.** `background.service_worker`, `action.default_popup`, and
  every path in `action.default_icon` and `icons` resolves to an existing file under `extension/`.
- **AC-13 — the extension version is independent.** `manifest["version"]` is a valid
  dot-separated version and is **not equal** to `package.json`'s version.

### The #11 constraint — the background path never touches `/api/sessions`

- **AC-14 — structural.** Neither `extension/background.js` nor `extension/notify_logic.js`
  contains the substring `/api/sessions`, and the set of API paths appearing in `background.js` is
  exactly `{"/api/logs", "/api/jobs"}`.
- **AC-15 — behavioural.** Driving one tick of the polling routine against a stub server that
  records every request (§10.2) produces exactly two requests, `GET /api/logs` and `GET /api/jobs`,
  in that order, and nothing else — no `POST`, no `/api/sessions`.
- **AC-16 — the panel gates its own `/api/sessions` polling.** `panel.js` contains a visibility
  check (`visibilityState` or `hidden`) and an active-tab check guarding the sessions refresh
  timer, and the sessions interval constant is **≥ 10000 ms**.

### The guarded write surface

- **AC-17 — `submit` defaults off.** The `submit` checkbox element in `panel.html` has **no**
  `checked` attribute, and `panel.js` sends `submit: false` when it is unchecked.
- **AC-18 — the target selector is not free text.** `panel.html` contains a `<select>` for the send
  target and **no** `<input type="text">`/`<textarea>` bound to the target field.
- **AC-19 — `__all__` is unreachable.** The string `__all__` does not appear in **any** file under
  `extension/`.
- **AC-20 — no stray hard-coded port.** The literal `8765` appears **at most once** across
  `extension/**`, and that occurrence is in the settings-defaults declaration.
- **AC-21 — SUPERSEDED by AC-42** (R1 = B widened the write surface). Do not implement AC-21.

### MV3 correctness and hygiene

- **AC-22 — no inline JS.** No file in `extension/` contains a `<script>` element with a non-empty
  body or an `on<event>=` attribute; MV3's CSP would silently break either.
- **AC-23 — no wall-clock timers for background polling.** `background.js` contains no
  `setInterval(` and no `setTimeout(` used for polling; scheduling goes through `chrome.alarms`.
  *(A `setTimeout` inside a fetch-timeout helper is fine; assert on `background.js` not containing
  `setInterval(` at all, and that `chrome.alarms.create` is present.)*
- **AC-24 — suite integrity.** `python3 tests/run_tests.py --twice` is green and byte-identical
  across both runs, and `python3 tests/counter_test.py` exits 0, both on the **merged** tree, not
  just on the feature branch (retro lesson L5).

### Counter-test (non-vacuity — a gate, not a nicety)

`tests/counter_test.py`'s `_fresh_copy()` must be extended to copy the `extension/` tree (same
pattern as the read-only `package.json`/`README.md`/`CHANGELOG.md` additions made for G11). Each
mutation must make the suite **fail**, and the harness must name the specific test that caught it.

- **AC-25 (M11) — neuter the permission set.** Add `"tabs"` to `manifest.json`'s `permissions` →
  AC-8 goes red.
- **AC-26 (M12) — neuter the #11 guard.** Insert a `/api/sessions` fetch into `background.js` →
  AC-14 goes red.
- **AC-27 (M13) — neuter the `__all__` guard.** Insert the literal `__all__` into `panel.js` →
  AC-19 goes red.
- **AC-28 (M14) — neuter the submit default.** Add `checked` to the submit checkbox in
  `panel.html` → AC-17 goes red.
- **AC-29 (M15) — neuter the packaging guard.** Add `"extension"` to `package.json`'s `files` →
  AC-2 goes red.

### Real-Chrome verification (Dida, §10.3)

- **AC-30 — loads clean.** The extension loads unpacked with **zero errors and zero warnings** on
  `chrome://extensions`, and the service worker registers.
- **AC-31 — the 403 path is real and legible.** Against a throwaway server started **without**
  `--allow-origin`, the panel shows the *not allowlisted* banner (not "unreachable"), the banner
  contains the exact `--allow-origin chrome-extension://<id>` command, the background poller
  **pauses** (badge `!`, exactly one notification), and no further requests are made — confirmed by
  the server's own log showing no growth over ≥2 poll periods.
- **AC-32 — allowlisting fixes it with no code change.** Restarting the same throwaway server with
  `--allow-origin chrome-extension://<id>` and pressing **Retry** makes the panel connect and the
  poller resume. **No file in the repo is edited between AC-31 and AC-32.**
- **AC-33 — the notifier works with no tab open.** With the panel **closed** and no admin tab open,
  writing a `kind:"error"` line into the throwaway server's log produces a desktop notification
  within one poll period and a badge count of 1; **and** over ≥3 consecutive ticks the server
  receives only `/api/logs` and `/api/jobs` requests — **zero** `/api/sessions`.
- **AC-34 — no install-time notification storm.** Loading the extension fresh against a server
  whose log **already contains** error lines produces **zero** notifications on the first tick.
- **AC-35 — the panel works end to end.** Sessions, Jobs, Log tabs render from the fake backend;
  toggling a job flips `enabled` in the throwaway jobs file and back.
- **AC-36 — a send is guarded and correct.** With `submit` unchecked, sending to one session
  produces exactly one `send_text` call to that one session in the fake `osascript` record **with
  no trailing Enter**, and zero sends to any other session. Repeating with `submit` checked
  produces the Enter. **The load-bearing assertion is what the fake `osascript` recorded, not the
  HTTP status code** — a status-only assertion does not satisfy AC-36. (Three of this product's
  worst defects were fixtures that made the right and wrong implementations indistinguishable; do
  not add a fourth.)

### Job write surface — added by the R1 = B amendment (2026-08-16)

Endpoints and payloads below were read off `iterm_web.py` on `develop`, not assumed.

Suite tests, same group `G12`:

- **AC-37 — one creation path.** The set of API paths reached by `panel.js` includes
  `/api/jobs/create_bulk` and **does not include `/api/jobs/create`**. Assert by exact path-set
  comparison, **not** substring containment — `/api/jobs/create` is a prefix of
  `/api/jobs/create_bulk`, so a naive `in` check is guaranteed to be wrong in one direction or the
  other.
- **AC-38 — scheduled-job targets are `index:`, never `id:`.** Every job-target value constructed in
  the create path is built from the literal prefix `index:`, and the prefix `id:` appears in no
  job-creation code path. *(Manual send may still use an `id:`-form target; scope the assertion to
  the create path, and write it so that moving the manual-send code around cannot silently satisfy
  it.)* This is BACKLOG #6's root cause — an `id:` is a session UUID iTerm2 re-mints on every
  session recreation, so an `id:`-targeted scheduled job rots. It does not get to come back.
- **AC-39 — "select all" expands client-side.** Given N sessions, the "select all" control produces
  a `create_bulk` body whose `jobs` array has **exactly N entries**, one per session, each with an
  `index:`-prefixed target, and no entry whose target is the literal `__all__` (AC-19 already
  forbids that string existing anywhere in `extension/`).
- **AC-40 — destructive/executing job actions are confirm-gated.** `POST /api/jobs/delete` and
  `POST /api/jobs/run` are issued **only** from a confirm handler: `panel.html` declares a confirm
  dialog element, and no code path reaches either endpoint directly from a list-row click handler.
- **AC-41 — job-create `submit` defaults off.** The job-create `submit` checkbox has no `checked`
  attribute in `panel.html`, and the created job carries `submit: false` when it is unchecked.
- **AC-42 — the v1 POST allowlist (replaces AC-21).** The set of API paths reached by any `POST`
  anywhere in `extension/` is **exactly**
  `{"/api/send", "/api/jobs/toggle", "/api/jobs/create_bulk", "/api/jobs/delete", "/api/jobs/run"}`
  — no `/api/jobs/create`, no `/api/read`.
- **AC-43 — the background worker still writes nothing.** `background.js` issues no `POST` at all
  (no `method:` other than an explicit `"GET"`, or no `method` key), and none of the five write
  paths appears in it. The panel's write surface growing must not leak into the half that runs
  unattended.

Counter-test mutations (`tests/counter_test.py`, M16–M18):

- **AC-44 (M16)** — change the create path's target prefix from `index:` to `id:` → **AC-38 red**.
- **AC-45 (M17)** — call `/api/jobs/delete` straight from the row handler, bypassing the confirm
  gate → **AC-40 red**.
- **AC-46 (M18)** — switch job creation to `/api/jobs/create` → **AC-37 and AC-42 red**.

Dida's real-Chrome verifications (§10.3), throwaway server only:

- **AC-47 — create works and validates.** Creating a job from the panel writes **exactly one** entry
  into the *throwaway* `iterm_jobs.json`, with an `index:`-prefixed target, `enabled: true`, and
  `submit: false` when the box is unchecked. Repeating with a **bad cron** returns 400, the server's
  `bad schedule:` message is shown verbatim next to the field, and **no** job is written.
- **AC-48 — "select all" creates one job per session.** With 3 fake sessions, "select all" produces
  **exactly 3** jobs, one per session, every target `index:`-prefixed, names disambiguated, and
  **zero** jobs with any other target form.
- **AC-49 — delete is confirm-gated and real.** Cancel → the job is still in the throwaway jobs
  file, unchanged. Confirm → it is gone, the list refreshes, and nothing else in the file changed.
- **AC-50 — run-now is confirm-gated and executes correctly.** Cancel → the fake `osascript` records
  **zero** sends. Confirm → **exactly one** send, to that job's target only, with Enter present iff
  the job's `submit` is true. A job whose target matches nothing renders the
  `MATCHED 0 SESSIONS — not delivered` status as a **failure**, not a success. **As with AC-36 the
  load-bearing assertion is the fake `osascript` record, not the HTTP status code.**

---

## 10. Test plan — Dida

### 10.1 Where the tests go
New class `G12ChromeExtension` in `tests/run_tests.py`, registered in the suite list next to
`G11OriginHostGuard`. Reuse the existing harness (`hermetic_env`, `write_tmp`,
`_web_admin_server`, the fake `osascript`) — **extend it; do not invent a second one.** New
mutations M11–M15 in `tests/counter_test.py` with `_fresh_copy()` extended to include `extension/`.

### 10.2 Testing JavaScript without a browser
`node` is available (the package already requires `node >= 16`) and the decision layer is pure by
construction (§5.5). Add `tests/extension_logic_driver.mjs`: it imports
`extension/notify_logic.js`, runs fixture-driven assertions with `node:assert`, prints a one-line
JSON summary, and exits non-zero on failure. A single Python test in G12 shells out to
`node tests/extension_logic_driver.mjs` and asserts exit 0, surfacing the driver's stderr on
failure. If `node` is not on `PATH`, the test **skips with an explicit message** — it must never
pass silently.

Logic cases the driver must cover (all pure, no browser, no network):

| # | Case | Expected |
|---|---|---|
| L1 | First poll, log already contains 3 error lines | 0 notifications; anchor seeded to newest |
| L2 | One new `kind:"error"` line since the anchor | exactly 1 notification |
| L3 | The same tick's data polled twice | 1 notification total, not 2 |
| L4 | Anchor missing (buffer rolled), 50 newer entries by `t` | ≤ 20 processed, collapsed into 1 notification |
| L5 | Enabled job, previous `next_run` 5 min past, `last_run` unchanged | exactly 1 drop notification |
| L6 | Same missed slot on the next tick | 0 further notifications (`dropReportedFor`) |
| L7 | Disabled job in the same situation | 0 notifications |
| L8 | Job fired (`last_run` advanced) | 0 drop notifications |
| L9 | 5 new error lines in one tick | 1 collapsed notification whose title names 5 |
| L10 | `parseServerTime("2026-08-16 14:30")` | correct local epoch ms; no `NaN` |

For AC-15 the driver (or a small Python-side stub server on an ephemeral port) records every
request the polling routine makes; the polling routine must therefore be callable with an injected
`fetch`, so keep the `fetch` boundary parameterisable.

### 10.3 Real-Chrome verification — safety rules, non-negotiable

These are yours to enforce, Dida; they are written here so they do not depend on any dispatch
repeating them.

1. **Throwaway copy of the extension.** Copy `extension/` to a scratch directory
   (`mktemp -d`) and load **that** unpacked — never the repo tree. Chrome's unpacked ID derives
   from the absolute path, so your ID will differ from anyone else's; that is expected and is
   exactly what AC-31/AC-32 exercise.
2. **Throwaway server, ephemeral port.** Copy `iterm_web.py` + `iterm_ctl.py` into a **second**
   scratch directory and run the server from there with `--port 0`-style ephemeral selection (or a
   picked high port you have verified is free). Because `JOBS_FILE`/`ACTIVITY_FILE` are derived
   from the script's own directory, running from the copy means the jobs file and activity log are
   created **in scratch** — the operator's real `iterm_jobs.json` and `activity.log` are never
   opened. Verify that by checking their mtimes before and after.
3. **NEVER port 8765, never the live process.** No request of any kind — not even a `GET /` — to
   the CEO's live server. Do not restart it, do not add a flag to it, do not `kill` it. If a test
   seems to need it, it is the wrong test; stop and tell me.
4. **Fake `osascript` on `PATH`** for every run that could reach a send, using the existing fake
   from the suite. **No real iTerm2 session is ever a target.** Fake sessions only.
5. **Never the CEO's real jobs.** The throwaway jobs file is one you wrote.
6. **Clean up before you finish** (CLAUDE.md end-of-session rules): remove the extension from
   `chrome://extensions`, kill the throwaway server, delete both scratch trees and the fake
   `osascript`, remove any `__pycache__`, and leave no tmux server behind. Never touch the real
   `activity.log` or `iterm_jobs.json`.
7. **Evidence in your worklog:** the extension ID you used, the ephemeral port, the exact
   `--allow-origin` command, and for each of AC-30…AC-36 what you observed — the notification text,
   the badge state, the server's request log, and the fake `osascript` record for AC-36.

### 10.4 What a PASS means
Every suite AC (AC-1…AC-20, AC-22…AC-24, AC-37…AC-43) and every counter-test mutation
(AC-25…AC-29, AC-44…AC-46) green from a clean checkout of the feature branch **and** from the trial
merge into `develop`; `run_tests.py --twice` byte-identical; `counter_test.py` exit 0 with all
**eight** mutations M11–M18 caught and each named by its catching test; and AC-30…AC-36 plus
AC-47…AC-50 observed and evidenced in your worklog. AC-21 is superseded and is not run. Anything
short of that is a FAIL with the specific AC named — do not hand me a qualified pass.

---

## 11. Notes for Ayala (code review)

Beyond the normal standard, the three things I most want a second pair of eyes on:

0. **The job write surface (§6.2), added late by the CEO's R1 = B decision.** It is the newest code
   in the branch and the only part that can delete a job or type into a terminal on a schedule.
   Two things specifically: that `index:` is used for every created job target (AC-38 — an `id:`
   here is BACKLOG #6 all over again), and that the confirm gate on delete/run-now is a real gate,
   not a dialog that some code path can skip.
1. **That the structural ACs are not vacuous.** AC-14, AC-19, AC-37, AC-42 are path/substring
   assertions.
   Substring assertions are exactly the kind of test that passes against a subtly wrong
   implementation — check that the counter-test mutations M11–M15 really are the ones that would
   catch a plausible regression, and say so if they are theatre.
2. **The 403 pause (§5.6).** This is the one behaviour whose absence would be invisible in normal
   use and harmful over days — a poller that keeps hitting a server that rejects it grows
   `activity.log` on a disk-constrained machine. Confirm the pause is real, that it survives a
   service-worker restart (it is persisted state, not an in-memory flag), and that it can only be
   cleared deliberately.
3. **The `diffLogEntries` fallback (§5.5 step 3).** Storm-vs-silence lives here. Both failure
   directions are bad and neither shows up in a happy-path test.

---

## 12. Branch, chain, and what must not happen

- **Branch:** `feature/chrome-extension`, cut from `develop`. Never commit code straight to
  `develop` or `main`.
- **Chain:** Gerrard builds → **Dida PASS** → **Ayala APPROVE** → **my ACCEPT** → I merge to
  `develop`. Ayala is the code-review gate, not me; any override of it is recorded in writing.
- **No push to `origin` in this stage.** Records/scrub state (BACKLOG #5) is still awaiting a CEO
  decision, so code merges stay **local**, exactly as stage 1 did. Pushing needs separate word.
- **Getting the spec onto the branch:** this document is committed on `main` (rule 12, records land
  on `main`). Gerrard brings it onto the feature branch with
  `git checkout main -- docs/specs/chrome-extension-control-panel.md` in his first commit, so the
  branch carries the spec its tests cite — the same pattern the origin-hardening spec followed.
- **The live tree stays on `develop`.** launchd execs `./start.sh` from this working tree, so the
  checked-out branch *is* the CEO's live configuration. Do all branch work in a `git worktree`
  under `../Products_usageMonitoring_worktrees/`. **Do not `git checkout` in the live tree.**
- **Definition of done:** merged to local `develop` with the chain complete, `extension/` present,
  the npm tarball provably unchanged, and my ACCEPT recorded. Web Store work does **not** start.

---

## 13. Risks I am flagging now, before the build

- **R1 — CLOSED 2026-08-16: the CEO answered B, directly in session.** I recommended deferring job
  create/delete/run-now to v1.1; he decided they go into **v1, before the branch is cut**. My
  recommendation is on the record and the call is his. Folded into §1.2, §6.2, §8 and AC-37…AC-50.
  The residual risk I am carrying forward rather than closing with it: this is now the admin page's
  **full job write surface** driven from an extension, so the guard discipline (confirm gates on
  delete and run-now, `index:`-only job targets, `__all__` unreachable, `submit` off by default) is
  the thing that has to hold. It is where I want Ayala to look first (§11) and it is where a
  regression would be most expensive.
- **R2 — the extension cannot talk to the CEO's live server until that server is restarted with
  `--allow-origin`.** That restart is a live-config act on a launchd `KeepAlive` process serving
  live agent sessions (#6/#7 precedent) and is **not** in this build. So the deliverable of this
  stage is a fully working, fully tested extension that talks to a *throwaway* server. Making it
  useful on the CEO's own machine is a separate, small, gated step I will bring up as its own
  question when the build passes.
- **R3 — unpacked extension IDs are path-derived.** Move the folder, get a new ID, and the
  allowlist entry stops matching with a 403. Mitigated by §7.3's warning text and by the 403 banner
  carrying the correct current command, but it is real friction and it is the strongest practical
  argument for eventually pinning a `"key"` — a stage-4 decision, not this build's.
- **R4 — platform policy drift.** Chrome keeps tightening access to loopback/private addresses.
  An extension with explicit `host_permissions` is today's sanctioned route and is not the target
  of that work, but "our integration depends on Google not changing its mind" is a durability cost
  a CLI does not otherwise carry. Recorded so nobody rediscovers it as a surprise.
- **R5 — a second poller on a machine that has hit swap exhaustion.** The design makes the
  extension strictly cheaper than the tab it replaces (§5.8), but only if the tab actually gets
  closed. If it ships and people keep the admin tab open anyway, it has not helped #11 — worth
  measuring after it is in use rather than assuming.
