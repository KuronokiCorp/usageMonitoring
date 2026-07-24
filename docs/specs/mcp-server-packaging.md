# Spec — Ship the MCP server into the npm package (itermon v1.2.0)

- **Owner (PM):** Messi (usagemonitoring-product-manager, v275001)
- **Developer:** Gerrard (v475001) · **Tester:** Dida (v475002) · **Reviewer:** Ayala (code-reviewer)
- **Date:** 2026-07-24
- **Branch:** `feature/mcp-server-packaging` → `develop`
- **Backlog item:** #1 "Ship itermon v1.2.0: MCP server into the npm package" (+ #2 Dida verification, folded in)

## Problem

`iterm_mcp.py` — a working, zero-dependency MCP stdio server exposing
`list_sessions` / `read_screen` / `send_command` over iTerm2 — already lives at the
repo root, but it does **not** ship to npm users:

- not listed in `package.json` `files` (so `npm install -g itermon` never delivers it),
- no `bin` entry (no `itermon-mcp` on `PATH`),
- no README section documenting setup/tools/usage,
- version still `1.1.4` in both `package.json` and the server's `SERVER_INFO`.

## Scope (what "shipped" means)

1. **`package.json`**
   - Add `iterm_mcp.py` to the `files` allowlist so it ships in the tarball.
   - Add a `bin` entry `itermon-mcp` → `iterm_mcp.py`.
   - Add an `npm run mcp` convenience script.
   - Bump `version` `1.1.4` → **`1.2.0`** (new backward-compatible feature = minor, semver).
   - Add `mcp` / `model-context-protocol` keywords; mention MCP in `description`.
2. **`iterm_mcp.py`**
   - Sync `SERVER_INFO.version` to `1.2.0`.
   - Make the global bin (a symlink under `node_modules/.bin/`) able to
     `import iterm_ctl` by inserting the file's real directory onto `sys.path`.
3. **`README.md`**
   - New **MCP server** section: tools table, `.mcp.json` setup (installed bin +
     from-clone), `npm run mcp`, macOS-permission note, and a safety note.
   - Features bullet + Files-table row.

## Out of scope (explicitly)

- Any change to the three tools' behavior or the CLI/web admin.
- Changing how unknown-tool / unknown-method errors are shaped (pre-existing).
- The actual `npm publish` and `develop`→`main` promotion — **CEO-gated (rule 6)**.

## Acceptance criteria

- `npm pack --dry-run` lists `iterm_mcp.py` in the tarball; secrets/runtime files
  (`.env`, `iterm_jobs.json`, `activity.log`) still excluded.
- JSON-RPC handshake works over stdio: `initialize` returns `serverInfo`
  `{name: itermon, version: 1.2.0}` + tools capability; `tools/list` returns the
  three tools with schemas; `tools/call` returns a content array and surfaces an
  engine failure as `isError` (never a transport crash); notifications get no reply.
- The `itermon-mcp` bin, invoked via a symlink from an unrelated cwd, imports
  `iterm_ctl` successfully.
- **Safety:** verification never touches or disrupts a live iTerm2 session
  (per the product's tester safety rule). On a non-macOS host this is inherent —
  no iTerm2 exists — and `tools/call` returns `isError` rather than acting.

## Gates (rule 15)

Tester PASS + Ayala APPROVE before merge to `develop`. `develop`→`main` + publish
stays a CEO-approved release.
