#!/usr/bin/env python3
"""itermon test suite (BACKLOG #3 -- docs/specs/committed-test-suite.md).

Run with:
    python3 tests/run_tests.py
    python3 tests/run_tests.py --twice   # run the whole suite twice, in two
                                          # fresh subprocesses, and assert the
                                          # results are byte-identical (L6's
                                          # "green once, red twice" check)

Zero third-party dependencies -- stdlib `unittest` only, per the spec's hard
constraint. See tests/README.md for what's covered and why.

Every test in here is hermetic: PATH is pinned to tests/fake/ for the whole
run (see hermetic_env() below), so nothing in this file, or in iterm_ctl.py /
iterm_mcp.py as exercised through it, can reach a real osascript/iTerm2 or a
real `ps`. No test depends on wall-clock time, the current working directory,
the operator's real iTerm2 sessions, iterm_jobs.json, or activity.log.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
FAKE_DIR = os.path.join(TESTS_DIR, "fake")
MCP_PATH = os.path.join(REPO_ROOT, "iterm_mcp.py")


def _ensure_fakes_executable() -> None:
    """The fakes' `#!/usr/bin/env python3` shebang only fires if the exec bit
    survives however this tree got here -- and some sandboxes/checkouts strip
    it. Don't make the suite's greenness depend on someone remembering to
    `chmod +x` by hand: assert-and-fix it once, up front, so a fresh clone
    (Dida's machine included) is self-healing on this one axis."""
    for name in ("osascript", "ps"):
        path = os.path.join(FAKE_DIR, name)
        mode = os.stat(path).st_mode
        wanted = mode | 0o111
        if mode != wanted:
            os.chmod(path, wanted)


_ensure_fakes_executable()

sys.path.insert(0, REPO_ROOT)
import iterm_ctl  # noqa: E402  (import after sys.path setup, on purpose)

# A real, empty, never-written-to directory -- used as PATH when a test needs
# to prove that *neither* the fake *nor* anything real is reachable (spec
# section 3's safety property). A guaranteed-empty real directory is more
# portable than PATH="" (whose "search cwd" semantics for an empty PATH
# component are platform-dependent).
NO_TOOLS_DIR = tempfile.mkdtemp(prefix="itermon-no-tools-")

# The fakes are plain `#!/usr/bin/env python3` scripts. hermetic_env() pins
# PATH to FAKE_DIR *only*, so `env` has no way to resolve `python3` unless we
# also hand it the running interpreter's own directory -- appended AFTER
# FAKE_DIR (never before it, and never the ambient/system PATH), so a fake
# always wins a name collision and no real osascript/ps ever becomes
# reachable through this addition. Verified once, loudly, below.
INTERP_DIR = os.path.dirname(os.path.realpath(sys.executable))


def _assert_interp_dir_has_no_real_tools() -> None:
    """Guard the hermeticity invariant (spec section 3): the directory we're
    about to splice onto PATH so `env python3` resolves must not itself ship
    a real `osascript` or `ps`. On every sane Python install it won't -- but
    "must not" beats "probably doesn't", so this checks and refuses to run
    rather than silently widening the safe surface."""
    for name in ("osascript", "ps"):
        if os.path.exists(os.path.join(INTERP_DIR, name)):
            raise RuntimeError(
                f"refusing to run: interpreter directory {INTERP_DIR!r} contains a "
                f"real {name!r} binary; adding it to PATH (even after FAKE_DIR) "
                f"would violate the hermetic-PATH safety property (spec section 3)"
            )


_assert_interp_dir_has_no_real_tools()


# --------------------------------------------------------------------------- #
# Hermeticity plumbing (spec section 3)
# --------------------------------------------------------------------------- #
def _build_hermetic_env(path=FAKE_DIR, **extra) -> dict:
    """Single source of truth for a hermetic environment dict -- used both by
    hermetic_env() (which installs it into os.environ for in-process calls)
    and by anything that hands an env= dict straight to subprocess.Popen()
    (G6's real iterm_mcp.py child), so there is exactly one place that knows
    how to make PATH safe-but-python3-resolvable, not two copies that can
    drift out of sync.

    PATH is `path` (by default tests/fake/, which holds *only* the fake
    `osascript` and fake `ps`) followed by INTERP_DIR -- appended, never
    prepended, so a same-named fake always shadows anything INTERP_DIR could
    theoretically offer, and _assert_interp_dir_has_no_real_tools() has
    already proven INTERP_DIR holds no `osascript`/`ps` at all. The sole
    purpose of adding INTERP_DIR is letting `env python3` resolve for the
    fakes' own shebang line -- without it, `env` can't find *any* python3 and
    every fake-dependent call dies before the fake's logic ever runs.

    Passing path=NO_TOOLS_DIR (a real, empty, fake-free directory) is how the
    safety-property test proves a missing fake fails loudly instead of
    quietly falling through to something real -- INTERP_DIR being on PATH
    too doesn't change that, since INTERP_DIR contains no `osascript`/`ps`
    either.
    """
    env = {"PATH": os.pathsep.join([path, INTERP_DIR])}
    # A few harmless passthroughs some stdlib bits (tempfile, subprocess
    # itself) can want; never anything that widens PATH.
    ambient = os.environ
    for key in ("HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"):
        if key in ambient:
            env[key] = ambient[key]
    env.update(extra)
    return env


@contextlib.contextmanager
def hermetic_env(path=FAKE_DIR, **extra):
    """Replace os.environ for the duration of the block with
    _build_hermetic_env(path, **extra). iterm_ctl.py's subprocess.run() calls
    inherit this environment, so they can only ever reach the fakes (or
    nothing), never a real osascript or ps."""
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(_build_hermetic_env(path=path, **extra))
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def write_tmp(content: str) -> str:
    """Write `content` to a fresh temp file and return its path. Caller does
    not need to clean up -- these are single-run scratch files under the
    system temp dir, never the working tree."""
    fd, path = tempfile.mkstemp(prefix="itermon-test-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# --------------------------------------------------------------------------- #
# G1 -- resolve_targets: the targeting contract (pure function, no fake needed)
# --------------------------------------------------------------------------- #
class G1ResolveTargets(unittest.TestCase):
    def setUp(self):
        self.sessions = [
            iterm_ctl.Session("1.1.1", "AAAA1111", "/dev/ttys001", "shell one"),
            iterm_ctl.Session("1.1.2", "BBBB2222", "/dev/ttys002", "daily-log watcher"),
            iterm_ctl.Session("2.1.1", "CCCC3333", "/dev/ttys003", "vim session.app"),
        ]

    def test_exact_index_matches_only_that_session(self):
        got = iterm_ctl.resolve_targets(self.sessions, "2.1.1", False)
        self.assertEqual([s.id for s in got], ["CCCC3333"])

    def test_index_shaped_string_matching_nothing_returns_empty(self):
        got = iterm_ctl.resolve_targets(self.sessions, "9.9.9", False)
        self.assertEqual(got, [])

    def test_id_prefix_case_insensitive_and_not_substring(self):
        got = iterm_ctl.resolve_targets(self.sessions, "id:aaaa", False)
        self.assertEqual([s.id for s in got], ["AAAA1111"])
        # "bb22" is a substring of BBBB2222 but not a *prefix* -> no match.
        got = iterm_ctl.resolve_targets(self.sessions, "id:bb22", False)
        self.assertEqual(got, [])

    def test_tty_exact_and_suffix_match(self):
        got = iterm_ctl.resolve_targets(self.sessions, "tty:/dev/ttys002", False)
        self.assertEqual([s.id for s in got], ["BBBB2222"])
        got = iterm_ctl.resolve_targets(self.sessions, "tty:ttys002", False)
        self.assertEqual([s.id for s in got], ["BBBB2222"])

    def test_name_prefix_is_a_regex_case_insensitive(self):
        got = iterm_ctl.resolve_targets(self.sessions, "name:^DAILY", False)
        self.assertEqual([s.id for s in got], ["BBBB2222"])
        # anchored regex that shouldn't match anything
        got = iterm_ctl.resolve_targets(self.sessions, "name:^DAILY$", False)
        self.assertEqual(got, [])

    def test_bare_string_is_case_insensitive_substring_not_regex(self):
        got = iterm_ctl.resolve_targets(self.sessions, "DAILY", False)
        self.assertEqual([s.id for s in got], ["BBBB2222"])
        # "." is a regex metachar; as a *substring* it must not act as a
        # wildcard. "a.b" is not a literal substring of "shell one" or any
        # other test session name, so this must find nothing.
        got = iterm_ctl.resolve_targets(self.sessions, "a.b", False)
        self.assertEqual(got, [])
        # The assertion above happens not to discriminate implementations:
        # none of self.sessions' names contain "a.b" AS A REGEX MATCH either
        # (no name has "a", then any one character, then "b"), so a bare
        # string handler that quietly does re.search(target, name) instead
        # of a literal substring check would pass it too. This session name
        # is a purpose-built trap: "azb" is not the literal substring "a.b"
        # (different middle character), but it DOES match the regex "a.b"
        # (which treats "." as "any character") -- so only the correct,
        # literal-substring implementation returns no match here.
        regex_trap = [
            iterm_ctl.Session("4.1.1", "DDDD4444", "/dev/ttys004", "job-azbeta")
        ]
        got = iterm_ctl.resolve_targets(regex_trap, "a.b", False)
        self.assertEqual(got, [])

    def test_all_flag_returns_everyone_and_ignores_target(self):
        got = iterm_ctl.resolve_targets(self.sessions, "this-matches-nothing", True)
        self.assertEqual(got, self.sessions)

    def test_none_target_no_all_flag_returns_empty(self):
        got = iterm_ctl.resolve_targets(self.sessions, None, False)
        self.assertEqual(got, [])

    def test_index_shape_is_exactly_three_dotted_numbers(self):
        # "2.1" (two parts) and "2.1.1.1" (four parts) must NOT take the
        # index-equality branch, even when a session's .index literally
        # equals the target string -- if they took the index branch this
        # would find that session; pinned here to prove they don't.
        two_part = [iterm_ctl.Session("2.1", "X", "/dev/ttysA", "no-match-here")]
        got = iterm_ctl.resolve_targets(two_part, "2.1", False)
        self.assertEqual(got, [])

        four_part = [iterm_ctl.Session("2.1.1.1", "Y", "/dev/ttysB", "no-match-here")]
        got = iterm_ctl.resolve_targets(four_part, "2.1.1.1", False)
        self.assertEqual(got, [])


# --------------------------------------------------------------------------- #
# G2 -- as_str AppleScript escaping
# --------------------------------------------------------------------------- #
def _oracle_as_str(value: str) -> str:
    """Independent, character-by-character reference implementation of the
    escaping contract (double-quoted AppleScript string literal: backslash
    escaped first, then the double quote). Deliberately written differently
    from iterm_ctl.as_str's two chained .replace() calls, so this isn't just
    the same code asserting against itself."""
    out = ['"']
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


class G2AsStrEscaping(unittest.TestCase):
    def test_plain_string_wrapped_in_quotes(self):
        self.assertEqual(iterm_ctl.as_str("hello"), _oracle_as_str("hello"))
        self.assertEqual(iterm_ctl.as_str("hello"), '"hello"')

    def test_embedded_quote_escaped(self):
        value = 'she said "hi"'
        self.assertEqual(iterm_ctl.as_str(value), _oracle_as_str(value))

    def test_embedded_backslash_escaped_before_the_quote(self):
        # Order matters: escape backslashes BEFORE quotes, or a value that
        # already contains a literal backslash-quote pair gets mangled.
        # This input exercises exactly that interaction.
        value = 'C:\\path\\to\\"file"'
        self.assertEqual(iterm_ctl.as_str(value), _oracle_as_str(value))

    def test_command_with_both_survives_send_text_intact(self):
        text = 'echo "a\\b" && printf "%s\\n" done'
        session = iterm_ctl.Session("1.1.1", "SID-XYZ", "/dev/ttys009", "s")
        log = write_tmp("")
        with hermetic_env(ITERMON_FAKE_LOG=log):
            iterm_ctl.send_text(session, text, enter=True)
        script = _last_logged_script(log)
        self.assertIn(iterm_ctl.as_str(text), script)


def _last_logged_script(log_path: str) -> str:
    with open(log_path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    return json.loads(lines[-1])["script"]


# --------------------------------------------------------------------------- #
# G3 -- list_sessions parsing
# --------------------------------------------------------------------------- #
class G3ListSessions(unittest.TestCase):
    def test_well_formed_listing_parses_three_sessions(self):
        SEP = iterm_ctl.SEP
        listing = (
            f"1.1.1{SEP}ID-ONE{SEP}/dev/ttys001{SEP}bash\n"
            "\n"  # blank line -- must be skipped, not crash
            f"1.1.2{SEP}ID-TWO{SEP}/dev/ttys002{SEP}daily.log watcher\n"
            f"bad{SEP}row\n"  # only 2 fields after split -> skipped
            f"2.1.1{SEP}ID-THREE{SEP}/dev/ttys003{SEP}zsh\n"
        )
        listing_path = write_tmp(listing)
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            sessions = iterm_ctl.list_sessions()
        self.assertEqual(len(sessions), 3)
        self.assertEqual(
            [(s.index, s.id, s.tty, s.name) for s in sessions],
            [
                ("1.1.1", "ID-ONE", "/dev/ttys001", "bash"),
                # name containing a space AND a dot must survive intact --
                # the parser splits on SEP, not whitespace.
                ("1.1.2", "ID-TWO", "/dev/ttys002", "daily.log watcher"),
                ("2.1.1", "ID-THREE", "/dev/ttys003", "zsh"),
            ],
        )

    def test_empty_output_gives_empty_list_and_print_table_does_not_raise(self):
        with hermetic_env():  # ITERMON_FAKE_LISTING unset -> fake returns ""
            sessions = iterm_ctl.list_sessions()
        self.assertEqual(sessions, [])

        buf = _capture_stdout(lambda: iterm_ctl.print_table(sessions))
        self.assertIn("No iTerm2 sessions found", buf)

    def test_fake_absent_fails_loudly_instead_of_reaching_a_real_osascript(self):
        # Safety property from spec section 3: with neither the fake nor a
        # real osascript reachable on PATH, list_sessions() must raise, never
        # silently return something (which would mean it fell through to a
        # real binary somewhere on the ambient PATH).
        with hermetic_env(path=NO_TOOLS_DIR):
            with self.assertRaises(Exception):
                iterm_ctl.list_sessions()


def _capture_stdout(fn) -> str:
    import io

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn()
    finally:
        sys.stdout = old
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# G4 -- send_text / read_contents generated AppleScript
# --------------------------------------------------------------------------- #
class G4SendReadAppleScript(unittest.TestCase):
    def setUp(self):
        self.session = iterm_ctl.Session(
            "3.2.1", "SESSION-ID-4242", "/dev/ttys044", "worker"
        )

    def test_send_text_enter_true_has_newline_yes(self):
        log = write_tmp("")
        with hermetic_env(ITERMON_FAKE_LOG=log):
            iterm_ctl.send_text(self.session, "git status", enter=True)
        script = _last_logged_script(log)
        self.assertIn("newline yes", script)

    def test_send_text_enter_false_has_newline_no(self):
        log = write_tmp("")
        with hermetic_env(ITERMON_FAKE_LOG=log):
            iterm_ctl.send_text(self.session, "git status", enter=False)
        script = _last_logged_script(log)
        self.assertIn("newline no", script)

    def test_send_text_targets_by_id_not_index(self):
        log = write_tmp("")
        with hermetic_env(ITERMON_FAKE_LOG=log):
            iterm_ctl.send_text(self.session, "pwd", enter=True)
        script = _last_logged_script(log)
        self.assertIn(f"(id of s) is {iterm_ctl.as_str(self.session.id)}", script)
        self.assertNotIn(self.session.index, script)

    def test_read_contents_returns_fakes_canned_screen_text(self):
        screen_path = write_tmp("$ echo hi\nhi\n")
        with hermetic_env(ITERMON_FAKE_SCREEN=screen_path):
            got = iterm_ctl.read_contents(self.session)
        self.assertEqual(got, "$ echo hi\nhi\n")


# --------------------------------------------------------------------------- #
# G5 -- _annotate_jobs foreground-process selection
# --------------------------------------------------------------------------- #
class G5AnnotateJobs(unittest.TestCase):
    def _run(self, ps_output: str):
        session = iterm_ctl.Session("1.1.1", "SID", "/dev/ttys007", "s")
        ps_path = write_tmp(ps_output)
        with hermetic_env(ITERMON_FAKE_PS_OUTPUT=ps_path):
            iterm_ctl._annotate_jobs([session])
        return session

    def test_the_plus_row_wins(self):
        s = self._run("100 Ss   bash\n101 R+   /usr/bin/vim\n")
        self.assertEqual(s.job, "vim")

    def test_the_last_plus_row_wins_when_several(self):
        s = self._run(
            "100 S+   /bin/one\n"
            "101 S+   /bin/two\n"
            "102 S+   /bin/three\n"
        )
        self.assertEqual(s.job, "three")

    def test_comm_path_is_basenamed(self):
        s = self._run("100 R+   /usr/bin/vim\n")
        self.assertEqual(s.job, "vim")

    def test_no_plus_row_leaves_job_empty(self):
        s = self._run("100 Ss   bash\n101 Sl   zsh\n")
        self.assertEqual(s.job, "")

    def test_ps_failing_entirely_leaves_job_empty_no_exception(self):
        session = iterm_ctl.Session("1.1.1", "SID", "/dev/ttys007", "s")
        with hermetic_env(path=NO_TOOLS_DIR):  # no ps binary reachable at all
            iterm_ctl._annotate_jobs([session])  # must not raise
        self.assertEqual(session.job, "")


# --------------------------------------------------------------------------- #
# G6 -- MCP wire protocol, as a real subprocess over stdio
# --------------------------------------------------------------------------- #
class MCPClient:
    """Thin JSON-RPC-over-stdio driver for a real `python3 iterm_mcp.py`
    subprocess. One reply line is read per request that carries an `id`;
    notifications (no `id`) are expected to produce no reply line at all --
    proven by sending a well-formed follow-up request and checking its reply
    lines up by id (see test_notification_gets_no_reply)."""

    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, MCP_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

    def send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def send_raw(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def recv(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise EOFError("iterm_mcp.py closed stdout unexpectedly")
        return json.loads(line)

    def close(self):
        # Don't close stdin ourselves first: Popen.communicate(input=None)
        # already flushes-then-closes self.stdin as its first step (that's
        # what signals EOF to iterm_mcp.py's read loop so it exits), and
        # calling .close() twice makes communicate()'s own .flush() raise
        # `ValueError: I/O operation on closed file` on an already-closed
        # stream. Let communicate() own the close.
        out, err = self.proc.communicate(timeout=10)
        return self.proc.returncode, err


class G6MCPWireProtocol(unittest.TestCase):
    """Re-expresses Dida's 2026-07-29 transcript
    (docs/worklog/usagemonitoring-tester/2026-07-29.md) as assertions, plus
    the list_sessions/read_screen cases the spec adds on top."""

    @classmethod
    def setUpClass(cls):
        listing = (
            f"1.1.1{iterm_ctl.SEP}ID-A{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}alpha\n"
            f"1.1.2{iterm_ctl.SEP}ID-B{iterm_ctl.SEP}/dev/ttys002{iterm_ctl.SEP}beta\n"
        )
        cls.listing_path = write_tmp(listing)
        env = _build_hermetic_env(ITERMON_FAKE_LISTING=cls.listing_path)
        cls.client = MCPClient(env)

    @classmethod
    def tearDownClass(cls):
        # Safety net only: normally test_99_clean_exit (the last test method,
        # by the class's sort order) has already closed the client and set
        # cls.exit_code below. tearDownClass runs *after every test method*,
        # not before test_99 -- so it must not assume test_99 ran (an earlier
        # test could have errored and short-circuited the run) and must not
        # double-close a client that test_99 already closed.
        if getattr(cls, "exit_code", None) is None:
            cls.exit_code, cls.stderr = cls.client.close()

    def test_01_initialize(self):
        self.client.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        reply = self.client.recv()
        self.assertEqual(reply["id"], 1)
        result = reply["result"]
        self.assertEqual(result["serverInfo"]["name"], "itermon")
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])

    def test_02_notification_gets_no_reply(self):
        self.client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # No id -> no reply line for this message. Prove it by sending a
        # well-formed request next and checking the very next line off
        # stdout is *that* request's reply, not a stray one.
        self.client.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        reply = self.client.recv()
        self.assertEqual(reply["id"], 2)
        self.assertEqual(reply["result"], {})

    def test_03_tools_list(self):
        self.client.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        reply = self.client.recv()
        tools = reply["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"list_sessions", "read_screen", "send_command"})
        for t in tools:
            self.assertIn("inputSchema", t)

    def test_04_unknown_tool_is_soft_error(self):
        self.client.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "frobnicate"},
            }
        )
        reply = self.client.recv()
        self.assertIn("result", reply)
        self.assertNotIn("error", reply)
        result = reply["result"]
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("frobnicate", text)
        for name in ("list_sessions", "read_screen", "send_command"):
            self.assertIn(name, text)

    def test_05_tools_call_missing_name_key_same_soft_branch(self):
        self.client.send(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}}
        )
        reply = self.client.recv()
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("None", reply["result"]["content"][0]["text"])

    def test_06_tools_call_null_name_identical_to_missing(self):
        self.client.send(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": None},
            }
        )
        reply = self.client.recv()
        # Same shape as #5 (missing name key) -- HANDLERS.get(None) is None
        # either way, per the PM's documented edge-case decision.
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("None", reply["result"]["content"][0]["text"])

    def test_07_unknown_method_is_hard_error(self):
        self.client.send({"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
        reply = self.client.recv()
        self.assertEqual(reply["error"]["code"], -32601)

    def test_08_malformed_json_line_no_reply_no_crash(self):
        self.client.send_raw("this is not valid json at all {{{")
        self.client.send({"jsonrpc": "2.0", "id": 8, "method": "ping"})
        reply = self.client.recv()
        self.assertEqual(reply["id"], 8)
        self.assertEqual(reply["result"], {})

    def test_09_tools_call_list_sessions_with_fake_on_path(self):
        self.client.send(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "list_sessions", "arguments": {}},
            }
        )
        reply = self.client.recv()
        result = reply["result"]
        self.assertFalse(result["isError"])
        rows = json.loads(result["content"][0]["text"])
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["id"] for r in rows}, {"ID-A", "ID-B"})

    def test_10_tools_call_read_screen_no_match_is_soft_error(self):
        self.client.send(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "read_screen",
                    "arguments": {"target": "no-such-session-xyz"},
                },
            }
        )
        reply = self.client.recv()
        result = reply["result"]
        self.assertTrue(result["isError"])
        self.assertIn("no session matches target", result["content"][0]["text"])

    def test_99_clean_exit(self):
        # Last test method by sort order (test_01 .. test_10, then test_99):
        # every other test has sent its request by now, so this is the right
        # moment to close stdin (signals EOF to iterm_mcp.py's read loop) and
        # assert the process exits clean. Stash the result on the class so
        # tearDownClass's safety net doesn't try to close an already-closed
        # client.
        self.__class__.exit_code, self.__class__.stderr = self.client.close()
        self.assertEqual(self.__class__.exit_code, 0)
        self.assertEqual(self.__class__.stderr, "")


