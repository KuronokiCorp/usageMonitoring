# itermon test suite

Run it: `python3 tests/run_tests.py` (or `npm test`). Twice-in-a-row determinism
check: `python3 tests/run_tests.py --twice`. Prove the suite can go red:
`python3 tests/counter_test.py` (or `npm run test:counter`).

Zero third-party dependencies -- stdlib `unittest` only, no pytest, nothing to
`pip install`. That's a deliberate constraint (spec:
`docs/specs/committed-test-suite.md`), not an oversight.

**Never touches a real iTerm2, and never requires macOS.** Every AppleScript
call iterm_ctl.py can make funnels through one function, `run_osascript()`,
which just runs `osascript` off `PATH`. This suite puts a fake `osascript`
(`tests/fake/osascript`, a stdlib Python script) and a fake `ps`
(`tests/fake/ps`) at the front of `PATH` for the whole run, so the real
`list_sessions` / `send_text` / `read_contents` code paths run for real --
including the AppleScript string they generate -- against canned output
instead of a live app. If the fake is ever missing from `PATH`, the affected
tests fail loudly (an exception) rather than silently reaching a real
`osascript`; that safety property is itself a test
(`G3ListSessions.test_fake_absent_fails_loudly_...`), not just a claim.

The two fakes need their executable bit set for `PATH` lookup to find them
the same way it finds a real `osascript`/`ps`; `run_tests.py` checks and
`chmod`s them itself at import time, so a fresh checkout that lost the bit
(some sandboxes/archivers do) self-heals instead of failing for an unrelated
reason. Both fakes start with `#!/usr/bin/env python3` -- `hermetic_env()`'s
`PATH` is the fake dir *plus* the running interpreter's own directory
(appended after, never before, and checked once at startup to hold no real
`osascript`/`ps`), so `env` can resolve `python3` without ever widening PATH
to anything that could reach a real one.

## What's covered (G1-G6 in run_tests.py)

- **G1** `resolve_targets` -- the targeting contract (index/id:/tty:/name:/bare
  substring, `--all`, the exact `\d+\.\d+\.\d+` index shape).
- **G2** `as_str` -- AppleScript string escaping (quotes, backslashes, order).
- **G3** `list_sessions` -- parsing the fake listing, blank/short lines, empty
  case, the PATH-absent safety property.
- **G4** `send_text` / `read_contents` -- the generated AppleScript (newline
  yes/no, targets by id not index, canned screen contents).
- **G5** `_annotate_jobs` -- foreground-process selection via a fake `ps`.
- **G6** the MCP wire protocol (`iterm_mcp.py`), run as a real subprocess over
  stdio -- this is Dida's 2026-07-29 transcript
  (`docs/worklog/usagemonitoring-tester/2026-07-29.md`) re-expressed as
  assertions, plus `list_sessions`/`read_screen` over the fake.

`tests/counter_test.py` is the non-vacuity harness: it copies the runtime
files into a throwaway `tempfile.mkdtemp()` directory (never touches this
working tree -- it asserts `git status --porcelain` is identical before and
after), applies five known mutations one at a time (the ones a multi-terminal
backend refactor would most plausibly get wrong), and asserts every one of
them turns the suite red. A mutation that survives green is a hole in the
suite, and the harness says so by name.

## What's *not* covered here, on purpose

- `iterm_web.py`'s HTTP surface and the cron scheduler. It's 31k of the
  product's 1183 lines and deserves its own spec; this item is scoped to the
  terminal seam (`resolve_targets` / `send_text` / `read_contents` /
  `list_sessions`) plus the MCP wire protocol, because that's what the
  multi-terminal refactor threatens. This gap is a scope decision, not
  something to mistake for coverage.
- `activity.log` / `iterm_jobs.json` -- out of scope for this item
  (BACKLOG #4), and no test in here reads or writes either file.
