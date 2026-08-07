# Spec — stable scheduled-job targets + 0-match as a visible failure

**Author:** Messi (usagemonitoring-product-manager, v275001)
**Date:** 2026-08-08
**Drives:** BACKLOG #6
**CEO decision it implements (2026-08-08, via Zidane):** retarget the 6 live cron jobs from
`id:<UUID>` to a stable selector (`name:` / `index:`) so they survive session recreation.
**Branch:** `feature/stable-job-targets` cut from `develop`.
**Version impact:** MINOR (additive selector + new failure surfacing). No breaking change. Do NOT
bump the version on this branch — Lampard owns versioning at release prep.

---

## 1. Background — what actually broke

All 6 jobs in `iterm_jobs.json` carry a hard-coded `id:<iTerm2 session UUID>`. iTerm2 mints a new
UUID whenever a session is recreated, so since **2026-07-19 12:00** every fire has logged
`sent to 0 session(s): no match` — ~240 no-ops over 20 days, while the admin page kept showing a
fresh `last_run` and `enabled: true`.

Two independent defects, and the spec fixes both:

1. **The targets rot.** They are minted as `id:` by the admin UI itself
   (`createJob()` expands "ALL sessions" to `'id:'+s.id`), which guarantees rot on the next
   iTerm2 restart.
