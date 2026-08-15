# Spec — Admin API origin hardening (BACKLOG #12)

**Author:** Messi (`usagemonitoring-product-manager`, v275001) · **Date:** 2026-08-15
**Item:** BACKLOG #12 — *"Any web page the user visits can execute arbitrary commands in every one
of their terminal sessions."* Ranked **top of the queue**, above #5.
**Chain:** Messi (this spec) → Gerrard (implement) → Dida (verify) → Ayala (review) → Messi ACCEPT + merge.
**Branch:** `feature/admin-api-origin-hardening`, cut from **`develop`**.
**Blocks:** BACKLOG #13 (Chrome extension) — no new client is pointed at this API until this lands.
**Release:** rides the unreleased 1.4.0 line on `develop`. **No publish in this spec** (rule 6).

---

## 0. Read this before you read anything else

Two constraints that override any convenience during this work:

1. **The checked-out branch of the repo at
   `06_Products/Products_usageMonitoring/` IS the CEO's live configuration.** launchd
   (`co.vectech.itermon`, `KeepAlive`) execs `./start.sh` from that working tree. **Do not
   `git checkout` in it.** Cut and work the feature branch in a `git worktree`.
2. **Never test against the live server.** The live admin was PID 41512 on port 8765 when this
   spec was written. Every test in this spec runs against an **in-process throwaway
   `ThreadingHTTPServer` on an OS-assigned ephemeral port** with a **fake `osascript` on `PATH`**
   and `JOBS_FILE` / `ACTIVITY_FILE` monkeypatched to a `tempfile.mkdtemp()` path. That harness
   already exists — `tests/run_tests.py`, class `G10ZeroMatchIsAFailure`, `_running_server()`,
   which already asserts `port != 8765`. Extend it; do not invent a second one.

---

## 1. The problem, precisely

`iterm_web.py` on `develop` (the live tree) serves an HTTP API on loopback with **no `Origin`
check, no `Host` check, and no authentication**. Verified by reading the file, not inferred:

- `Handler._read_body()` (line ~260) reads `Content-Length` and calls `json.loads`. **It never
  inspects `Content-Type`.**
- `Handler.do_POST()` (line ~296) routes `/api/send` → `do_send(target, command, submit)` →
  `iterm_ctl.send_text(s, command, enter=True)` for every resolved session. Target `__all__`
  resolves to **every session**.
- `grep -n "Origin"` over the file returns exactly one hit, in the admin page's own JavaScript.
  No server-side check exists.

### The attack

Because the body is parsed regardless of `Content-Type`, an attacker page can send the request as
`text/plain`, which makes it a **CORS *simple request*** — **no preflight**:

```js
fetch('http://127.0.0.1:8765/api/send', {
  method: 'POST', mode: 'no-cors', headers: {'Content-Type': 'text/plain'},
  body: '{"target":"__all__","command":"<anything>","submit":true}' })
```

The attacker cannot read the opaque response and does not need to: by the time the response is
discarded, the command has been **typed and Entered in every session**. Loopback binding is no
defence — the request originates on the victim's own machine. Port 8765 is our documented default
and is not on Chrome's blocked-port list.

### Why the severity is higher here than the textbook case

- The server is launchd `KeepAlive` — **always listening**, not "running while in use".
- The targets are Claude Code panes with tool permissions, and `submit:true` is precisely how this
  product presses Enter on an approval prompt. BACKLOG #7 already records a nudge auto-approving
  an `rm -f` in another product's tree. Blast radius is not one shell.
- It ships to **every npm user** who runs `npm start`, under a README line
  (`README.md:253`) that reads *"Bound to `127.0.0.1` only (local, no auth)"* as though that were
  reassurance. It is the disclosure.
- No `Host` check also leaves **DNS rebinding** open, which upgrades the attacker from *blind
  writes* to *reading replies* — session list, job list, and `/api/read` screen contents.

---

## 2. The fix, and why it is this fix and not an auth token

**`Origin` allowlist + `Host` allowlist. Both fail-closed. No token.**

The reflex fix is an auth token. **I am ruling it out, and the reason is a compatibility fact, not
a preference.** `/api/send` carries a comment in the shipped source stating that *"scripts posting
here depend on"* its 200 status and its `sent` key. itermon is a published npm package; adding a
required credential would break every existing user's `curl` and every script written against it —
a **major** version bump on a security patch, which is the worst possible shape to force on users.

The asymmetry that makes the chosen fix work:

| Client | Sends `Origin` on a cross-origin POST? | Effect of this fix |
|---|---|---|
| Any browser, incl. `no-cors` simple requests and `<form>` POSTs | **Always** | **Rejected** — drive-by dead |
| `curl`, `wget`, `requests`, `urllib`, shell scripts | **No `Origin` header at all** | **Untouched** — byte-identical behaviour |

Cost to existing users: **zero**. That asymmetry is the entire reason for this design.

### Robust-either-way property (deliberate)

The admin page is served from the same origin it calls. Per the Fetch standard, a same-origin
**POST** *does* carry `Origin`. If that turns out to be wrong in some browser, the admin page still
works — because "no `Origin` header" is also an allow. The design is correct under both readings;
Dida still verifies the real browser path (AC-B1) rather than resting on the standard.

---

## 3. Non-goals and honest limits — state these, do not oversell the fix

Write these into the README change (AC-D1). A security fix that is described as more than it is
becomes the next disclosure.

- **This does not authenticate anything.** Any **local process** — other software on the machine,
  a malicious dependency in any other project — can still POST to the API with no `Origin` and be
  obeyed. This fix defends against **browser drive-by and DNS rebinding only**.
- **An XSS in the admin page itself bypasses it entirely** (same origin). Out of scope.
- **`__all__` remains reachable** from a no-`Origin` client. Rejecting `__all__` writes from
  browser origins is BACKLOG #12's noted optional follow-on; it is **not** in this spec.
- **No CORS response headers are added.** We are not making the API cross-origin-callable; we are
  refusing cross-origin callers. `Access-Control-Allow-Origin` must not appear anywhere.
- **`Content-Type` is deliberately NOT validated.** Requiring `application/json` would also kill
  the simple-request path — and it would break `curl -d '…'`, which sends
  `application/x-www-form-urlencoded`. Do not "harden" this as a bonus; it is a breaking change
  wearing a security costume.

---

## 4. Design

All of it lives in `iterm_web.py`. No other shipped file changes behaviour.

### 4.1 Allowlist derivation

```python
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1", "[::1]")
EXTRA_ALLOWED_ORIGINS: list[str] = []   # populated by main() from --allow-origin
```

The handler derives the allowlist from **the address the server is actually bound to**
(`self.server.server_address[:2]`), never from a hard-coded `8765`. This is required, not stylistic:
the test harness binds an ephemeral port, and users run `--port`.

- `bound_host` in `("0.0.0.0", "::", "")` (wildcard bind) → use `LOOPBACK_NAMES` only.
- `bound_host` a concrete non-loopback address → `LOOPBACK_NAMES` **plus** that address.
- **Allowed origins** = `{f"http://{h}:{port}" for h in names}` ∪ `EXTRA_ALLOWED_ORIGINS`.
  Also accept the port-less form when `port == 80`.
- **Allowed Hosts** = `{h, f"{h}:{port}" for h in names}` ∪ the host part of each
  `EXTRA_ALLOWED_ORIGINS` entry. Compare **case-insensitively**.

### 4.2 The guard

One guard, applied to **every** request method — not pasted into each handler:

| Condition | Verdict |
|---|---|
| `Host` header **absent** | **REJECT 403** (fail-closed; every browser and every mainstream HTTP client sends `Host`, incl. `curl --http1.0`) |
| `Host` not in allowed Hosts | **REJECT 403** (kills DNS rebinding) |
| `Origin` header absent | **ALLOW** (curl / scripts / top-level navigation) |
| `Origin` == `"null"` | **REJECT 403** (sandboxed iframe, `file://` — attacker-controllable) |
| `Origin` in allowed origins | **ALLOW** |
| `Origin` anything else | **REJECT 403** |

Checks run **before** the body is read and **before** any routing — a rejected request must not
reach `_read_body()`, `do_send()`, `load_jobs()`, `list_sessions()`, or any subprocess.

Rejection response: **HTTP 403**, JSON body `{"error": "forbidden"}`. Do **not** echo the
attacker's `Origin`/`Host` back in the response body.

**Structural requirement — the guard must not be skippable by a future handler.** Implement it so
that adding a `do_DELETE`/`do_PUT` later cannot bypass it (e.g. by overriding a single method that
all verbs funnel through, or by an explicit dispatch table). AC-8 tests this by **enumerating**
`do_*` methods at runtime, so the test does not need updating when a verb is added.

### 4.3 Rejection logging — rate-limited (this one is load-bearing)

