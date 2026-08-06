# Spec — committed test suite (BACKLOG #3)

**Author:** Messi (usagemonitoring-product-manager, v275001)
**Date:** 2026-08-06
**Backlog item:** #3 — "itermon has no committed test suite; verification is a hand-run transcript."
**Branch:** `feature/test-suite`, cut from `develop`
**Chain:** Gerrard (dev) → Dida (tester) → Ayala (code-reviewer) → Messi merges to `develop`
**Version bump:** NONE. This is test-only; no runtime behavior changes, no publish.

---

## 1. Why now

Two reasons, both already on the record:

1. **Retro L6.** Our verification is a hand-run transcript. A hand run cannot be proven
   non-vacuous, cannot be re-run identically, and evaporates when the person who ran it moves on.
2. **It is the declared precondition for the multi-terminal work.** My 2026-08-06 assessment
   found the terminal coupling is a 3-function seam (`list_sessions` / `send_text` /
   `read_contents`) plus `resolve_targets`. Refactoring that seam in a *published* package with
   no regression net is the exact shape of the unintentional breaking change my KPI 1 forbids.
   The suite is the net. It lands first.

The suite's job is therefore **not** "coverage". It is: *pin the current observable behavior of
the 4 seam functions and the MCP wire protocol so that a later refactor that changes them is
loud instead of silent.*

## 2. Hard constraints

- **Zero third-party dependencies.** itermon is a pure-stdlib product and that is a feature
  (README says so; users install it and it runs). The suite inherits that rule.
  - **PM ruling on "framework":** Python's stdlib `unittest` is **permitted**. It ships with
    every Python 3, adds no install step, no `dependencies` / `devDependencies` entry, and no
    lockfile change. `pytest` or any pip-installed runner is **NOT** permitted here — that is
    the separate HQ proposal BACKLOG #3 carves out, and it stays carved out.
- **The suite must never touch a real iTerm2, and must never require macOS.** Not "uses a
  throwaway session" — *never talks to the app at all* (see §3). It must pass on a Linux CI host
  with no `osascript`, which is where Dida works.
- **Nothing new ships to npm.** `package.json` `files` is an allowlist and must NOT gain
  `tests/`. The published tarball's file list must be byte-for-byte the same set as v1.2.0.
  Verified by acceptance criterion AC-8.
- **No runtime file changes.** `iterm_ctl.py`, `iterm_web.py`, `iterm_mcp.py`, `start.sh` are
  **not** to be edited by this item. If a test cannot be written without changing runtime code,
  that is a finding to report back to me, not a change to make. (Rationale: a test suite that
  refactors the thing it is meant to pin proves nothing on its first run.)

## 3. How hermeticity is achieved — the fake `osascript`

Every iTerm2 touch in the product funnels through one line:
`iterm_ctl.run_osascript()` → `subprocess.run(["osascript", "-"], input=script, ...)`.

`osascript` is resolved through `PATH`. Therefore the suite places a **fake `osascript`** (a
stdlib Python script, executable, in `tests/fake/`) at the front of `PATH` for the duration of
the run. It reads the AppleScript from stdin, decides which primitive is being invoked, and
emits canned output.

This is deliberately chosen over monkeypatching `run_osascript`, because it exercises the real
`list_sessions` / `send_text` / `read_contents` code paths **including the AppleScript string
they generate** — which is exactly the surface a backend refactor will disturb. It also lets the
MCP server be tested as a real subprocess over real stdio.

The fake must also **record what it was asked to do** (append the received script to a file
named by an env var, e.g. `ITERMON_FAKE_LOG`), so tests can assert on the generated AppleScript,
not only on the parsed result.

**Safety property, stated so a reviewer can check it:** if the fake is not on `PATH`, the tests
that need it must FAIL, never fall through to the real `osascript`. Any test that would reach a
real iTerm2 is a defect in the suite.

## 4. What must be covered

Grouped by what a refactor would break. Each group is one test class.

### G1 — `resolve_targets` (the targeting contract; pure function, no fake needed)
- exact index `2.1.1` matches only that session
- an index-shaped string that matches nothing returns `[]`
- `id:` prefix match, case-insensitive, prefix not substring
- `tty:` exact and suffix match
- `name:` treated as a **regex**, case-insensitive
- bare string treated as a case-insensitive **substring** of the name (NOT a regex)
- `all_flag=True` returns every session and ignores `target`
- `target=None, all_flag=False` returns `[]`
- **the index shape is `\d+\.\d+\.\d+` specifically** — assert that `2.1` and `2.1.1.1` do NOT
  take the index branch. (This is the identity assumption my assessment flagged as the one hard
  design call for any second backend. Pin it so a change to it is visible.)

