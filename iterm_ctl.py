#!/usr/bin/env python3
"""
iterm_ctl - monitor and control terminal sessions from the command line.

Speaks iTerm2's AppleScript interface on macOS, and tmux everywhere else (or
on macOS too, if you ask for it) -- see docs/specs/tmux-universal-backend.md.
Needs no extra Python packages. On macOS/iTerm2 the first run may trigger an
"allow Terminal to control iTerm2" automation prompt; approve it once. The
tmux backend needs no such permission, but does need a tmux server.

Examples:
    ./iterm_ctl.py list
    ./iterm_ctl.py send 2.1.1 "git status"
    ./iterm_ctl.py send index:2.1.1 "git status"
    ./iterm_ctl.py send id:A0205 "ls -la"
    ./iterm_ctl.py send name:daily "echo hi"
    ./iterm_ctl.py send --all "pwd" --yes
    ./iterm_ctl.py read 2.1.1
    ./iterm_ctl.py watch --interval 2
    ./iterm_ctl.py list --backend tmux
    ./iterm_ctl.py backends
"""
import argparse
import os
import re
import shutil
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
        self.index = index          # human address: iTerm2 "2.1.1" (window.tab.session,
                                     # 1-based, positional -- renumbers on window reorder)
                                     # or tmux "session:window.pane" (e.g. "work:2.0").
                                     # Stability is a backend property -- see CAPABILITIES
                                     # "stable_ids" below, not something this field itself
                                     # promises.
        self.id = sid                # the machine handle: iTerm2 session UUID, or tmux's
                                      # server-lifetime-stable "%N" pane id. Both backends
                                      # send and read exclusively by this field, never index.
        self.tty = tty               # /dev/ttysNNN
        self.name = name             # iTerm2 session title, or tmux window name
        self.job = ""                # foreground process (filled in later)
        self.cwd = ""                # foreground process cwd (best effort; iTerm2 leaves
                                      # this empty in practice, tmux populates it -- see
                                      # CAPABILITIES "cwd")


# Field separator unlikely to appear in a title.
SEP = "\x1f"


def list_sessions() -> list[Session]:
    """List every session known to the active backend (spec section 2)."""
    return _get_backend().list_sessions()


def _annotate_jobs(sessions: list[Session]) -> None:
    """Fill in foreground job (and cwd when available) for each session via ps/lsof.

    iTerm2-backend-only: the tmux backend gets its job column directly from
    tmux (#{pane_current_command}) and never calls this (spec section 3.1)."""
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
    """Backend-neutral targeting (spec section 2.2): operates on Session value
    objects only, never on a terminal, so both backends are held to the same
    identity semantics in exactly one place."""
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
    if target.startswith("index:"):
        # docs/specs/stable-job-targets-and-zero-match-failure.md S1: the
        # explicit spelling of the bare-index fast path above (branch 1).
        # Same match semantics -- exact string equality on Session.index,
        # no substring fallback -- so both spellings are interchangeable.
        # Before this branch existed, "index:2.1.1" fell all the way through
        # to the bare-substring fallback below, matched nothing, and
        # returned [] silently: the exact bug class this spec exists to
        # kill (AC-1/AC-2). Placed with the other explicit prefixes, before
        # the exact-match and bare-substring fallbacks, per spec ordering.
        needle = target[len("index:"):]
        return [s for s in sessions if s.index == needle]
    # Exact match on index or id (spec section 5.1, BACKLOG 0c) -- lets a tmux
    # user address a pane bare by its human "session:window.pane" address
    # (e.g. "work:2.0") or its stable "%N" pane id, the way tmux users already
    # think about panes. Deliberately placed after the id:/tty:/name: prefix
    # branches and before the bare-substring fallback (spec's ordering).
    exact = [s for s in sessions if s.index == target or s.id == target]
    if exact:
        return exact
    # bare string -> treat as name substring
    return [s for s in sessions if target.lower() in s.name.lower()]


# --------------------------------------------------------------------------- #
# iTerm2 backend
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


