# Scope proposal — Chrome extension for itermon

**Status:** PROPOSAL / spec draft. **Not approved, not started, no code written.**
**Author:** Messi (`usagemonitoring-product-manager`, v275001) · **Date:** 2026-08-15
**Origin:** CEO directive 2026-08-15, in full: *"itermon also need chrome extension handle it."*
Scope and intent were left to me. This document is my interpretation, the constraints I
verified against the code, and the two questions I am sending up.

> Written knowing `docs/specs/` is **deliberately public** under item #5's design call.
> No internal candour that does not belong in front of a stranger.

---

## 1. What the extension would actually be for

The honest starting point: **the web admin already does everything.** `iterm_web.py` serves a
single page at `127.0.0.1:8765` with sessions list, send-a-command, preview-screen, full job
CRUD, and a live activity log. A popup that shows sessions and sends text is a **worse copy of
a page that already exists in the same browser, one bookmark away.** If that were the whole
idea, my answer would be "not worth building."

There is exactly one thing a Manifest V3 extension can do that a localhost tab cannot:

**Run when no tab is open, and interrupt the user.**
An MV3 service worker woken by `chrome.alarms` can poll `/api/logs` and `/api/sessions`, raise a
`chrome.notifications` desktop alert, and put a count on the toolbar badge — with the admin page
closed and the browser minimised.