2. **The rot is invisible.** A send that matches **0** sessions is reported exactly like a
   successful one: log kind `send`, `last_status: "sent to 0 session(s)"`. This is the third
   instance in this product of *a wrong result that is byte-identical to a legitimately empty
   one* (3a's anchored fixtures; the 1.3.0 `LC_ALL`/SEP bug). It is why this survived three weeks.

## 2. Selector reality — read this before implementing

Established from the live machine on 2026-08-08, not assumed:

| selector | survives session recreation? | notes |
|---|---|---|
| `id:` | **No** — new UUID every time | current state; fails to 0 matches (dead but safe) |
| `tty:` | **No** — new `/dev/ttysNNN` | same rot as `id:` |
| `name:` | **No, for this product's users** | Claude Code rewrites the tab title per task ("✳ Rebuild p…" → "⠂ Keep working on next steps"); and two live sessions are both literally named `-zsh`, so one `name:` job would deliver **twice** |
| index (`1.1.1`) | **Yes, positionally** | a session recreated in the same window/tab/pane keeps its index |

Index has its own honest weakness, and it is the mirror image of `id:`'s: **`id:` fails to
nothing, index can match the wrong neighbour.** Evidence: on the last delivering fire
(2026-07-19 12:00) job `…— 1.1.4 …` resolved to a session then sitting at index **1.1.3** — the
panes had already shifted by one. So index targeting keeps delivering across a layout change, but
possibly to a different session than the operator picked.

**For this particular job set that weakness does not bite**: all 6 jobs carry the *identical*
command and were created by one "ALL sessions" bulk registration, so the intent is "nudge every
session once per 3 h". Six distinct index selectors over six live sessions reproduce that intent
exactly, with no duplicate delivery. This is why index — not `name:` — is the retarget choice.
Record the weakness in the docs; do not pretend index is a durable identity.

## 3. Scope

### S1 — `index:` prefix support (`iterm_ctl.py::resolve_targets`)

Today a bare `1.1.1` resolves by index, but the explicit form the CEO and the docs both use,
`index:1.1.1`, does **not**: it falls through every prefix branch to the bare-substring name
fallback, matches nothing, and returns 0 sessions **silently** — the exact bug class this spec
exists to kill.

- Add a branch, placed with the other prefixes and **before** the bare exact/substring fallbacks:
  `index:<value>` matches sessions whose `index == value`.
- Match semantics identical to the existing bare-index branch (exact string equality on
  `Session.index`), so both spellings are interchangeable.
- **Additive only.** Bare `1.1.1`, `id:`, `tty:`, `name:`, exact-id and substring behaviour must
  be untouched. This is a published CLI; scripts depend on all of them.
- Update the `send`/`read` argparse help strings and the module docstring example list to name
  `index:` alongside the others.

### S2 — a 0-match send is a failure, not a success

Applies to the scheduler and the web API. **Do not change the CLI's behaviour** — `cmd_send` /
`cmd_read` already print to stderr and return `1`; that part is correct today.

- `run_job()` (`iterm_web.py`): when `len(hits) == 0`
  - log with kind **`error`** (not `send`), message must name the job and the target and say
    plainly that nothing was delivered, e.g.
    `job "<name>" (<target>) matched 0 sessions — NOT DELIVERED: <command repr>`;
  - return a `status` string that is visibly a failure and cannot be confused with a delivery —
    it is written into `job["last_status"]` and rendered in the admin. Use exactly
    `MATCHED 0 SESSIONS — not delivered`.
  - `len(hits) >= 1` keeps its current log kind (`send`) and its current
    `sent to N session(s)` status string verbatim — the happy path is not being redesigned.
- `/api/send` (`iterm_web.py`): on 0 hits, log kind `error` with the same "not delivered" wording,
  and add a `"matched": 0` field to the JSON response. **Keep the existing `{"sent": hits}` key
  and the 200 status code** — changing the shape or the code would break any script posting to
  this endpoint. Surfacing goes in a *new* field, never by mutating an old one.
- Admin UI: a job row whose `last_status` is the failure string renders in the existing error
  colour (reuse the `err` / `#ffb0b0` styling already in the page — no new CSS system). The `send`
  panel shows an explicit "matched 0 sessions — nothing was sent" error rather than a bare ok.

### S3 — stop minting rotting targets in the UI (root cause)

Rule to implement: **immediate actions may use `id:`; scheduled actions must not.**

- `createJob()` — both the "ALL sessions" expansion and individually ticked session rows must
  build job targets as `index:<s.index>`, not `id:<s.id>`. Job names keep their current
  `<base> — <index>  <job>  <name>` shape.
- `renderJobTargets()` — checkbox `value` becomes `index:<s.index>` (this is the job-creation
  picker). Keep the `__all__` row and its exclusivity behaviour exactly as it is.
- **Unchanged on purpose:** the manual-send `<select>` (`targetOptions()`) and the sessions
  table's `use` button keep `id:<uuid>`. A manual send happens seconds after the page rendered
  the list, where a UUID is the *most* precise thing available and cannot rot in that window.
- `loadJobs()` — the target column currently resolves a display name only for `'id:'+s.id`. Make
  it also resolve `index:<idx>` and a bare index to the matching live session's name, and when
  nothing matches, show the raw target (current fallback) — an unresolvable target must look
  unresolvable, not blank.

### Out of scope — goes to BACKLOG #7, not this branch

- N-consecutive-miss warning / auto-pause of a job that has missed repeatedly.
- Any automatic migration of existing `id:` jobs on load. **The live file is retargeted by hand,
  by me, after this branch passes** — code must not rewrite the operator's job file behind them.
- A genuinely sticky per-session identity that survives both recreation *and* rearrangement.

## 4. Acceptance criteria (executable)

Suite: `python3 tests/run_tests.py` and `python3 tests/counter_test.py` must both be green, and
new coverage goes in the committed suite (retro L6 — a green suite whose counter-test is red
proves nothing).

- **AC-1** `resolve_targets(sessions, "index:2.1.1", False)` returns exactly the session whose
  `index == "2.1.1"`.
- **AC-2** `resolve_targets(sessions, "index:9.9.9", False)` returns `[]` — no substring fallback,
  no accidental name match.
- **AC-3** Regression: bare `"2.1.1"`, `"id:<prefix>"`, `"tty:…"`, `"name:<regex>"`, exact id,
  and bare-substring targeting all return what they returned before this change. Assert on all
  six forms, not a sample.
- **AC-4** A `name:` target that matches two identically named sessions still returns **both**
  (documenting the duplicate-delivery hazard rather than silently deduping).
- **AC-5** `run_job()` with a target matching 0 sessions returns
  `status == "MATCHED 0 SESSIONS — not delivered"` and emits a log entry of kind `error`.
- **AC-6** `run_job()` with a target matching 1 session still returns
  `"sent to 1 session(s)"` and logs kind `send` — byte-identical to today.
- **AC-7** `/api/send` with a 0-match target returns HTTP 200, `sent == []`, `matched == 0`, and
  logs kind `error`.
- **AC-8** Mutation / counter-test: delete the new 0-match branch in `run_job` and at least one
  test must go red. Ayala rejected 1.3.0 once for a fix with zero regression coverage — do not
  bring that back.
- **AC-9 (real terminal, tester)** On a **throwaway** session — never the operator's live ones —
  a scheduled job with a bare-index target fires and the text arrives; then kill and recreate the
  session in the same pane position and the **same job fires and delivers again** without being
  edited. This is the whole claim of the CEO's decision; if it fails, say so loudly.
- **AC-10 (real terminal, tester)** Same throwaway job with an `id:` target, after recreation,
  now logs the AC-5 failure state — i.e. the bug that hid for 20 days is now visible within one
  fire.

## 5. Constraints

- **Stdlib only**, both files. itermon has no runtime dependencies and that is a product promise.
- **Never touch `iterm_jobs.json` or `activity.log` in the repo root** during development or
  testing. They are live operator state (CLAUDE.md). A test server must run from a copied tree so
  it gets its own `JOBS_FILE`/`ACTIVITY_FILE`, on a port that is **not 8765**, and must never be
  left running.
- The live admin (PID 1471, `127.0.0.1:8765`) **stays up and is not restarted** by anyone on this
  branch. It is the operator's scheduler. Do not start a second scheduler against the real jobs
  file — that double-fires every job into the CEO's real sessions.
- Prefer `ITERMON_BACKEND=tmux` on a throwaway socket for resolution-logic verification (zero
  reach into iTerm2), and keep real-iTerm2 work to the minimum needed for AC-9/AC-10. Kill the
  throwaway tmux server and remove the socket afterwards (this product's signature mess).
- Branch discipline (rule 15): `feature/stable-job-targets` off `develop`; **do not push to
  `origin`** (item #5 / the open compliance finding); merge to `develop` is mine, after tester
  PASS + Ayala APPROVE.
