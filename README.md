# iTerm2 Session Monitor & Controller

Monitor every running **iTerm2** session from one place, send commands to any of
them, and schedule recurring "continue" nudges that an AI only fires when a
session actually looks stuck.

Built for keeping long-running CLI agents (e.g. Claude Code) alive across usage
limits and interruptions — but works with any iTerm2 session.

- **macOS only** (uses iTerm2's AppleScript interface)
- **Zero dependencies** — pure Python 3 standard library; no `pip install`
- **No iTerm2 setup** — uses AppleScript, so the Python API and its preferences
  are not required (macOS will prompt once to allow automation; approve it)

---

## Features

- **List** all sessions across every window/tab with tty, foreground job, and a
  stable UUID.
- **Send** a command to one session, a matched subset, or all — from the CLI or a
  local web UI.
- **Read** any session's visible screen.
- **Watch** — a live auto-refreshing monitor.
- **Schedule** recurring sends with cron expressions (an in-process scheduler).
- **AI auto-continue** — before sending, an LLM (MiniMax) reads the session and
  decides `continue` / `wait` / `skip`, so nudges only go to genuinely idle or
  rate-limited sessions. Falls back to a conservative rule-based check when no
  API key is set.

---

## Requirements

- macOS with **iTerm2** installed and running
- **Python 3.10+**
- (Optional) a **MiniMax API key** for the AI auto-continue backend

---

## Quick start

```bash
git clone <your-repo-url> iterm-monitor
cd iterm-monitor

# CLI
./iterm_ctl.py list

# Web admin (loads .env if present)
cp .env.example .env      # add your MiniMax key (optional)
./start.sh --open         # serves http://127.0.0.1:8765
```

The first command may trigger a one-time macOS prompt to allow controlling
iTerm2 — approve it.

---

## The CLI — `iterm_ctl.py`

```bash
./iterm_ctl.py list                        # snapshot of all sessions
./iterm_ctl.py read 3.1.1                   # print a session's visible screen
./iterm_ctl.py send 3.1.1 "git status"      # run a command in one session
./iterm_ctl.py send --all "pwd"             # run in every session (asks y/N)
./iterm_ctl.py watch --interval 2           # live monitor (Ctrl-C to stop)
```

### Targeting a session

`send` and `read` accept any of these selectors:

| Selector        | Example         | Notes                                            |
|-----------------|-----------------|--------------------------------------------------|
| index           | `3.1.1`         | window.tab.session. **Positional — shifts when windows open/close.** |
| `id:PREFIX`     | `id:C86EE5`     | matches the stable session UUID. **Most reliable.** |
| `tty:NNN`       | `tty:ttys002`   | matches the device tty                           |
| `name:REGEX`    | `name:daily`    | case-insensitive regex on the title              |
| substring       | `AdMobs`        | case-insensitive substring of the title          |
| `--all`         | —               | every session                                    |

> **Prefer `id:` for anything scripted.** iTerm renumbers windows constantly
> (the frontmost becomes window 1), so `3.1.1` can point at a different session
> minute to minute; the UUID never moves.

### `send` flags

- `--no-enter` — type the text without pressing Return.
- `--yes` / `-y` — skip the confirmation on `--all` / multi-match sends.

---

## The web admin — `iterm_web.py`

```bash
./start.sh --open            # http://127.0.0.1:8765, loads .env
./start.sh --port 9000       # custom port
```

Bound to `127.0.0.1` only (local, no auth). Three panels:

1. **Sessions** — live auto-refreshing list; click *use* to target one.
2. **Send a command** — pick a session, type a message, Send. The **Submit**
   checkbox adds an extra Enter that Claude Code's TUI needs (leave it on for
   Claude sessions, off for a plain shell). *Preview screen* dumps the current
   contents; *AI check* shows the AI's verdict without sending.
3. **Scheduled (cron) sends** — register recurring jobs (see below).

### Registering scheduled jobs

Give a job a name, one or more target sessions (⌘/Ctrl-click for several — one
job is created per session), a message, and a 5-field cron expression
(`min hour day month weekday`), with preset buttons (every 5 min, hourly, daily
9am, weekdays 9am…). Jobs show their next/last run and can be run-now, paused, or
deleted. Targets are stored by **session UUID**, so a job keeps hitting the right
session even as iTerm reorders windows.

**How the scheduler runs:** an in-process thread wakes ~once a minute (aligned
just past each minute boundary), tests every job's cron expression against the
current minute, and fires the matches. Jobs persist to `iterm_jobs.json` and
survive restarts, but **only fire while the server is running** — this is
deliberate, because driving iTerm needs the automation permission the server
inherits from your terminal, which a plain system `crontab` usually lacks.

