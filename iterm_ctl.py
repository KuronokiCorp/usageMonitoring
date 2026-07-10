#!/usr/bin/env python3
"""
iterm_ctl - monitor and control all running iTerm2 sessions from the command line.

Uses iTerm2's AppleScript interface (via osascript), so it needs no extra Python
packages and no changes to iTerm2's preferences. The first run may trigger a macOS
"allow Terminal to control iTerm2" automation prompt; approve it once.

Examples:
    ./iterm_ctl.py list
    ./iterm_ctl.py send 2.1.1 "git status"
    ./iterm_ctl.py send id:A0205 "ls -la"
    ./iterm_ctl.py send name:daily "echo hi"
    ./iterm_ctl.py send --all "pwd" --yes
    ./iterm_ctl.py read 2.1.1
    ./iterm_ctl.py watch --interval 2
"""
import argparse
import re
import subprocess
import sys
import time

APP = "iTerm2"  # iTerm2 AppleScript application name


# --------------------------------------------------------------------------- #
# AppleScript plumbing
# --------------------------------------------------------------------------- #
def run_osascript(script: str) -> str:
    """Run an AppleScript source string and return stdout (raises on failure)."""
    proc = subprocess.run(
        ["osascript", "-"],
        input=script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "osascript failed")
    return proc.stdout


def as_str(value: str) -> str:
    """Escape a Python string into an AppleScript double-quoted string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------- #
# Session discovery
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, index, sid, tty, name):
        self.index = index          # e.g. "2.1.1" (window.tab.session, 1-based)
        self.id = sid               # iTerm session UUID
        self.tty = tty              # /dev/ttysNNN
        self.name = name            # session title
        self.job = ""               # foreground process (filled in later)
        self.cwd = ""               # foreground process cwd (best effort)


# Field separator unlikely to appear in a title.
SEP = "\x1f"


def list_sessions() -> list[Session]:
    script = f"""
tell application {as_str(APP)}
  set out to ""
  set wi to 0
  repeat with w in windows
    set wi to wi + 1
    set ti to 0
    repeat with t in tabs of w
      set ti to ti + 1
      set si to 0
      repeat with s in sessions of t
        set si to si + 1
        set out to out & wi & "." & ti & "." & si & "{SEP}" & (id of s) & "{SEP}" & (tty of s) & "{SEP}" & (name of s) & linefeed
      end repeat
    end repeat
  end repeat
  return out
end tell
"""
    raw = run_osascript(script)
    sessions = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(SEP)
        if len(parts) < 4:
            continue
        sessions.append(Session(parts[0], parts[1], parts[2], parts[3]))
    _annotate_jobs(sessions)
    return sessions


def _annotate_jobs(sessions: list[Session]) -> None:
    """Fill in foreground job (and cwd when available) for each session via ps/lsof."""
    for s in sessions:
        tty_name = s.tty.replace("/dev/", "")
        try:
            out = subprocess.run(
                ["ps", "-t", tty_name, "-o", "pid=,stat=,comm="],
                capture_output=True, text=True,
            ).stdout
        except Exception:
            continue
        fg_pid = None
        fg_comm = ""
        for row in out.splitlines():
            cols = row.split(None, 2)
            if len(cols) < 3:
                continue
            pid, stat, comm = cols
            if "+" in stat:  # process group in the foreground
                fg_pid, fg_comm = pid, comm
        if fg_comm:
            s.job = fg_comm.split("/")[-1]


# --------------------------------------------------------------------------- #
# Targeting
# --------------------------------------------------------------------------- #
def resolve_targets(sessions, target, all_flag) -> list[Session]:
    if all_flag:
        return sessions
    if target is None:
        return []
    if re.fullmatch(r"\d+\.\d+\.\d+", target):
        return [s for s in sessions if s.index == target]
    if target.startswith("id:"):
        needle = target[3:].lower()
        return [s for s in sessions if s.id.lower().startswith(needle)]
    if target.startswith("tty:"):
        needle = target[4:]
        return [s for s in sessions if s.tty == needle or s.tty.endswith(needle)]
    if target.startswith("name:"):
        pat = re.compile(target[5:], re.IGNORECASE)
        return [s for s in sessions if pat.search(s.name)]
    # bare string -> treat as name substring
    return [s for s in sessions if target.lower() in s.name.lower()]


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def _for_session(session_id: str, body: str) -> str:
    """Wrap `body` (AppleScript acting on loop var `s`) in an id lookup over all sessions."""
    return f"""