That maps onto this product's **signature failure mode, which is silence.** Two scheduled jobs
matched zero sessions for **six days** (BACKLOG #6/#7). Nothing broke loudly; the tool reported
"sent to 0 sessions" into a log nobody was reading, and what caught it was a human opening
`activity.log` by hand, late at night. A monitoring product whose own failures are only
discoverable by manually reading its log file has the wrong shape. The extension is the cheapest
place to fix that, because the alerting surface has to live *outside* the thing being monitored.

So the value is **not** "itermon in your browser." It is **"itermon tells you when it broke."**

### What I explicitly propose to leave out, and why

- **Job creation / editing.** Full CRUD in a popup duplicates the admin for no gain and doubles
  the surface that can misfire into a live terminal. The admin page is one click away from the
  popup; link to it.
- **Content scripts, any page injection.** Not needed for anything here, and they are the one
  extension component that *is* subject to CORS (see §2). Zero content scripts keeps the server
  unchanged and the permission ask minimal.
- **Anything that runs when Chrome is closed.** An MV3 worker does not. If the CEO wants alerting
  that survives a closed browser, the right build is a native notifier in `iterm_web.py`
  (`osascript -e 'display notification'`), not an extension — worth saying out loud, because it
  is a legitimate alternative to this entire proposal and it needs no browser at all.

---

## 2. Constraints — verified against the code, not assumed

Read of `iterm_web.py` @ `develop` (the live tree), 2026-08-15.

| Constraint | Finding |
|---|---|
| **Bind address** | `--host` default `127.0.0.1` (`iterm_web.py` `main()`). Loopback-only by default. Good, and the extension does not change it. |
| **CORS** | **The server sends no CORS headers at all**, and needs none — see below. |
| **Mixed content** | `chrome-extension://` is a secure context, but `http://127.0.0.1` is a *potentially trustworthy origin* per W3C, so it is **not** blocked as mixed content. Plain HTTP to loopback is fine; no TLS needed. |
| **Port is user-configurable** | `--port` exists, so a hardcoded `8765` would break custom-port users. Chrome **match patterns have no port component** — `http://127.0.0.1/*` covers every port on loopback in one permission. No options page needed for this. |
| **MV3 background** | Service worker, not a persistent page; it is killed when idle. All polling must go through `chrome.alarms` (period granularity ~30–60 s), never `setInterval`. |
| **Auth** | **None.** No token, no session, no `Origin` check, no `Host` check. Anything that can open a TCP connection to the port has full control. |

### The CORS answer, stated precisely because it decides the shape

In MV3, fetches made from **extension pages and the extension service worker** to a host listed
in `host_permissions` are **exempt from CORS** — the server does not have to return
`Access-Control-Allow-Origin`. **Content scripts are not exempt** (since Chrome 85 they are
subject to normal CORS).

**Consequence, and it is the single most useful finding in this document:** if every request is
made from the service worker / popup and there are **no content scripts**, the extension needs
**zero changes to `iterm_web.py`** to talk to the API. No CORS headers, no new endpoint, no
version bump forced on existing users, **no risk to the published npm package at all.** That is
why §1 rules out content scripts on principle rather than on taste.

### Watch item, flagged not hidden

Chrome has been progressively tightening access to loopback/private addresses (Private Network
Access). Today, an extension with explicit `host_permissions` for `127.0.0.1` is the *sanctioned*
route and is not the target of that work — but it is a moving platform policy, and "our
integration depends on Google not changing its mind" is a real durability cost that a CLI tool
does not otherwise carry. Recorded so a future session does not rediscover it as a surprise.

---

## 3. 🔴 The blocker this request uncovered — a live drive-by RCE in the admin API

I did not go looking for this. It fell out of asking "what would an extension have to
authenticate with?", and the answer is "nothing, because nothing does."

**`Handler._read_body()` reads `Content-Length` and calls `json.loads`. It never checks
`Content-Type`. There is no `Origin` check, no `Host` check, and no auth anywhere in the file.**

A `POST` with `Content-Type: text/plain` is a **CORS *simple request*** — the browser sends it
with **no preflight**. So any web page, in any tab, on a machine running the admin can do:

```js
fetch('http://127.0.0.1:8765/api/send', {
  method: 'POST', mode: 'no-cors',
  headers: {'Content-Type': 'text/plain'},
  body: '{"target":"__all__","command":"<anything>","submit":true}'
})
```

The attacker cannot *read* the reply (it is opaque) — but they do not need to. By the time the
response is discarded, `do_send()` has already resolved `__all__` to **every session** and
`send_text(..., enter=True)` plus the `submit` Enter has **typed and executed the command in
every terminal the user has open.** `8765` is not on Chrome's blocked-port list, and it is the
documented default.

**This is arbitrary command execution on the developer's machine, triggered by visiting a web
page.** Severity is amplified by the two facts specific to this deployment:

1. The CEO's server is **launchd `KeepAlive`** — it is not "running while you use it", it is
   **always listening**, and it was listening while I wrote this (PID 41512, port 8765).
2. Those terminals hold live Claude Code agent sessions with tool permissions. `submit:true` is
   how this product presses Enter on an approval prompt. The blast radius is not one shell.

It also ships to **every npm user** who runs `npm start`, and the README's "Bound to `127.0.0.1`
only (local, no auth)" reads as reassurance when it is the opposite.

### The fix, and why it costs existing users nothing

The instinct is "add an auth token" — **which would be a breaking change**, and the code itself
says so: the `/api/send` handler carries a comment that *"scripts posting here depend on"* the
200 status and the `sent` key. A mandatory token breaks every `curl` a user has already written.

The correct minimal fix is an **`Origin` allowlist plus a `Host` check**:

- **Browsers always attach `Origin` to a cross-origin POST**, including `no-cors` simple
  requests. Reject any request whose `Origin` is present and not allowlisted → **the drive-by is
  dead.**
- **`curl` and scripts send no `Origin` header at all** → **they are untouched.** Full backward
  compatibility, which is the constraint I actually care about for a published package.
- Allowlist = `http://127.0.0.1:<port>`, `http://localhost:<port>`, and later
  `chrome-extension://<id>`.
- A **`Host` header check** against the same set closes DNS rebinding (a rebound name would
  otherwise sail past the loopback bind).

Small, additive, testable, and it is the same mechanism the extension needs in order to be
*authorised* rather than merely *tolerated*. **This is now BACKLOG #12 and I have ranked it above
everything, including #5.** Sequencing call I am making as PM: **no second client gets pointed at
this API until #12 lands.** Adding an always-on background poller to an API that any web page can
already command is the wrong order of operations.

---

## 4. Recommended shape — "Notifier first"

**MV3 extension, in-repo at `extension/`, load-unpacked, read-mostly, no server change.**

**Permissions (the whole ask):** `alarms`, `notifications`, `storage`;
`host_permissions: ["http://127.0.0.1/*"]`. No `tabs`, no content scripts, no `<all_urls>`.

**Behaviour**

- Service worker on a `chrome.alarms` tick (default **60 s**, user-settable, never below 30 s):
  `GET /api/logs` and `GET /api/jobs`.
- **Notify on:** a `kind:"error"` log line (this is what `matched 0 sessions — NOT DELIVERED`
  emits), a job whose `next_run` has passed with no corresponding fire, and admin
  server unreachable → reachable transitions.
- **Badge:** count of unacknowledged errors. Click → popup.
- **Popup:** session list (index / name / job), per-job enabled + next-run, last ~20 log lines,
  and a link to the full admin page. **Read-only in v1** except for the one control below.
- **The single write action:** *Send* to one explicitly-selected session, reusing `POST /api/send`
  — behind a confirm step, with `submit` defaulting to **off**. Justification: an alert that says
  "job X delivered nothing" is worth much more if you can act without hunting for the tab. It is
  one click, on one named target, never `__all__`. **`__all__` is not reachable from the
  extension at all** — the popup has no control that can produce it.
- **Backoff:** on connection failure, back off to 5 min and stop notifying until recovery. The
  extension must not become the thing that keeps the machine busy.

**Deliberate interaction with BACKLOG #11 (self-inflicted polling load).** The admin page polls
`/api/sessions` every **5 s** with no `document.hidden` check — ~120 subprocess spawns/minute per
open tab. A naive extension is a *second* poller and makes that worse on a machine that hit swap
exhaustion on 08-14. Two rules fall out, and they are acceptance criteria, not advice:
1. The extension polls `/api/logs` and `/api/jobs` (cheap, in-process reads) on its normal tick.
   It touches **`/api/sessions` — the expensive one, one `osascript` + one `ps` per session —
   only when the popup is actually open.**
2. Net effect must be **negative** load: the extension exists so the admin tab can be *closed*.
   If it ships and people keep the tab open anyway, it failed.

**Packaging:** `extension/` is **excluded from `package.json` `files`.** The published tarball
stays byte-identical to the 7-file / 27.4 kB set that has now been gate-certified twice. An
extension cannot be installed from npm anyway, so shipping it there is pure risk for zero reach.

**Cost estimate:** S–M for the extension (a manifest, a service worker, a popup — no build step,
no bundler, no dependency, consistent with this product's zero-runtime-dep stance). #12 is S.

---

## 5. Alternatives I considered and rejected

| Option | Why not |
|---|---|
| **Full remote-control extension** (job CRUD, `__all__` send, screen preview) | Duplicates the admin, and widens the surface of an API with a live RCE hole. Wrong direction until #12 lands, and probably wrong after. |
| **Bookmark / PWA the admin page** | Free, but cannot notify with the tab closed — which is the entire value. |
| **Native macOS notification from `iterm_web.py`** | Genuinely competitive: no browser, no store, no Google policy risk, works when Chrome is closed. Weaker cross-platform story now that tmux/Linux is supported (1.3.0), and it is not what the CEO asked for. **Kept on the table as option C in the scope question below** — it deserves to be, and I would rather put it in front of him than quietly discard it. |
| **Firefox/Safari too** | Not now. One browser, one user, zero evidenced demand. |

---

## 6. Questions for the CEO (rule 16)

Both are genuinely CEO-reserved: one is direction, one is an outward act. Everything else in this
document is my call and I have made it.

### Q1 — What shape should the Chrome extension take?

- **A. Notifier first (RECOMMENDED).** Background alerts on job failure / silent-drop + toolbar
  badge + read-only popup + one guarded single-session send. Small, safe, fixes the "failures are
  silent" problem that has already bitten twice.
- **B. Full browser control panel.** Everything the admin page does, in the popup. Bigger, mostly
  duplicates what exists, and widens the write surface.
- **C. Skip the extension; build native macOS notifications into the server instead.** Same alerting
  benefit, no browser, no store, no platform-policy risk — but not a Chrome extension, and weaker
  now that itermon runs on Linux/tmux.

### Q2 — Publish to the Chrome Web Store, or keep it local?

- **A. Load-unpacked / local only (RECOMMENDED).** Lives in `extension/`, the user enables
  Developer Mode and points Chrome at the folder. **Not an outward act — nothing public, no
  account, no review, reversible.** Fits where we honestly are: 0 evidenced external users, the
  distribution test (#0b) not yet run, and item #5 still holding every push, so the public repo
  still says 1.2.0 while npm serves 1.3.0. Cost: no auto-update, and Developer Mode is friction.
- **B. Publish to the Chrome Web Store.** Real install flow, auto-update, discoverable. But it is
  an **outward act**: $5 developer account, Google review, a public privacy-policy/listing that
  ties VecTech's name to the product, and a permissions justification for `127.0.0.1` access.
  It would also be the *second* thing we ever put in public before we have told a single human
  the product exists, pointing reviewers at a repo a version behind npm.
- **C. Defer the decision until the extension exists and #0b's evidence bar (3 real users) is
  met.** Build under A, revisit the store with evidence.

---

## 7. Ranking (my call, recorded here; BACKLOG is authoritative)

1. **#12 — admin API `Origin`/`Host` hardening.** Displaces #5 at the top. It is a live remote
   code execution path on the CEO's always-on server; #5 is a disclosure problem, serious but
   bounded and 12 sessions old. #12 is also **not blocked by #5's no-push hold** — it is a code
   change to `develop`, which is where the live tree already is — so it can actually move now.
2. **#5** — records split (unchanged; still gates pushes and #0b).
3. **#7** — nudge hardening (safety predicate, durable identity, N-miss warning).
4. **#13 — this extension.** Blocked by #12 by my sequencing call.

I did **not** rank the extension above existing work. The CEO asked for it without a deadline,
it has one real benefit, and it is a new client for an API that currently has a hole in it.