---

## AI auto-continue

A normal cron job fires its message unconditionally. Tick **AI check** and the
scheduler instead reads the session first and only sends when the session looks
stuck. Each check classifies the screen into:

- **continue** — idle/interrupted, or hit a usage limit that has already reset → send.
- **wait** — hit a usage limit whose reset is still in the future → don't send yet.
- **skip** — actively working, healthy idle prompt, or waiting on a human → leave alone.

**Precise resume timing.** On a `wait`, the model also reads the reset time shown
on screen ("resets at 6pm", "try again in 2 hours", "23:00") and returns it. The
scheduler parses that into an absolute time and sets a **one-shot wake** for that
moment — so instead of resuming at "the next poll after reset," the job re-checks
the session right as its limit resets (to the minute) and sends "continue" then.
If no reset time is shown, it falls back to ordinary polling. The job row shows
`⏰ wake <time>` while a precise resume is pending.

Two backends, chosen automatically (shown by the header badge):

- **MiniMax** (preferred) — calls MiniMax's OpenAI-compatible endpoint
  (`POST {base}/chat/completions`) over stdlib HTTP. Default model
  `MiniMax-M2.7` (a reasoning model; the code strips its `<think>…</think>`
  pass before parsing the JSON verdict). Configured via `.env` — see below.
- **Heuristic** (fallback) — pure-stdlib pattern matching, used when
  `MINIMAX_API_KEY` is unset or a call fails. Deliberately conservative: it only
  nudges on an explicit "you hit your limit" / "interrupted" signal and skips
  everything else, so it never types into a busy session.

Test either backend without sending anything via the **AI check** button, or:

```bash
echo "You've hit your usage limit.\n❯ " | ./iterm_ai.py     # prints the verdict
```

---

## Configuration

The web server reads these environment variables (via `.env`, loaded by
`start.sh`):

| Variable            | Default                          | Purpose                               |
|---------------------|----------------------------------|---------------------------------------|
| `MINIMAX_API_KEY`   | *(unset → heuristic fallback)*   | MiniMax API key                       |
| `MINIMAX_MODEL`     | `MiniMax-M2.7`                   | model id                              |
| `MINIMAX_BASE_URL`  | `https://api.minimax.io/v1`     | international; CN: `https://api.minimax.chat/v1` |

```bash
cp .env.example .env      # then edit .env with your key
./start.sh --open
```

`.env` is **gitignored** — do not commit your key. If you'd rather not keep it on
disk, `export MINIMAX_API_KEY=…` in your shell instead; `start.sh` uses whatever
is already in the environment.

---

## How it works

Everything rests on iTerm2's AppleScript interface. `iterm_ctl.py` wraps three
primitives via `osascript`:

- **list** — walk windows → tabs → sessions, reading UUID / tty / title
- **send** — `write text` to a session found by UUID (optionally with Enter)
- **read** — `get contents` of a session's visible screen

`iterm_web.py` is a stdlib HTTP server exposing those over JSON plus the cron
scheduler thread. `iterm_ai.py` is the decision engine (MiniMax + heuristic).

```
cron tick → read session screen → MiniMax: continue/wait/skip
          → if continue: type message (+ Enter for Claude's TUI) into that session by UUID
```

---

## Files

| File             | What it is                                             |
|------------------|--------------------------------------------------------|
| `iterm_ctl.py`   | CLI + the AppleScript primitives (list / send / read / watch) |
| `iterm_web.py`   | Local web admin + in-process cron scheduler            |
| `iterm_ai.py`    | AI decision engine (MiniMax backend + heuristic fallback) |
| `start.sh`       | Launches the web server with `.env` loaded             |
| `.env.example`   | Config template — copy to `.env`                       |
| `iterm_jobs.json`| Saved scheduled jobs (created at runtime, gitignored)  |

---

## Notes & limitations

- **macOS + iTerm2 only.** The AppleScript app name is `iTerm2`.
- **Scheduled jobs run only while the web server is running.**
- **`send --all` types into every session**, including ones running Claude Code —
  a y/N confirmation (CLI) is the only guard. Prefer targeting a specific `id:`.
- **Sending into a Claude Code session takes an extra Enter** to submit (the TUI
  treats a pasted newline as a literal newline); the web UI's *Submit* checkbox
  and the AI/cron path handle this automatically.
- **The heuristic fallback is coarse** — it can misread text like a usage-limit
  *banner* on an otherwise-idle session. Set a MiniMax key for accurate judgments.