tell application {as_str(APP)}
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is {as_str(session_id)} then
{body}
        end if
      end repeat
    end repeat
  end repeat
  error "session not found: " & {as_str(session_id)}
end tell
"""


def send_text(session: Session, text: str, enter: bool) -> None:
    newline = "yes" if enter else "no"
    body = f"          tell s to write text {as_str(text)} newline {newline}\n          return"
    run_osascript(_for_session(session.id, body))


def read_contents(session: Session) -> str:
    body = "          return (contents of s)"
    return run_osascript(_for_session(session.id, body))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def print_table(sessions: list[Session]) -> None:
    if not sessions:
        print("No iTerm2 sessions found (is iTerm2 running?).")
        return
    idx_w = max(4, max(len(s.index) for s in sessions))
    tty_w = max(3, max(len(s.tty) for s in sessions))
    job_w = max(3, max(len(s.job) for s in sessions))
    header = f"{'IDX':<{idx_w}}  {'TTY':<{tty_w}}  {'JOB':<{job_w}}  ID(8)     NAME"
    print(header)
    print("-" * len(header))
    for s in sessions:
        print(f"{s.index:<{idx_w}}  {s.tty:<{tty_w}}  {s.job:<{job_w}}  {s.id[:8]}  {s.name}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_list(args):
    print_table(list_sessions())


def cmd_send(args):
    sessions = list_sessions()
    targets = resolve_targets(sessions, args.target, args.all)
    if not targets:
        print("No sessions matched the target.", file=sys.stderr)
        return 1
    command = " ".join(args.command)
    if len(targets) > 1 and not args.yes:
        print(f"About to send to {len(targets)} sessions:")
        for s in targets:
            print(f"  {s.index}  {s.tty}  {s.job}  {s.name}")
        reply = input(f'Send {command!r} to all {len(targets)}? [y/N] ').strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 1
    for s in targets:
        send_text(s, command, enter=not args.no_enter)
        print(f"sent -> {s.index} ({s.tty}) {s.name}")
    return 0


def cmd_read(args):
    sessions = list_sessions()
    targets = resolve_targets(sessions, args.target, args.all)
    if not targets:
        print("No sessions matched the target.", file=sys.stderr)
        return 1
    for s in targets:
        print(f"===== {s.index}  {s.tty}  {s.name} =====")
        print(read_contents(s).rstrip("\n"))
        print()
    return 0


def cmd_watch(args):
    try:
        while True:
            sessions = list_sessions()
            sys.stdout.write("\033[2J\033[H")  # clear screen, home cursor
            print(f"iTerm2 sessions @ {time.strftime('%H:%M:%S')}  (Ctrl-C to stop)\n")
            print_table(sessions)
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description="Monitor and control all iTerm2 sessions.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", aliases=["ls"], help="list all sessions")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("send", help="send a command to matching session(s)")
    sp.add_argument("target", nargs="?", help="index (2.1.1), id:PREFIX, tty:NNN, name:REGEX, or substring")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="command text to send")
    sp.add_argument("--all", action="store_true", help="send to every session")
    sp.add_argument("--no-enter", action="store_true", help="type without pressing Enter")
    sp.add_argument("--yes", "-y", action="store_true", help="skip confirmation for multi-session sends")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("read", help="print visible screen contents of matching session(s)")
    sp.add_argument("target", nargs="?", help="index, id:, tty:, name:, or substring")
    sp.add_argument("--all", action="store_true", help="read every session")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("watch", help="live-refreshing session monitor")
    sp.add_argument("--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    sp.set_defaults(func=cmd_watch)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