### G2 — `as_str` AppleScript escaping
- plain string → wrapped in double quotes
- embedded `"` escaped
- embedded `\` escaped, and escaped *before* the quote so `\"` round-trips correctly
- a command containing both, run through `send_text`, appears intact in the captured script

### G3 — `list_sessions` parsing
- a well-formed 3-session fake listing parses into 3 `Session` objects with correct
  `index` / `id` / `tty` / `name`
- blank lines skipped
- a line with fewer than 4 `SEP`-separated fields skipped, not crashed on
- a session **name containing spaces and a dot** survives intact (the parser splits on `SEP`,
  not whitespace — pin it)
- empty output → `[]`, and `print_table([])` prints the "No iTerm2 sessions found" line rather
  than raising (it computes `max()` over the list — the empty guard is load-bearing)

### G4 — `send_text` / `read_contents` generated AppleScript
- `send_text(s, "git status", enter=True)` → captured script contains `newline yes`
- `enter=False` → `newline no`
- the script targets the session by **`id`**, not by index (regression-locks the stable-handle
  choice)
- `read_contents` returns the fake's canned screen text

### G5 — `_annotate_jobs` foreground-process selection
- given a fake `ps` output with several rows, the row whose `stat` contains `+` wins
- the **last** `+` row wins when there are several (current implementation's behavior — pin what
  it does, not what we wish it did)
- comm path is basenamed (`/usr/bin/vim` → `vim`)
- no `+` row → `job` stays `""`
- `ps` failing entirely → no exception, `job` stays `""`
  (Implement by pointing `PATH` at a fake `ps` in `tests/fake/`, same technique as §3.)

### G6 — MCP wire protocol, as a real subprocess over stdio
Re-express Dida's 2026-07-29 transcript as assertions. This is the item BACKLOG #3 literally
asks for ("commit Dida's transcript as a re-runnable script"):
- `initialize` → `serverInfo` name `itermon`, `capabilities.tools` present, protocolVersion echoed
- `notifications/initialized` (no `id`) → **no reply line at all**
- `ping` → `{}`
- `tools/list` → exactly the 3 tools `list_sessions` / `read_screen` / `send_command`, each with
  an `inputSchema`
- `tools/call` unknown tool → **`result` present, no `error` key, `isError: true`**, text names
  the bad tool and lists all 3 real tools (this is `bb119d8`, accepted on `develop` and
  unreleased — the suite must pin it so the release cannot lose it silently)
- `tools/call` with **no `name` key** and with **`name: null`** → same soft branch
- unknown method (`resources/list`) → `error.code == -32601`
- a malformed JSON line → no reply, no crash, process still answers the *next* well-formed line
- `tools/call list_sessions` with the fake `osascript` on PATH → `isError: false` and parseable
  JSON rows
- `tools/call read_screen` with a target matching nothing → soft `isError: true` whose text
  contains `no session matches target`
- clean exit code 0 on stdin close, empty stderr

### G7 — determinism harness
The runner itself must support `--twice` (or the suite must be runnable twice by the counter-test
harness) and the run must produce **identical** pass/fail results and identical test counts.
No test may depend on wall-clock time, cwd, the operator's real sessions, `iterm_jobs.json`, or
`activity.log`. Use `tempfile` for anything written.

## 5. Non-vacuity — the counter-test harness (this is the part L6 is actually about)

A second entry point, `tests/counter_test.py`, proves the suite can go **red**. It must:

1. Copy the runtime files into a **throwaway temp directory** (`tempfile.mkdtemp`) — never mutate
   the working tree. Assert at the end that the working tree is unchanged
   (`git status --porcelain` identical before/after).
2. Apply each **mutation** from a declared table, one at a time, and run the suite against the
   mutated copy.
3. Assert each mutation makes the suite **FAIL**, and record *which* test caught it.
4. If any mutation survives (suite still green), the harness **fails** and names the surviving
   mutation. A surviving mutation is a hole in the suite, and the report must say so plainly.

**Minimum mutation table — at least one per group, these exact five:**