# --------------------------------------------------------------------------- #
# --twice: determinism harness (spec section 4, G7)
# --------------------------------------------------------------------------- #
def _run_once():
    proc = subprocess.run(
        [sys.executable, os.path.realpath(__file__)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _summary_line(stderr: str) -> str:
    # unittest's TextTestRunner prints "Ran N tests in X.XXXs" followed by
    # "OK" / "FAILED (failures=.., errors=..)" as its last two non-blank
    # lines. The elapsed-time digits are, deliberately, wall-clock and WILL
    # differ between the two runs -- strip them out so the comparison is
    # test count + final result only, not timing noise that would make an
    # actually-deterministic suite look like it mismatched.
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    ran_line = next((ln for ln in lines if ln.startswith("Ran ")), "")
    ran_line = re.sub(r"in [\d.]+s", "in Xs", ran_line)
    result_line = lines[-1] if lines else ""
    return f"{ran_line} | {result_line}"


def run_twice_and_compare() -> int:
    print("Running the suite twice, back to back, in fresh subprocesses...")
    code1, out1, err1 = _run_once()
    code2, out2, err2 = _run_once()
    summary1, summary2 = _summary_line(err1), _summary_line(err2)
    print(f"run 1: exit={code1}\n{summary1}\n")
    print(f"run 2: exit={code2}\n{summary2}\n")
    if code1 != code2 or summary1 != summary2:
        print("--twice: MISMATCH between run 1 and run 2 -- suite is not deterministic.")
        return 1
    print(f"--twice: identical exit code ({code1}) and identical summary both runs. OK")
    return 0 if code1 == 0 else code1


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _load_suite() -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (
        G1ResolveTargets,
        G2AsStrEscaping,
        G3ListSessions,
        G4SendReadAppleScript,
        G5AnnotateJobs,
        G6MCPWireProtocol,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return suite


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--twice" in argv:
        return run_twice_and_compare()

    with hermetic_env():
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(_load_suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
