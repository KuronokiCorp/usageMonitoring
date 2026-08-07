# Changelog

All notable changes to `itermon` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [Semantic Versioning](https://semver.org/): breaking changes always land as
a major version bump, with migration notes.

This file starts with the 1.3.0 release (7 Aug 2026). Entries for 1.0.0–1.2.0 below are
reconstructed from git history for reference; they were not written at the time.

## [Unreleased]

## [1.3.0] - 2026-08-07

### Added
- **tmux universal backend.** itermon now works inside any terminal that can host a tmux
  session — Ghostty, Warp, Alacritty, WezTerm, Terminal.app, VS Code's terminal — and
  **over SSH on a Linux box**, not just iTerm2 on macOS. The honest cost: for any terminal
  other than iTerm2, your work has to be running *inside* tmux; itermon talks to tmux, not
  to the terminal window directly. On macOS with iTerm2, nothing changes — that path still
  needs no tmux at all.
- `--backend {auto,iterm2,tmux}` flag on `list`, `send`, `read`, `watch`.
- `ITERMON_BACKEND` environment variable (`iterm2` | `tmux`) — the way to select a backend
  for the MCP server or any scripted caller without a code change.
- **`auto` default resolution: `darwin` → `iterm2`; every other platform → `tmux`.**
  Deliberate ruling, not an oversight: **macOS still defaults to iTerm2 even when itermon
  is run from inside a tmux pane** (`$TMUX` set) — `auto` does not sniff `$TMUX` and switch.
  Existing macOS users get byte-identical output on upgrade; if you want the tmux backend
  on macOS, opt in explicitly with `--backend tmux` or `ITERMON_BACKEND=tmux`.
- New `iterm-ctl backends` subcommand — prints both backends, which one is available on
  the current machine, which one `auto` would pick here, and the full capability table
  (`stable_ids`, `job_column`, `job_via_ps`, `cwd`, `scrollback`, `needs_os_permission`,
  `cross_platform`).
- `resolve_targets` gains exact-match-on-index-or-id as a selector, so tmux pane
  addresses (`work:2.0`) and bare pane ids (`%3`) resolve the same way iTerm2 selectors do.
- Additive `itermon` bin alias alongside the existing `iterm-ctl` (which keeps working,
  unchanged — it is not going away).

### Changed
- **`os: ["darwin"]` removed from `package.json`.** `npm install itermon` on Linux no
  longer fails with `EBADPLATFORM`. This widens who can *install* the package — it is not
  a claim that every part of itermon runs cross-platform. The web admin (`iterm-admin`)
  and the cron scheduler remain **macOS + iTerm2 only**; see the README's platform table.
- `package.json` `description` and `keywords` updated to mention tmux/SSH/Linux.
- **MCP: unknown-tool error shape** (shipped to `develop` 2026-07-29, released here). A
  `tools/call` request naming a tool that doesn't exist used to raise a raw JSON-RPC
  `-32603` transport error. It now returns a **soft tool result** —
  `{"content": [{"type": "text", "text": "Unknown tool: ... Available tools: ..."}],
  "isError": true}` — the same shape as any other tool-execution failure, so an MCP
  client/model sees a readable error instead of a transport fault. Judged patch-level:
  it only changes the shape of an already-failing call, and the new text names the
  available tools, which the raw RPC error did not. Called out explicitly here per the
  release-manager's standing instruction not to let a behavior change ride silently.

### Unchanged (verified, not just claimed)
- The existing macOS/iTerm2 path is byte-identical: `iterm_mcp.py` and `iterm_web.py`
  have a 0-line diff from this work; the generated AppleScript was diffed and matches.
  No existing CLI flag, output shape, or exit code changed on the iTerm2 path.

## [1.2.0] - 2026-07-24

### Added
- MCP server (`iterm_mcp.py`) shipped in the npm package, alongside the existing
  CLI (`iterm-ctl`) and local web admin. Exposes `list_sessions`, `send_text`,
  `read_contents` (and friends) as MCP tools over stdio JSON-RPC.
- `itermon-mcp` bin entry.

Additive/backward-compatible; nothing removed or behaviorally changed on the existing
CLI/web-admin path.

## [1.1.4] - 2026-07-19

### Changed
- Republish only, to refresh the README on npm with updated "Buy Me a Coffee" funding
  links. No code change.

## [1.1.3] - 2026-07-18

### Added
- Landing page at itermon.vectech.co linked from the README; funding links.

## [1.1.2] - 2026-07-14

### Fixed
- Web admin: `/api/sessions` responses had no cache headers, so browsers served a stale
  session list after a window/session was killed. Added `Cache-Control: no-store` on API
  responses and `cache: 'no-store'` on the front-end fetch.

## [1.1.1] - 2026-07-13

### Changed
- README: documented window/tab/split-pane indexing (`W.T.S`) and added the web admin
  screenshot.

## [1.1.0] - 2026-07-12

### Removed
- **The AI auto-continue feature** (`iterm_ai.py`, the MiniMax + heuristic decision
  engine): dropped `ai_check` jobs, the `/api/ai/*` endpoints, `resume_at` precise-wake
  scheduling, the AI badge/check button/checkbox in the web admin, and MiniMax config.
  Cron jobs now do plain sends. Core stayed: monitor, send, read, watch, cron scheduler,
  activity log.

Note: this was a feature *removal* shipped as a minor version at the time, pre-dating
this changelog and this product's current semver discipline (breaking = major, always).
Recorded here for historical accuracy, not as a precedent — see the release-manager's
1.3.0 KPI commitment above.

## [1.0.0] - 2026-07-10

### Added
- First npm-packaged release: `package.json`, `bin` entries (`iterm-ctl`, `iterm-admin`),
  `publish:npm` script. Package later renamed to `itermon` (was
  `iterm-usage-monitor`).

Note: no git tags exist on the public `KuronokiCorp/usageMonitoring` remote for any past
release, so version-compare links are intentionally omitted here rather than shipped
broken. `npm view itermon versions` is the source of truth for what was actually
published; this file's dates are npm-publish dates reconstructed from commit history.