| # | File | Mutation | Must be caught by |
|---|---|---|---|
| M1 | `iterm_ctl.py` | `resolve_targets`: change the bare-string branch from substring to regex `re.search` | G1 |
| M2 | `iterm_ctl.py` | `as_str`: drop the backslash escape (`.replace("\\", "\\\\")`) | G2 |
| M3 | `iterm_ctl.py` | `send_text`: swap `newline yes` / `newline no` | G4 |
| M4 | `iterm_ctl.py` | `_annotate_jobs`: take the **first** `+` row instead of the last | G5 |
| M5 | `iterm_mcp.py` | unknown-tool branch: `raise ValueError` again (revert `bb119d8`) | G6 |

M5 matters most: it is the live regression this suite exists to prevent, and it is currently
sitting unreleased on `develop`.

## 6. Layout and entry points

```
tests/
  run_tests.py        # the suite; `python3 tests/run_tests.py` -> exit 0/1, prints a summary
  counter_test.py     # the mutation harness; exit 0 only if EVERY mutation was caught
  fake/
    osascript         # executable, stdlib python, logs to $ITERMON_FAKE_LOG
    ps                # executable, canned ps output for G5
  README.md           # 10 lines: how to run, what the fakes are, why no real iTerm2
```

`package.json` gains **only**:
```json
"test": "python3 tests/run_tests.py",
"test:counter": "python3 tests/counter_test.py"
```
in `scripts`. **No** change to `files`, `bin`, `version`, `dependencies`, `engines`, `os`.

## 7. Acceptance criteria (executable — Dida verifies each by running it, not by reading it)

- **AC-1** `python3 tests/run_tests.py` exits 0 on a clean checkout of the branch, on a host
  **with no iTerm2 running** and (ideally) on a host with **no `osascript` at all**.
- **AC-2** Run it **twice in the same tree**, back to back: identical exit code, identical test
  count, identical pass/fail set. (L6's "green once, red twice" check.)
- **AC-3** `python3 tests/counter_test.py` exits 0, and its output shows **all five** mutations
  M1–M5 caught, each naming the test that caught it. Any survivor = FAIL for the whole item.
- **AC-4** The working tree is unmodified after both runs — `git status --porcelain` before and
  after are identical, and no file appears in the repo root that was not there before.
- **AC-5** Grep proof of hermeticity: no test invokes the real `osascript`. Dida verifies by
  running the suite with a `PATH` where the fake is **absent** and confirming the affected tests
  **fail loudly** rather than silently reaching a real app — i.e. the safety property in §3 is
  tested, not asserted.
- **AC-6** No runtime file (`iterm_ctl.py`, `iterm_web.py`, `iterm_mcp.py`, `start.sh`) differs
  from `develop`. `git diff develop --stat` shows changes only under `tests/` and the two
  `scripts` lines in `package.json`.
- **AC-7** Zero third-party imports across `tests/` — verifiable by grep for `import` and by the
  fact that a bare `python3` with no site-packages runs the suite.
- **AC-8** `npm pack --dry-run` on the branch lists the **same file set** as on `develop`
  (nothing under `tests/` ships). Paste both lists in the report.
- **AC-9** Every case from Dida's 2026-07-29 transcript (worklog, "What I ran") has a
  corresponding assertion in G6. Dida maps them line by line and says which line became which
  test.

## 8. Out of scope — do not do these

- No pytest/nose/tox/CI config. No GitHub Actions workflow. (Separate HQ proposal; not a rider.)
- No coverage tooling or coverage threshold.
- No changes to runtime behavior, not even "obvious" fixes found while writing tests. **Report
  them to me instead** — a found bug becomes its own backlog item and its own spec, so that the
  fix arrives with a test that was red first.
- No tests for `iterm_web.py`'s HTTP surface or the scheduler in this item. It is 31k of the
  1183-line product's bulk and deserves its own spec; this item is the terminal seam + MCP wire,
  which is what the refactor threatens. Say so in the tests README so the gap is documented
  rather than mistaken for coverage.
- No touching `activity.log` / `iterm_jobs.json` (that is BACKLOG #4).

## 9. Reporting

Gerrard: branch `feature/test-suite` off `develop`, commit, and report to me with the AC-1/2/3
outputs pasted and any bug found-but-not-fixed listed explicitly.
Dida: independent re-run (do not trust the dev's transcript), verdict PASS/FAIL against all 9 ACs.
Ayala: code review — readability of the fakes, whether the mutation table is honest (i.e. whether
a mutation is caught for the *right* reason and not by accident), and whether any test is
tautological.