class Iterm2Backend:
    """The original, unmodified iTerm2/AppleScript engine, moved behind the
    backend seam (spec section 2) without changing a single generated
    AppleScript string, error path, or return value. See docs/specs/
    tmux-universal-backend.md section 2.1 -- this class's three methods are
    the pre-existing module-level list_sessions/send_text/read_contents
    bodies, verbatim."""

    name = "iterm2"
    empty_message = "No iTerm2 sessions found (is iTerm2 running?)."
    CAPABILITIES = {
        "stable_ids": False,
        "job_column": True,
        "job_via_ps": True,
        "cwd": False,
        "scrollback": False,
        "needs_os_permission": True,
        "cross_platform": False,
    }

    def list_sessions(self) -> list[Session]:
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

    def send_text(self, session: Session, text: str, enter: bool) -> None:
        newline = "yes" if enter else "no"
        body = f"          tell s to write text {as_str(text)} newline {newline}\n          return"
        run_osascript(_for_session(session.id, body))

    def read_contents(self, session: Session) -> str:
        body = "          return (contents of s)"
        return run_osascript(_for_session(session.id, body))


# --------------------------------------------------------------------------- #
# tmux backend (spec section 3, BACKLOG 0c/0e)
# --------------------------------------------------------------------------- #
def _require_tmux() -> None:
    if shutil.which("tmux") is None:
        raise RuntimeError(
            "tmux backend selected but 'tmux' was not found on PATH. Install "
            "it (e.g. `brew install tmux` on macOS, `apt install tmux` on "
            "Debian/Ubuntu) or choose a different backend with "
            "--backend iterm2 / ITERMON_BACKEND=iterm2."
        )


def _tmux_env() -> dict:
    """Env for every `tmux` subprocess call, forcing a UTF-8 locale
    regardless of the caller's own environment.

    Verified live (not in the spike -- found while building this): tmux's -F
    format engine decides whether its connecting client is UTF-8-capable by
    string-matching "UTF-8" against LC_ALL/LC_CTYPE/LANG -- it does NOT
    validate the value against an installed locale, so a fixed sentinel like
    "C.UTF-8" works even on a system that has never installed that locale
    (macOS never ships it; this is not a "real" locale switch, just a string
    tmux greps for). Without this, a minimal environment -- no LANG at all,
    or a non-UTF-8 one, exactly what a bare SSH session or a cron job can
    hand a script -- makes tmux silently replace SEP ("\\x1f", a C0 control
    byte) with "_" in every #{...} field of list-panes' output, which
    collapses the 6-field parse to 1 field per line and makes the tmux
    backend appear to find zero sessions on precisely the stripped-down,
    non-interactive environments this feature exists for. Only LC_ALL is
    forced; PATH, TMUX_TMPDIR, HOME, and everything else pass through
    unchanged."""
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    return env


class TmuxBackend:
    """tmux backend -- every invocation here is exactly what
    docs/spikes/2026-08-06-terminal-universality-spike.md verified live on
    tmux 3.6a, made binding by docs/specs/tmux-universal-backend.md section 3.
    Do not "improve" these argv lists; -l -- is fail-closed and
    non-negotiable (spec 3.2, BACKLOG 0e)."""

    name = "tmux"
    empty_message = "No tmux panes found (is a tmux server running?)."
    CAPABILITIES = {
        "stable_ids": True,
        "job_column": True,
        "job_via_ps": False,
        "cwd": True,
        "scrollback": True,
        "needs_os_permission": False,
        "cross_platform": True,
    }

    def list_sessions(self) -> list[Session]:
        _require_tmux()
        fmt = SEP.join(
            [
                "#{session_name}:#{window_index}.#{pane_index}",
                "#{pane_id}",
                "#{pane_tty}",
                "#{window_name}",
                "#{pane_current_command}",
                "#{pane_current_path}",
            ]
        )
        proc = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", fmt],
            capture_output=True,
            text=True,
            env=_tmux_env(),
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            # "no server running" is not an error (spec 3.1) -- it maps to an
            # empty session list, the same way iTerm2 with no windows does.
            # Verified live, two distinct real messages for the same "there is
            # no server" condition depending on whether this socket path was
            # ever used before: "no server running on <path>" (a stale/killed
            # server's socket file still exists) and "error connecting to
            # <path> (No such file or directory)" (the socket directory was
            # never created at all -- e.g. a fresh $TMUX_TMPDIR). Both map here.
            low = stderr.lower()
            if "no server running" in low or "no such file or directory" in low:
                return []
            raise RuntimeError(stderr or "tmux list-panes failed")
        sessions = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(SEP)
            if len(parts) < 6:
                continue
            index, pane_id, tty, name, job, cwd = parts[:6]
            s = Session(index, pane_id, tty, name)
            s.job = job
            s.cwd = cwd
            sessions.append(s)
        return sessions

    def send_text(self, session: Session, text: str, enter: bool) -> None:
        # Empty text with enter=False is a no-op -- do not invoke tmux at all
        # (spec 3.2; preserves iterm_web.py's "send a bare newline" call with
        # text="" enter=True, which must still fire the Enter).
        if text == "" and not enter:
            return
        _require_tmux()
        if text != "":
            # -l -- is mandatory: send-keys without -l reads its argument as a
            # KEY NAME, not text (spike-proven: 'C-c' sends Ctrl-C, nothing is
            # typed). The user's text is always a single argv element, never
            # shell-interpolated.
            proc = subprocess.run(
                ["tmux", "send-keys", "-t", session.id, "-l", "--", text],
                capture_output=True,
                text=True,
                env=_tmux_env(),
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "tmux send-keys failed")
        if enter:
            proc = subprocess.run(
                ["tmux", "send-keys", "-t", session.id, "Enter"],
                capture_output=True,
                text=True,
                env=_tmux_env(),
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "tmux send-keys failed")

    def read_contents(self, session: Session) -> str:
        _require_tmux()
        proc = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session.id],
            capture_output=True,
            text=True,
            env=_tmux_env(),
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "tmux capture-pane failed")
        return proc.stdout


