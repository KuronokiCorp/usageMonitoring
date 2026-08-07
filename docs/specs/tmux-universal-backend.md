# Spec — tmux universal backend + backend seam extraction

**Item:** BACKLOG 0c (+ 0d, 0e, 3a)
**Date:** 2026-08-07
**Author:** Messi (usagemonitoring-product-manager, v275001)
**Authority:** CEO decision via Zidane, 2026-08-07 — **build the tmux universal backend.**
Supersedes the 2026-08-06 "Option A: validate first, build later" hold on item 0c. The CEO also
**pre-authorized the npm release** conditional on the full gate (tester PASS + code-reviewer
APPROVE + PM ACCEPT, **no overrides**).
**Evidence base:** `docs/spikes/2026-08-06-terminal-universality-spike.md` (all three tmux
primitives verified live on tmux 3.6a). This spec is the spike's design calls made binding.
**Regression net:** `tests/run_tests.py` + `tests/counter_test.py` (BACKLOG #3, `d210001`).

---

## 0. What this is for, in one paragraph

itermon today can only see iTerm2, on macOS, with a TCC automation grant. The spike proved that
adding terminals one at a time can never reach "works in any terminal" — you cannot write an
adapter for an API a vendor never shipped, and Ghostty, the newest and best-designed API in the
set, cannot read a screen at all. tmux gets there in one move: it runs *inside* iTerm2, Ghostty,
Warp, Alacritty, WezTerm, Terminal.app, VS Code — **and over SSH on a Linux box**, which is where
the agent-babysitting use case our own npm keywords target actually lives. This spec extracts a
backend seam behind itermon's existing 4-function surface and adds a tmux backend behind it.

**The single most important acceptance property: the iTerm2 path does not change.** Not its
output, not its generated AppleScript, not its error strings. The suite is the proof, and it was
built for exactly this refactor.

---

## 1. Scope

**In scope**
1. Backend seam extraction behind `list_sessions` / `send_text` / `read_contents` /
   `resolve_targets` (item 0d's standing convention made structural).
2. A tmux backend implementing the three primitives, with an explicit capability contract.
3. Backend selection (flag + env var) with a deterministic, backward-compatible default.
4. BACKLOG **3a** — close the `resolve_targets` `name:` search→match coverage hole.
5. README multi-terminal story; honest platform claims; `package.json` `os` decision.

**Out of scope — do not touch, do not "while we're here"**
- `iterm_web.py`'s HTTP surface and the cron scheduler (stays iTerm2-only this release; the web
  admin is a macOS/iTerm2 tool and the README must say so).
- BACKLOG #1 (AI job management), #4 (activity.log retention), #5 (records split).
- Any history rewrite or force-push (BACKLOG #5 scope, explicitly unauthorized).
- kitty / WezTerm / Ghostty / PTY-wrapper backends. The seam must make them *possible*, not
  present. No speculative third backend, no plugin loader, no entry-point registry.
- Adding any third-party dependency. itermon is zero-dep and stays zero-dep.

---

## 2. The seam

### 2.1 Hard compatibility constraint (this is the acceptance bar, not a guideline)

`iterm_mcp.py` and `iterm_web.py` both do `import iterm_ctl` and call **module-level**
`iterm_ctl.list_sessions()`, `iterm_ctl.resolve_targets(...)`, `iterm_ctl.send_text(s, text,
enter=...)`, `iterm_ctl.read_contents(s)`. `tests/run_tests.py` additionally reaches
`iterm_ctl.as_str`, `iterm_ctl._annotate_jobs`, `iterm_ctl.Session`, `iterm_ctl.print_table`,
`iterm_ctl.run_osascript`, `iterm_ctl.SEP`, `iterm_ctl.APP`.

**Every one of those names keeps its current module-level location, signature and behavior.**
The seam goes *underneath* them. `list_sessions()` becomes "ask the active backend"; it does not
move, get renamed, gain a required argument, or change what it returns. A refactor that forces an
edit to `iterm_mcp.py`'s call sites has failed this spec.

### 2.2 Shape

A backend is a small object (class instance or module — implementer's choice, class preferred)
exposing:

```
name          -> str            # "iterm2" | "tmux"
list_sessions()  -> list[Session]
send_text(session, text, enter) -> None
read_contents(session) -> str
empty_message   -> str          # what print_table prints when there are no sessions
CAPABILITIES    -> dict[str, bool]   # section 4
```

`resolve_targets` is **backend-neutral and stays a free function** — it operates on `Session`
value objects, not on a terminal. That is deliberate: identity semantics are the thing two
backends must agree on, so they live in one place both backends are held to.

`Session` keeps its current fields (`index`, `id`, `tty`, `name`, `job`, `cwd`). No new required
field. The docstring comments on `index` and `id` must be updated to describe both backends
rather than only iTerm2's `window.tab.session`.

### 2.3 Backend selection

Resolution order, evaluated once at first use:
1. Explicit CLI flag `--backend {auto,iterm2,tmux}` (available on `list`, `send`, `read`,
   `watch`).
2. Environment variable `ITERMON_BACKEND` (`iterm2` | `tmux`). This is how `iterm_mcp.py` and
   any scripted caller select a backend without a code change — no new MCP tool arguments this
   release.
3. `auto` (the default): **`darwin` → `iterm2`; every other platform → `tmux`.**

**Ruling — `auto` never silently switches backends on macOS.** Not even when `$TMUX` is set.
Today every macOS user gets iTerm2 and some of them run itermon *inside* a tmux pane inside
iTerm2; if `auto` preferred tmux on seeing `$TMUX`, those users' `list` output would change shape
overnight from an upgrade they didn't ask for. That is exactly the "unintentional breaking
change" my KPI is written against. Deterministic and explainable beats clever: macOS users opt in
with `--backend tmux` or `ITERMON_BACKEND=tmux`, and the README tells them to. Revisit only if a
real user asks.

An unknown backend name is a fail-closed error listing the valid ones — never a silent fallback.

---

## 3. tmux backend

Everything here is from the spike, verified live on tmux 3.6a. Do not re-derive it; if reality
disagrees with this section, report the disagreement rather than improvising a fix.

### 3.1 list

One call:

```
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}<SEP>#{pane_id}<SEP>#{pane_tty}<SEP>#{window_name}<SEP>#{pane_current_command}<SEP>#{pane_current_path}'
```

with `<SEP>` = the existing `SEP = "\x1f"`. Field mapping:

| `Session` field | tmux source | Note |
|---|---|---|
| `index` | `#{session_name}:#{window_index}.#{pane_index}` | e.g. `work:2.0` — the human address, and the one tmux itself accepts as a target |
| `id` | `#{pane_id}` | `%3` — **server-lifetime stable**, proved in the spike to survive killing a neighbouring window. All sends/reads target this, never `index`. |
| `tty` | `#{pane_tty}` | |
| `name` | `#{window_name}` | **Design call, see below** |
| `job` | `#{pane_current_command}` | filled at list time; **`_annotate_jobs` is NOT called on the tmux path** |
| `cwd` | `#{pane_current_path}` | a field the iTerm2 backend leaves empty in practice |

**Design call — `name` is `#{window_name}`, not `#{pane_title}`.** `pane_title` is the closer
literal analogue of iTerm2's session title, but it defaults to the hostname, which is *identical
for every pane on the machine* — that would make `name:` targeting and the NAME column worthless
by default. `window_name` defaults to the running command and is the label the user actually
sees in their status bar and names after their project. Recorded here as intentional so it is
reviewed as a decision, not read as a mistake.

**Design call — no `ps` shell-out on the tmux path.** tmux hands us the foreground command
directly. Skipping `ps` removes a per-session subprocess, removes a macOS-vs-Linux `ps` flag
compatibility question we would otherwise have to own, and is simply more accurate. Consequence
for the capability contract: §4.

**"no server running"** on stderr with a non-zero exit is **not an error** — it maps to an empty
session list, the same way iTerm2 with no windows yields none. Any *other* non-zero exit raises
`RuntimeError` carrying tmux's stderr, which `main()` already renders as `error: ...` / exit 2.

### 3.2 send — fail-closed literal mode (BACKLOG 0e, non-negotiable)

```
tmux send-keys -t <pane_id> -l -- <text>          # the user's text, ALWAYS literal
tmux send-keys -t <pane_id> Enter                 # only when enter=True, as a SECOND call
```

`send-keys` without `-l` interprets its argument as a **key name**. The spike proved the
counter-case on a live pane: `send-keys 'C-c'` sends the Ctrl-C *key* and types nothing;
`send-keys -l -- 'C-c'` types the three characters. A backend that forgets `-l` would silently
convert any command containing `Enter`, `C-c`, `Escape`, `Space` or `Tab` into control keys —
i.e. **execute a different command than the user asked for**. That is precisely the failure retro
L1/L3 exists to prevent, and "refuse rather than send the wrong thing" is this product's rule.

Requirements:
- `-l --` is mandatory and must be covered by a test that fails if either token is dropped.
- Empty text with `enter=False` is a no-op (do not invoke tmux at all). Empty text with
  `enter=True` sends only the Enter — this preserves `iterm_web.py`'s "send a bare newline"
  behavior (`iterm_web.py:94`).
- The text is passed as a **single argv element**. No shell string interpolation anywhere in this
  backend — `subprocess.run([...])` with a list, never `shell=True`.

### 3.3 read

```
tmux capture-pane -p -t <pane_id>
```

Returns the visible screen — the same contract shape as iTerm2's `contents of s`. **Do not** add
`-S` scrollback in this release. Scrollback is a genuine capability tmux gives us that iTerm2
cannot, and it is worth having, but `read_contents()` means "the visible screen" to every existing
caller (CLI, web admin, MCP `read_screen`) and quietly widening it would change what a monitoring
tool returns on a cron timer. It goes in the capability contract as *available, not yet exposed*,
and becomes its own backlog item with its own flag.

### 3.4 tmux missing

If the tmux backend is selected (explicitly or by `auto` on a non-darwin platform) and `tmux` is
not on `PATH`: a clear `RuntimeError` naming the problem and how to fix it. Not a traceback, not
a silent empty list.

---

## 4. Capability contract

Backends differ, and the honest move is to say where in a place code and docs both read.

| Capability key | iterm2 | tmux | Meaning |
|---|---|---|---|
| `stable_ids` | `False` | `True` | `Session.id` survives structural change (iTerm2's `index` renumbers on window reorder; tmux `%id` does not) |
| `job_column` | `True` | `True` | JOB is populated |
| `job_via_ps` | `True` | `False` | job comes from a `ps -t <tty>` shell-out vs. from the terminal itself |
| `cwd` | `False` | `True` | `Session.cwd` is populated |
| `scrollback` | `False` | `True` | backend *could* read beyond the visible screen (not exposed this release, §3.3) |
| `needs_os_permission` | `True` | `False` | macOS TCC automation grant required on first use |
| `cross_platform` | `False` | `True` | works off macOS |

Surfaced as a new additive subcommand:

```
iterm-ctl backends
```

printing each backend, whether it is available on this machine, which one `auto` would pick here,
and the capability table. This is the one new user-visible command in this release; it exists so
"which backends do I have and what do they do" is answerable without reading the README, and so
the capability claims are executable rather than prose.

---

## 5. Identity model & `resolve_targets` (includes BACKLOG 3a)

### 5.1 The rule

`resolve_targets` gains **one** new branch, inserted **after** the `id:` / `tty:` / `name:`
prefix branches and **before** the bare-substring fallback:

> **exact match on `s.index` or `s.id`** — if any session matches exactly, return those.

This is a strict extension with **zero** iTerm2 risk: iTerm2 `index` values always match
`\d+\.\d+\.\d+` and are therefore consumed by the existing first branch, and an iTerm2 `id` is a
UUID that no one types bare. What it buys is that `iterm-ctl send work:2.0 "…"` and
`iterm-ctl send %3 "…"` both work on tmux, which is how tmux users already think about panes.

Every other branch is **byte-identical**, including `--all`, `target is None`, and the
`\d+\.\d+\.\d+` shape check.

### 5.2 The identity design call, resolved

The spike named `Session.index` instability as the one genuinely hard design call. It is hard
**only on the iTerm2 side**, and this release does not paper over that difference — it exposes it:

- **`id` is the machine handle.** Both backends already send and read exclusively by `id` (iTerm2
  looks up its UUID inside the AppleScript loop; tmux targets `%id`). Unchanged, both backends.
- **`index` is the human address**, and its stability is a **backend property**, now declared in
  `CAPABILITIES.stable_ids` rather than apologised for in the README.
- No attempt is made to synthesise a fake stable index for iTerm2 or a fake `W.T.S` index for
  tmux. Faking either would make two genuinely different terminals look identical right up to the
  moment a user's `2.1.1` hit the wrong pane. The README keeps its iTerm2 reorder warning and
  gains one line pointing out that the tmux backend does not have the problem.

### 5.3 BACKLOG 3a — the `name:` search→match hole

Found by Dida, confirmed by Ayala: mutating `pat.search` → `pat.match` in the `name:` branch
leaves the suite green, because every fixture pattern is `^`-anchored and the two are
indistinguishable on that data.

**Ruling: the code is correct and must NOT change.** `name:REGEX` is documented and shipped as an
unanchored search; switching to `match` would silently break every user whose pattern is not
anchored. This is a **test** hole, and the fix is a test fix:

1. Strengthen the fixture so at least one session name has the pattern in the **middle** (e.g. a
   session named `run daily backup` matched by `name:daily`) — a case where `search` finds it and
   `match` does not. **Strengthen the fixture; never weaken the mutation.**
2. Add **M6** (`pat.search` → `pat.match` in `resolve_targets`) to `tests/counter_test.py`'s
   mutation table and prove it now turns the suite red.

This is fixed *in this release specifically* because `resolve_targets` is the exact function whose
identity semantics gate the second backend (§5.1 adds a branch to it). Landing a new branch in a
function with a known blind spot next door is how the blind spot becomes a defect.

---

## 6. Packaging & platform honesty

### 6.1 `os: ["darwin"]` — remove it

**Ruling: remove the `os` field from `package.json`.**

npm's `os` field is not a documentation field, it is an **enforcement** field: `npm install
itermon` on Linux fails hard with `EBADPLATFORM`. Shipping a tmux backend whose entire point is
"works over SSH on a Linux server" while keeping a hard Linux install block would make the
headline feature unreachable by the users it is for. Removing a restriction cannot break an
existing macOS install — it only widens who may install — so this is additive, not breaking.

The honesty obligation moves where it belongs: into the README, as a table saying exactly what
works where, including that **the web admin and the cron scheduler remain macOS + iTerm2 only**.
Removing `os` must not become an implied claim that every part of itermon is cross-platform. Say
the limits out loud.

### 6.2 `bin` — add an additive `itermon` alias

Add `"itermon": "iterm_ctl.py"` alongside the existing `"iterm-ctl"`. **`iterm-ctl` keeps
working, permanently** — it is the documented entry point of two shipped versions and removing it
is exactly the breaking change I refuse to make. The alias exists because the tool is no longer
iTerm-specific and the package is already called `itermon`.

### 6.3 Metadata

- `description`: mention tmux and that it works in any terminal / over SSH.
- `keywords`: add `tmux`, `ssh`, `linux`. Keep every existing keyword.
- `files`: **unchanged**. `tests/` still does not ship. An `npm pack --dry-run` file-set diff
  against 1.2.0 must show **only** what this spec intends (no new runtime file is added by this
  spec — the backends live inside `iterm_ctl.py`).

### 6.4 Version

**1.3.0** — minor. New backend, new flag/env var, new `backends` subcommand, new bin alias,
widened platform support; no removal, no signature change, no behavior change on the existing
macOS/iTerm2 path. Version bump and CHANGELOG are the **release-manager's** step (Lampard), not
the developer's — the feature branch carries no version bump.

---

## 7. Documentation

`README.md`:
- New section near the top: **"Which terminal do you use?"** — the one-line story: itermon speaks
  iTerm2 natively on macOS, and speaks **tmux** everywhere else, which means it works inside
  Ghostty, Warp, Alacritty, WezTerm, Terminal.app, VS Code's terminal, and **over SSH on a Linux
  box**. The honest cost stated plainly in the same breath: **for non-iTerm2 terminals you must
  run your work inside tmux.** Do not bury that; a user who discovers it after installing is a
  user we misled.
- Backend selection: `--backend`, `ITERMON_BACKEND`, and the `auto` rule from §2.3 including the
  explicit "macOS defaults to iTerm2 even inside tmux — pass `--backend tmux` if you want tmux".
- The capability table (§4), matching `iterm-ctl backends` exactly.
- A **platform support** table: CLI (macOS: both backends / Linux: tmux), MCP server (same as
  CLI), web admin + cron scheduler (**macOS + iTerm2 only**).
- Keep the existing iTerm2 index-reorder warning; add that tmux pane ids do not have that problem.

---

## 8. Acceptance criteria

Executable. Dida verifies each one independently and reports per-AC, not in aggregate.

**Regression — the iTerm2 path is untouched**
- **AC-1** `python3 tests/run_tests.py` green, and `python3 tests/run_tests.py --twice` green
  (identical results across two fresh subprocesses), on the feature branch.
- **AC-2** `python3 tests/counter_test.py` green — **every** mutation still caught, including the
  new M6. A green suite with a surviving mutation is a suite that has stopped proving anything.
- **AC-3** The AppleScript generated by `list_sessions`, `send_text` and `read_contents` on the
  iterm2 backend is **byte-identical** to 1.2.0's. Verified by the existing G4 tests plus a direct
  diff against the 1.2.0 tree.
- **AC-4** `iterm_mcp.py` and `iterm_web.py` are **unmodified** (`git diff` empty for both), and
  the G6 MCP wire tests still pass.
- **AC-5** With no flag and no env var on macOS, `iterm-ctl list` uses the iterm2 backend and its
  output — including the empty-case message `No iTerm2 sessions found (is iTerm2 running?)` — is
  unchanged.

**tmux backend — verified against a real tmux, hermetically**
- **AC-6** Against a **throwaway tmux server on a private socket** (`tmux -S
  /tmp/itermon-test-*.sock`, never the operator's own server — the same discipline Dida applies to
  iTerm2): `list` shows every pane with index, stable `%id`, tty, name, job and cwd populated.
- **AC-7** `send` delivers a command that executes, and a `read` immediately after shows its
  output. Round-trip through the real CLI, not through internals.
- **AC-8** **Literal-mode counter-test:** sending the literal text `C-c` types the three
  characters `C-c` into the pane and does **not** deliver a Ctrl-C. Prove it by asserting the
  characters appear via `capture-pane`. This AC fails if `-l` or `--` is dropped.
- **AC-9** Pane-id stability: create three windows, kill the middle one, and assert the surviving
  pane keeps the same `Session.id` and is still addressable by it.
- **AC-10** `send`/`read` against a non-existent pane id produces the existing user-facing error
  path (`No sessions matched the target.` / `error: …`), never a traceback.
- **AC-11** With no tmux server running at all, `list` prints the empty message and exits 0 — it
  does not raise.
- **AC-12** tmux absent from `PATH` + tmux backend selected → a clear error naming tmux, exit
  non-zero, no traceback.

**Targeting**
- **AC-13** On tmux, all of these resolve the same pane: exact index `sess:0.0`, exact id `%0`,
  `id:%0`, `tty:…`, `name:<window name>`, and a bare substring of the name.
- **AC-14** On iTerm2 fixtures, every pre-existing `resolve_targets` case returns exactly what it
  returned before (this is G1, unchanged and still green).
- **AC-15** The `name:` fixture now distinguishes `search` from `match` (§5.3) — demonstrated by
  M6 going red.

**Packaging & docs**
- **AC-16** `npm pack --dry-run` file set is **identical** to 1.2.0's. No test file, no new file
  ships.
- **AC-17** `package.json` has no `os` field; `bin` contains both `iterm-ctl` and `itermon`;
  `files` unchanged.
- **AC-18** `iterm-ctl backends` runs on a machine with and without tmux installed and prints the
  capability table plus what `auto` would pick here.
- **AC-19** README contains the multi-terminal section, the backend-selection rules, the
  capability table matching AC-18's output, and the platform table that states the web admin and
  scheduler are macOS + iTerm2 only.
- **AC-20** Zero third-party imports added anywhere. `pip install` is never required.

---

## 9. Chain, branch, gates

- Branch `feature/tmux-backend` off `develop`.
- **Gerrard** (developer) implements §2–§7 and the test additions. No version bump on this branch.
- **Dida** (tester) verifies AC-1…AC-20 independently, throwaway tmux socket only, and re-runs the
  suite **twice** in the same tree plus the counter-test (retro L6).
- **Ayala** (code-reviewer) reviews against this spec.
- **Messi** ACCEPT / REQUEST CHANGES, then merges `--no-ff` to `develop`. **No single-heading**:
  I do not verify my own spec's implementation, and I do not review it.
- **Lampard** (release-manager) then prepares 1.3.0: version bump, `CHANGELOG.md` (this product
  has never had one — start it here), release notes that name the deliberate behavior deltas, and
  publish prep. Release itself is CEO-pre-authorized **conditional on the full gate above with no
  override**. If any gate goes red, the release does not happen and it comes back to me.

## 10. Things I decided so nobody has to guess

- `auto` never prefers tmux on macOS, even inside `$TMUX` (§2.3).
- tmux `Session.name` is `#{window_name}` (§3.1).
- No `ps` shell-out on the tmux path (§3.1).
- Scrollback is capability-declared but **not** exposed this release (§3.3).
- `resolve_targets` gains exactly one new branch, positioned to be a strict extension (§5.1).
- BACKLOG 3a is a **test** fix; `pat.search` stays (§5.3).
- `os: ["darwin"]` is removed, and the README pays the honesty debt (§6.1).
- `iterm-ctl` survives forever; `itermon` is additive (§6.2).
- Version is 1.3.0 and the bump happens at release prep, not on the feature branch (§6.4).