Log rejections via `log_event("error", …)` so the operator can *see* a drive-by attempt in the
Activity panel. **But `log_event()` appends to `activity.log` on disk with no cap**, so an attacker
page firing in a loop would fill the disk — on a machine that has already hit 96 % disk (#7/#11)
that is a denial of service handed over with the fix. Therefore:

- Log the **first** rejection immediately.
- Then **at most one rejection entry per 60 seconds**, and that entry must state the number of
  rejections suppressed since the last one (e.g. `… (+37 more suppressed)`).
- The window is a module-level constant so the test can shrink it. Never a `sleep` in a test.

### 4.4 `--allow-origin` escape hatch

`main()` gains a repeatable `--allow-origin ORIGIN` argument appending to `EXTRA_ALLOWED_ORIGINS`.
Additive, opt-in, default empty — the default stays fail-closed. It exists so that:
- an operator binding to a LAN address and browsing by hostname is not locked out by a security
  patch (the one plausible behaviour change this fix causes), and
- BACKLOG #13's extension has a supported home for `chrome-extension://<id>` **without another
  code change**.

Validation: reject at startup (clear error, non-zero exit) anything that is not a bare scheme +
host [+ port] — no path, no trailing slash, no `*`. **A literal `*` must be refused**, so nobody
can turn the allowlist off by accident.

### 4.5 Documentation (ships)

- `README.md:253` — replace *"Bound to `127.0.0.1` only (local, no auth)"* with an accurate
  statement: loopback-bound, **no authentication**, protected against browser drive-by and DNS
  rebinding by `Origin`/`Host` allowlisting, **and still obeys any local process**. Document
  `--allow-origin`.
- `CHANGELOG.md` — under `## [Unreleased]`, a `### Security` entry describing the hole, the fix,
  the explicit "scripts and `curl` are unaffected" promise, and `--allow-origin`.

---

## 5. Acceptance criteria

Every AC is a test in `tests/run_tests.py` unless marked otherwise. New group **G11**.

### Security — the fix works

- **AC-1 — the exploit is closed.** A `POST /api/send` with `Content-Type: text/plain`,
  `Origin: https://evil.example`, body `{"target":"__all__","command":"echo pwned","submit":true}`,
  against the throwaway server with a **fake listing of 3 sessions**, returns **403** — **and the
  fake `osascript` records ZERO send operations.** *The status-code assertion alone does not
  satisfy AC-1.* The load-bearing assertion is that **nothing was typed**. (This product's three
  worst defects were all "the test passes whether or not the code is right"; do not add a fourth.)
- **AC-2 — `Origin: null` is rejected.** 403, nothing sent.
- **AC-3 — DNS rebinding is closed.** `GET /api/sessions` with `Host: evil.example:<port>` and
  **no `Origin`** → **403**, and `list_sessions()` is never called (no `osascript` spawned).
  Repeat for `/api/logs`, `/api/jobs`, and `POST /api/read`.
- **AC-4 — missing `Host` is rejected.** Raw socket, HTTP/1.0 request line, no `Host` → 403.
- **AC-5 — the guard runs before any work.** With a bad `Origin`, a request to
  `/api/jobs/create` produces **no job**, no write to the temp `JOBS_FILE`, and no subprocess.

### Non-regression — existing users are untouched

- **AC-6 — no `Origin` = unchanged.** `POST /api/send` with **no `Origin` header** (exactly what
  `curl` sends), 1 matching session: **200**, payload has key `sent` with one entry, activity-log
  kind `send`. Byte-identical to pre-fix behaviour.
- **AC-7 — the 0-match contract still holds.** No `Origin`, 0 matches: **200**,
  `{"sent": [], "matched": 0}`, log kind `error`. G10's AC-7 must still pass **unmodified** — if
  an existing test needs editing to stay green, stop and raise it with me; that is a regression,
  not a test-maintenance chore.
- **AC-8 — every verb is guarded.** Enumerate `Handler`'s `do_*` methods at runtime; for each,
  assert a request with `Origin: https://evil.example` yields 403. Must not need editing when a
  verb is added.
- **AC-9 — same-origin is allowed.** `POST /api/send` with `Origin: http://127.0.0.1:<bound port>`
  → 200 and delivered. Same for `http://localhost:<bound port>`.
- **AC-10 — the allowlist follows the bound port.** On an ephemeral port `P`,
  `Origin: http://127.0.0.1:P` is allowed and `Origin: http://127.0.0.1:8765` is **rejected**.
  This is the test that fails if anyone hard-codes 8765.
- **AC-11 — `--allow-origin` works and is validated.** An origin passed in is accepted; `*` and a
  value with a path are refused at startup with a non-zero exit.

### Operational

- **AC-12 — rejection logging is rate-limited.** With the window constant shrunk, N rapid
  rejections produce **exactly 2** activity-log entries, the second naming the suppressed count.
  No `sleep`.
- **AC-13 — no new per-request cost (#11 must not get worse).** The guard performs **no subprocess
  spawn, no file I/O, and no network call**. Assert structurally (rejected requests spawn nothing)
  — not by timing.
- **AC-14 — packaging unchanged.** `npm pack` yields the **same 7-file set** as 1.3.0
  (`package.json` + the 6 in `files`). Only `iterm_web.py`'s and `README.md`'s size change. No new
  file ships; `package.json`'s `files` array is **not** edited. **No version bump in this branch.**
- **AC-15 — suite integrity.** `python3 tests/run_tests.py --twice` green (byte-identical both
  runs) and `python3 tests/counter_test.py` green, both on the **merged** tree, not just the branch
  (retro L5).

### Counter-test (non-vacuity — this is a gate, not a nicety)

- **AC-16 — new mutation M9.** Add to `tests/counter_test.py` a mutation that **removes/neuters the
  `Origin` check**, and prove it is **caught** (red). Add **M10** for the `Host` check. Strengthen
  the fixture if a mutation survives; **never weaken the mutation** to make it pass.

### Documentation

- **AC-D1 — README.** `README.md:253`'s claim replaced per §4.5, including the honest limits from
  §3 (local processes are still obeyed).
- **AC-D2 — CHANGELOG.** `### Security` entry under `## [Unreleased]`.

---

## 6. Verification — Dida, and the safety rules that bind you

**PASS requires an independently re-run proof that the exploit is closed, not a code read.**

1. **Do not touch the live server, the CEO's `iterm_jobs.json`, the repo-root `activity.log`, or
   port 8765.** Work in a **copied tree / worktree**, with the G10-style harness: ephemeral port,
   fake `osascript` on `PATH`, `JOBS_FILE`/`ACTIVITY_FILE` in `tempfile.mkdtemp()`. If you find
   yourself needing the live server to prove something, stop and tell me.
2. **Prove the exploit reproduction is real (the part I care most about).** On a **throwaway copy**
   of the tree, revert the guard and re-run AC-1 — it must go **red, with the command actually
   delivered to all 3 fake sessions**. A test that would pass against the vulnerable code proves
   nothing. Record both directions in your report.
3. **AC-B1 — one real-browser exercise** (BACKLOG #10's finding: admin-page behaviour is
   browser-exercised, never code-read). Against **your throwaway server on its own port**, with the
   fake `osascript` in place:
   - (a) Load the admin page, send to one session from the UI → **works** (this is the same-origin
     `Origin` path, and the one thing this fix could plausibly break for real users).
   - (b) Serve a one-file attacker page from a **second** local server on a **different port**
     (`python3 -m http.server`), containing the §1 `fetch()` snippet, open it in the browser →
     **403, nothing delivered.** Capture the fake `osascript` log as the evidence.
   - Kill both servers and remove the attacker page afterwards (cleanup rule).
4. Report against every AC by number, with the two counter-test mutations shown red.

## 7. Review — Ayala

Focus, in priority order:
1. **Is the guard actually unskippable?** Try to find a request path that reaches `_read_body()` or
   `do_send()` without passing it. AC-8 is a test; you are the one who decides it is *structurally*
   true.
2. **Is any existing client broken?** Specifically hunt for anything that makes a no-`Origin`
   request behave differently from pre-fix — that is the one thing this change is not allowed to do.
3. **Vacuity.** Three of this product's defects were fixtures that made right and wrong
   indistinguishable. Assume AC-1 is vacuous until you have made it fail yourself.
4. Rate-limit correctness under `ThreadingHTTPServer` (concurrent requests, shared counter, lock).

## 8. Packaging, semver, release

- **No version bump on this branch.** It rides the unreleased 1.4.0 line already on `develop`.
- `package.json` `files` unchanged. Published file set stays the gate-certified 7 files.
- Semver when 1.4.0 is eventually cut: **MINOR**. New rejection behaviour exists, but every
  documented client contract (`/api/send` → 200 + `sent`) is preserved, so it is not major.
- **Publishing is CEO-gated (rule 6) and is not part of this work.**

## 9. Going live on the CEO's machine — plan, do not improvise

The fix only protects the CEO once his launchd-managed server restarts onto the new code. That is a
**live-configuration act** with the #6/#7 precedent: `develop` is the live branch, so merging this
to `develop` changes what a crash/reboot restarts onto, and a deliberate restart is what makes it
effective immediately. **Nobody restarts it as part of this spec.** After ACCEPT I write the
restart step up separately, with the current PID recorded before and after, and the 6 live jobs
re-verified enabled afterwards (they are enabled by an explicit, informed CEO decision — do not
"helpfully" re-pause them).