# --------------------------------------------------------------------------- #
# Backend selection (spec section 2.3)
# --------------------------------------------------------------------------- #
_BACKEND_CLASSES = {"iterm2": Iterm2Backend, "tmux": TmuxBackend}
_VALID_BACKENDS = tuple(_BACKEND_CLASSES)  # ("iterm2", "tmux"), in this order

# Set by main() from the CLI's --backend flag. None/"auto" both mean "not
# pinned by the flag" -- the next source (ITERMON_BACKEND, then platform
# default) decides. Never touched outside main(); library callers (iterm_mcp.py,
# iterm_web.py, tests) select a backend via $ITERMON_BACKEND only, exactly as
# spec 2.3 describes, with no code change and no import of this name.
_CLI_BACKEND = None


def _set_cli_backend(value: str) -> None:
    global _CLI_BACKEND
    _CLI_BACKEND = value


def _resolve_backend_name() -> str:
    """Resolution order (spec 2.3): explicit --backend flag, then
    $ITERMON_BACKEND, then the platform default (darwin -> iterm2, else
    tmux). 'auto' -- whether it's the flag's default, an explicit
    `--backend auto`, or `$ITERMON_BACKEND=auto` -- always falls through to
    the next source; it is a resolution mode, not a fourth concrete backend.
    An empty or whitespace-only $ITERMON_BACKEND is treated the same as
    unset (falls through), since that's how wrapper scripts/CI commonly
    express "not set". An unknown, non-empty, non-'auto' name from either
    source is fail-closed: a RuntimeError naming the valid choices, never a
    silent fallback. Re-resolved on every call (cheap: no I/O) rather than
    cached once per process, so it stays correct across hermetic_env()'s
    per-test os.environ swaps instead of depending on call order."""
    if _CLI_BACKEND in _VALID_BACKENDS:
        return _CLI_BACKEND
    env = os.environ.get("ITERMON_BACKEND")
    if env is not None:
        env = env.strip()
        # Empty/whitespace-only and 'auto' both mean "not pinned by the env
        # var either" -- fall through to the platform default, same as the
        # flag's own 'auto'. An empty string is how wrapper scripts/CI commonly
        # express "unset" (ITERMON_BACKEND=$SOMETHING with $SOMETHING unset),
        # and failing hard on that -- or on the literal value this module's own
        # error message and docstring advertise as valid -- would turn this
        # additive release into a breaking one for anyone who follows either.
        # A genuine typo (non-empty, not 'auto', not a known backend) still
        # fails closed below: silently falling back to the other backend on a
        # misspelled name would be worse than a loud error.
        if env and env != "auto":
            if env not in _VALID_BACKENDS:
                raise RuntimeError(
                    f"unknown backend {env!r} in $ITERMON_BACKEND -- valid backends: "
                    + ", ".join(_VALID_BACKENDS)
                    + " (or 'auto')"
                )
            return env
    return "iterm2" if sys.platform == "darwin" else "tmux"


def _get_backend():
    return _BACKEND_CLASSES[_resolve_backend_name()]()


def _iterm2_available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def send_text(session: Session, text: str, enter: bool) -> None:
    """Send `text` to `session` via the active backend (spec section 2)."""
    _get_backend().send_text(session, text, enter)


def read_contents(session: Session) -> str:
    """Read `session`'s visible screen via the active backend (spec section 2)."""
    return _get_backend().read_contents(session)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def print_table(sessions: list[Session], empty_message=None) -> None:
    if not sessions:
        # empty_message defaults to the pre-existing iTerm2 string so every
        # existing caller (this module's own cmd_list/cmd_watch used to hardcode
        # nothing else, and the test suite calls print_table(sessions) with no
        # second argument) sees byte-identical output. Backend-aware callers
        # pass their backend's own empty_message (spec section 2.2/4).
        print(empty_message or "No iTerm2 sessions found (is iTerm2 running?).")
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
    print_table(list_sessions(), empty_message=_get_backend().empty_message)


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
    backend = _get_backend()
    label = "iTerm2" if backend.name == "iterm2" else backend.name
    try:
        while True:
            sessions = list_sessions()
            sys.stdout.write("\033[2J\033[H")  # clear screen, home cursor
            print(f"{label} sessions @ {time.strftime('%H:%M:%S')}  (Ctrl-C to stop)\n")
            print_table(sessions, empty_message=backend.empty_message)
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


def cmd_backends(args):
    current = _resolve_backend_name()
    availability = {"iterm2": _iterm2_available(), "tmux": _tmux_available()}

    print("Backends on this machine:")
    for name in _VALID_BACKENDS:
        picked = "  <- auto would pick this here" if name == current else ""
        avail = "available" if availability[name] else "not available"
        print(f"  {name:<7} {avail}{picked}")
    print()

    print("Capabilities:")
    keys = list(Iterm2Backend.CAPABILITIES)  # fixed order, spec section 4's table
    name_w = max(len(k) for k in keys)
    print(f"  {'CAPABILITY':<{name_w}}  ITERM2  TMUX")
    for key in keys:
        i_val = "yes" if Iterm2Backend.CAPABILITIES[key] else "no"
        t_val = "yes" if TmuxBackend.CAPABILITIES[key] else "no"
        print(f"  {key:<{name_w}}  {i_val:<6}  {t_val}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _add_backend_flag(sp) -> None:
    sp.add_argument(
        "--backend",
        choices=list(_VALID_BACKENDS) + ["auto"],
        default="auto",
        help="backend to use: auto (default; macOS -> iterm2, else tmux), iterm2, or tmux",
    )


def build_parser():
    p = argparse.ArgumentParser(
        description="Monitor and control terminal sessions (iTerm2 on macOS, tmux everywhere else)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", aliases=["ls"], help="list all sessions")
    _add_backend_flag(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("send", help="send a command to matching session(s)")
    sp.add_argument("target", nargs="?", help="index (2.1.1), index:VALUE, id:PREFIX, tty:NNN, name:REGEX, or substring")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="command text to send")
    sp.add_argument("--all", action="store_true", help="send to every session")
    sp.add_argument("--no-enter", action="store_true", help="type without pressing Enter")
    sp.add_argument("--yes", "-y", action="store_true", help="skip confirmation for multi-session sends")
    _add_backend_flag(sp)
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("read", help="print visible screen contents of matching session(s)")
    sp.add_argument("target", nargs="?", help="index, index:, id:, tty:, name:, or substring")
    sp.add_argument("--all", action="store_true", help="read every session")
    _add_backend_flag(sp)
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("watch", help="live-refreshing session monitor")
    sp.add_argument("--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    _add_backend_flag(sp)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser(
        "backends",
        help="show available backends, their capabilities, and what 'auto' picks here",
    )
    sp.set_defaults(func=cmd_backends)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    backend_flag = getattr(args, "backend", None)
    if backend_flag:
        _set_cli_backend(backend_flag)
    try:
        return args.func(args) or 0
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
