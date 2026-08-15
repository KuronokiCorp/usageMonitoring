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
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

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
    for name in ("osascript", "ps", "tmux"):
        path = os.path.join(FAKE_DIR, name)
        mode = os.stat(path).st_mode
        wanted = mode | 0o111
        if mode != wanted:
            os.chmod(path, wanted)


_ensure_fakes_executable()

sys.path.insert(0, REPO_ROOT)
import iterm_ctl  # noqa: E402  (import after sys.path setup, on purpose)
import iterm_web  # noqa: E402  (G10 -- iterm_web.py's scheduler/API, spec
                   # docs/specs/stable-job-targets-and-zero-match-failure.md).
                   # Importing the module has no side effects on its own --
                   # it defines functions/classes/the PAGE string only; the
                   # server, the scheduler thread, and load_recent_log() all
                   # run from main(), never from import. Never touches the
                   # real iterm_jobs.json/activity.log: G10 monkeypatches
                   # iterm_web.JOBS_FILE/ACTIVITY_FILE to a throwaway temp
                   # path in its own setUp/tearDown before any test runs.

# A real, empty, never-written-to directory -- used as PATH when a test needs
# to prove that *neither* the fake *nor* anything real is reachable (spec
# section 3's safety property). A guaranteed-empty real directory is more
# portable than PATH="" (whose "search cwd" semantics for an empty PATH
# component are platform-dependent).
NO_TOOLS_DIR = tempfile.mkdtemp(prefix="itermon-no-tools-")

# The real, ambient $PATH, captured once here -- at import time, before
# main() (bottom of this file) ever wraps the whole test run in one outer
# hermetic_env() that pins PATH to FAKE_DIR for the duration. G9TmuxBackendLive
# needs the *real* tmux, reachable on the *real* PATH, for its raw
# subprocess.run() setup/teardown calls (new-session/kill-window/kill-server);
# `dict(os.environ)` captured *inside* setUpClass would instead see whatever
# hermetic PATH the outer wrapping had already installed by then, and hand
# tests/fake/tmux a real socket to argue with. hermetic_env(path=...) itself
# doesn't have this problem (it always builds PATH fresh from its `path`
# argument, never from ambient os.environ) -- this constant is only needed
# for the handful of call sites that build a subprocess env by hand.
REAL_PATH = os.environ.get("PATH", "")

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
    for name in ("osascript", "ps", "tmux"):
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
    # Build the new env from the *current* (pre-clear) os.environ before
    # touching it -- _build_hermetic_env()'s HOME/LANG/... passthrough reads
    # os.environ live, so calling it after os.environ.clear() would hand it
    # an already-empty ambient and silently pass through nothing every time
    # (found while building the tmux backend tests: tmux's -F engine decides
    # UTF-8-safe output per client by checking LANG/LC_ALL, and a missing
    # LANG made it mangle SEP -- see docs/worklog/usagemonitoring-developer/
    # 2026-08-07.md). This also makes nesting correct: an inner
    # hermetic_env() call now sees the outer one's env as its "ambient",
    # exactly as a real nested environment would.
    new_env = _build_hermetic_env(path=path, **extra)
    try:
        os.environ.clear()
        os.environ.update(new_env)
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
            # BACKLOG 3a fixture (spec section 5.3): "backup" sits in the
            # MIDDLE of this name, not the start -- a case where re.search
            # finds it and re.match (anchored at position 0) does not. Every
            # other fixture name above is effectively ^-anchored against the
            # patterns used on it, which is exactly the coverage hole 3a
            # closes: without this session, mutating `pat.search` ->
            # `pat.match` in the name: branch leaves the suite green. Chosen
            # to avoid "daily" so it does not also change what the pre-existing
            # DAILY-targeted assertions below match.
            iterm_ctl.Session("3.1.1", "EEEE5555", "/dev/ttys005", "run backup task"),
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

    def test_name_prefix_is_unanchored_search_not_match(self):
        # BACKLOG 3a (spec section 5.3): `name:REGEX` ships and is documented
        # as an *unanchored search*; `pat.search` in the code is correct and
        # must stay. This is the coverage hole's fix -- a fixture ("run
        # backup task") whose pattern ("backup") sits in the middle, where
        # search finds it and match (anchored at position 0) does not.
        got = iterm_ctl.resolve_targets(self.sessions, "name:backup", False)
        self.assertEqual([s.id for s in got], ["EEEE5555"])

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
        # BRANCH 1 (the \d+.\d+.\d+ *shape* fast path) must not fire for a
        # 2-part or 4-part target, even against a session whose .index is
        # unrelated -- proven with fixtures that also do NOT equal the
        # target, so this exercises branch 1's shape gate specifically and
        # not the separate exact-match branch tested below.
        two_part_shape = [iterm_ctl.Session("9.9.9", "X", "/dev/ttysA", "no-match-here")]
        got = iterm_ctl.resolve_targets(two_part_shape, "2.1", False)
        self.assertEqual(got, [])

        four_part_shape = [iterm_ctl.Session("9.9.9", "Y", "/dev/ttysB", "no-match-here")]
        got = iterm_ctl.resolve_targets(four_part_shape, "2.1.1.1", False)
        self.assertEqual(got, [])

    def test_non_three_part_index_or_id_matches_via_exact_match_branch(self):
        # spec docs/specs/tmux-universal-backend.md section 5.1 (BACKLOG 0c)
        # adds a new branch -- exact string match on s.index or s.id --
        # positioned after id:/tty:/name: and before the bare-substring
        # fallback. It is deliberately NOT gated by the \d+.\d+.\d+ shape
        # check (that gate belongs to branch 1 only, which exists purely to
        # give iTerm2's real index format a fast exact path). This is what
        # lets a tmux user type a bare "work:2.0" (session:window.pane) or a
        # bare "%3" (pane id) and hit the right pane (AC-13).
        #
        # Consequence, deliberate and spec-authorized: a session whose
        # .index happens to be a non-3-part string -- impossible for a real
        # iTerm2 session, but exactly the tmux "session:window.pane" shape --
        # now matches when the target equals it exactly. Before 5.1 landed
        # (2026-08-07) this test asserted the opposite (`== []`) for these
        # same two shapes; see docs/worklog/usagemonitoring-developer/
        # 2026-08-07.md for why that assertion changed rather than being
        # dropped silently.
        two_part = [iterm_ctl.Session("2.1", "X", "/dev/ttysA", "no-match-here")]
        got = iterm_ctl.resolve_targets(two_part, "2.1", False)
        self.assertEqual(got, two_part)

        four_part = [iterm_ctl.Session("2.1.1.1", "Y", "/dev/ttysB", "no-match-here")]
        got = iterm_ctl.resolve_targets(four_part, "2.1.1.1", False)
        self.assertEqual(got, four_part)

        # Same branch, the .id side -- this is the "%3" bare-pane-id case.
        by_id = [iterm_ctl.Session("3.9.9", "%3", "/dev/ttysC", "no-match-here")]
        got = iterm_ctl.resolve_targets(by_id, "%3", False)
        self.assertEqual(got, by_id)

    # -- docs/specs/stable-job-targets-and-zero-match-failure.md S1: the
    # explicit "index:" prefix. Before this branch existed, "index:2.1.1"
    # fell through every prefix check to the bare-substring fallback,
    # matched nothing, and returned [] silently -- exactly the "wrong
    # result that is byte-identical to a legitimately empty one" bug class
    # this whole spec exists to kill (see spec section 1's "the rot is
    # invisible"). Reproduced live against this exact branch at 68f8b93
    # before this fix (Messi's dispatch note): 'index:2.1.1' -> [] while
    # '2.1.1' -> ['2.1.1']. --

    def test_ac1_index_prefix_matches_exact_session(self):
        got = iterm_ctl.resolve_targets(self.sessions, "index:2.1.1", False)
        self.assertEqual([s.id for s in got], ["CCCC3333"])
        # Both spellings are interchangeable -- same session, same result.
        self.assertEqual(got, iterm_ctl.resolve_targets(self.sessions, "2.1.1", False))

    def test_ac2_index_prefix_no_match_returns_empty_not_substring_fallback(self):
        got = iterm_ctl.resolve_targets(self.sessions, "index:9.9.9", False)
        self.assertEqual(got, [])

    def test_index_prefix_does_not_fall_back_to_a_name_substring_match(self):
        # A target whose index: value doesn't exist anywhere, but which
        # WOULD match a session by name as a bare substring, must still
        # return [] -- proves the index: branch's own return short-circuits
        # before the bare-substring fallback ever runs, not just that its
        # value happens not to collide with a name.
        sessions = [iterm_ctl.Session("1.1.1", "SID", "/dev/ttys001", "index:not-a-real-index")]
        got = iterm_ctl.resolve_targets(sessions, "index:not-a-real-index", False)
        self.assertEqual(got, [])

    def test_ac3_regression_all_six_pre_existing_forms_are_unchanged(self):
        # spec AC-3: bare index, id:, tty:, name:, exact-id/index (the 5.1
        # branch), and bare-substring must all return exactly what they
        # returned before S1 landed. Asserted together, in one place, per
        # the spec's explicit "all six forms, not a sample."
        got = iterm_ctl.resolve_targets(self.sessions, "2.1.1", False)  # bare index
        self.assertEqual([s.id for s in got], ["CCCC3333"])

        got = iterm_ctl.resolve_targets(self.sessions, "id:aaaa", False)  # id:
        self.assertEqual([s.id for s in got], ["AAAA1111"])

        got = iterm_ctl.resolve_targets(self.sessions, "tty:ttys002", False)  # tty:
        self.assertEqual([s.id for s in got], ["BBBB2222"])

        got = iterm_ctl.resolve_targets(self.sessions, "name:^DAILY", False)  # name:
        self.assertEqual([s.id for s in got], ["BBBB2222"])

        by_id = [iterm_ctl.Session("3.9.9", "%3", "/dev/ttysC", "no-match-here")]
        got = iterm_ctl.resolve_targets(by_id, "%3", False)  # exact-match branch (5.1)
        self.assertEqual(got, by_id)

        got = iterm_ctl.resolve_targets(self.sessions, "DAILY", False)  # bare substring
        self.assertEqual([s.id for s in got], ["BBBB2222"])

    def test_ac4_duplicate_name_target_returns_both_sessions_not_deduped(self):
        # spec AC-4: this documents a real hazard (S2's background: two live
        # sessions both literally named "-zsh") -- a name: match on two
        # identically-named sessions must return BOTH, not silently dedupe
        # to one. resolve_targets never had dedup logic; this pins that it
        # still doesn't after S1's additive change.
        twins = [
            iterm_ctl.Session("1.1.1", "TWIN-A", "/dev/ttys010", "-zsh"),
            iterm_ctl.Session("1.2.1", "TWIN-B", "/dev/ttys011", "-zsh"),
            iterm_ctl.Session("1.3.1", "OTHER", "/dev/ttys012", "vim"),
        ]
        got = iterm_ctl.resolve_targets(twins, "name:^-zsh$", False)
        self.assertEqual({s.id for s in got}, {"TWIN-A", "TWIN-B"})
        self.assertEqual(len(got), 2)


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
# G8 -- tmux backend seam, hermetic (docs/specs/tmux-universal-backend.md
# section 3, BACKLOG 0c/0e), using tests/fake/tmux the same way G1-G6 use
# tests/fake/osascript. Runs on any machine, tmux installed or not.
# --------------------------------------------------------------------------- #
def _tmux_log_records(log_path: str) -> list:
    with open(log_path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def _tmux_calls(log_path: str) -> list:
    """The argv list of every fake-tmux invocation, in order -- what every
    pre-round-4 test in this class asserts on."""
    return [record["argv"] for record in _tmux_log_records(log_path)]


def _tmux_call_lc_all(log_path: str) -> list:
    """The LC_ALL value fake-tmux actually saw in *its own* env for every
    invocation, in order (BACKLOG round 4, Ayala's finding: _tmux_env()'s
    LC_ALL=C.UTF-8 fix had zero regression coverage -- deleting it left the
    suite green because every test's fake-tmux process just inherited
    whatever real, already-UTF-8 locale the host machine happened to have.
    This reads what TmuxBackend's subprocess.run(env=...) actually handed
    the child process, independent of the host's own ambient locale)."""
    return [record["env_LC_ALL"] for record in _tmux_log_records(log_path)]


class G8TmuxBackendHermetic(unittest.TestCase):
    def test_list_parses_all_six_fields(self):
        SEP = iterm_ctl.SEP
        listing = (
            f"work:2.0{SEP}%3{SEP}/dev/ttys021{SEP}editor{SEP}vim{SEP}/Users/x/proj\n"
        )
        listing_path = write_tmp(listing)
        with hermetic_env(ITERMON_BACKEND="tmux", ITERMON_FAKE_TMUX_LISTING=listing_path):
            sessions = iterm_ctl.list_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s.index, "work:2.0")
        self.assertEqual(s.id, "%3")
        self.assertEqual(s.tty, "/dev/ttys021")
        self.assertEqual(s.name, "editor")
        self.assertEqual(s.job, "vim")
        self.assertEqual(s.cwd, "/Users/x/proj")

    def test_no_server_running_is_empty_list_not_an_error(self):
        # spec 3.1: "no server running" on stderr with non-zero exit is NOT
        # an error -- it maps to an empty list, same as iTerm2 w/ no windows.
        with hermetic_env(ITERMON_BACKEND="tmux", ITERMON_FAKE_TMUX_NO_SERVER="1"):
            sessions = iterm_ctl.list_sessions()
        self.assertEqual(sessions, [])

    def test_socket_dir_never_created_is_also_empty_list_not_an_error(self):
        # A second, real, live-verified "no server" message (see
        # G9TmuxBackendLive.test_ac11): a *fresh* $TMUX_TMPDIR that has never
        # had a server on it produces "error connecting to <path> (No such
        # file or directory)", not "no server running on <path>" -- same
        # underlying condition (no server), different wording depending on
        # whether the socket path was ever used before. Both must map to [].
        with hermetic_env(
            ITERMON_BACKEND="tmux",
            ITERMON_FAKE_TMUX_ERROR="error connecting to /tmp/x/tmux-501/default (No such file or directory)",
        ):
            sessions = iterm_ctl.list_sessions()
        self.assertEqual(sessions, [])

    def test_other_stderr_raises_runtime_error(self):
        with hermetic_env(
            ITERMON_BACKEND="tmux", ITERMON_FAKE_TMUX_ERROR="boom: something else broke"
        ):
            with self.assertRaises(RuntimeError):
                iterm_ctl.list_sessions()

    def test_send_text_uses_literal_dash_l_dash_dash(self):
        # BACKLOG 0e -- non-negotiable (spec 3.2). Without -l, send-keys reads
        # its argument as a KEY NAME, not text: the spike proved send-keys
        # 'C-c' sends the Ctrl-C key and types nothing, while send-keys -l --
        # 'C-c' types the three characters. This fails if either token is
        # dropped, or if their order/position changes.
        log = write_tmp("")
        session = iterm_ctl.Session("work:0.0", "%9", "/dev/ttys001", "s")
        with hermetic_env(ITERMON_BACKEND="tmux", ITERMON_FAKE_TMUX_LOG=log):
            iterm_ctl.send_text(session, "C-c", enter=False)
        calls = _tmux_calls(log)
        self.assertEqual(calls, [["send-keys", "-t", "%9", "-l", "--", "C-c"]])

    def test_send_text_with_enter_is_two_calls_text_then_enter(self):
        log = write_tmp("")
        session = iterm_ctl.Session("work:0.0", "%9", "/dev/ttys001", "s")
        with hermetic_env(ITERMON_BACKEND="tmux", ITERMON_FAKE_TMUX_LOG=log):
            iterm_ctl.send_text(session, "git status", enter=True)
        calls = _tmux_calls(log)
        self.assertEqual(
            calls,
            [
                ["send-keys", "-t", "%9", "-l", "--", "git status"],
                ["send-keys", "-t", "%9", "Enter"],
            ],
        )

    def test_empty_text_no_enter_is_a_no_op_no_tmux_call_at_all(self):
        log = write_tmp("")
        session = iterm_ctl.Session("work:0.0", "%9", "/dev/ttys001", "s")
        with hermetic_env(ITERMON_BACKEND="tmux", ITERMON_FAKE_TMUX_LOG=log):
            iterm_ctl.send_text(session, "", enter=False)
        self.assertEqual(_tmux_calls(log), [])

    def test_empty_text_with_enter_sends_only_enter(self):
        # Preserves iterm_web.py's "send a bare newline" call (iterm_web.py's
        # run_job()/send_to_targets() send text="" enter=True to submit).
        log = write_tmp("")
        session = iterm_ctl.Session("work:0.0", "%9", "/dev/ttys001", "s")
        with hermetic_env(ITERMON_BACKEND="tmux", ITERMON_FAKE_TMUX_LOG=log):
            iterm_ctl.send_text(session, "", enter=True)
        self.assertEqual(_tmux_calls(log), [["send-keys", "-t", "%9", "Enter"]])

    def test_read_contents_uses_capture_pane(self):
        log = write_tmp("")
        screen = write_tmp("$ echo hi\nhi\n")
        session = iterm_ctl.Session("work:0.0", "%9", "/dev/ttys001", "s")
        with hermetic_env(
            ITERMON_BACKEND="tmux", ITERMON_FAKE_TMUX_LOG=log, ITERMON_FAKE_TMUX_SCREEN=screen
        ):
            got = iterm_ctl.read_contents(session)
        self.assertEqual(got, "$ echo hi\nhi\n")
        self.assertEqual(_tmux_calls(log), [["capture-pane", "-p", "-t", "%9"]])

    def test_every_tmux_invocation_gets_lc_all_c_utf8(self):
        # BACKLOG round 4 (Ayala's finding): _tmux_env() forces
        # LC_ALL=C.UTF-8 on every tmux subprocess call, working around
        # tmux's -F engine silently replacing SEP with "_" (collapsing the
        # 6-field parse) when it doesn't see a UTF-8-declaring locale in its
        # own env -- see _tmux_env()'s docstring in iterm_ctl.py. Every
        # earlier test in this file exercises that code path but none of
        # them asserted on the env, so all 61 stayed green even with the
        # LC_ALL line deleted outright (Ayala proved this live: mutated a
        # throwaway copy, deleting exactly that line, reran the full suite,
        # 61/61 OK -- the fix was defended by nothing). Pins hostile ambient
        # LC_ALL="C"/LANG="C" as explicit hermetic_env() overrides (never
        # the host's real locale, so this can't accidentally pass for the
        # same reason the old suite did) and checks every one of the three
        # tmux subcommands TmuxBackend issues, not just one.
        log = write_tmp("")
        listing_path = write_tmp(
            f"work:0.0{iterm_ctl.SEP}%9{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}s{iterm_ctl.SEP}zsh{iterm_ctl.SEP}/tmp\n"
        )
        screen_path = write_tmp("hi\n")
        session = iterm_ctl.Session("work:0.0", "%9", "/dev/ttys001", "s")
        with hermetic_env(
            ITERMON_BACKEND="tmux",
            ITERMON_FAKE_TMUX_LOG=log,
            ITERMON_FAKE_TMUX_LISTING=listing_path,
            ITERMON_FAKE_TMUX_SCREEN=screen_path,
            LC_ALL="C",
            LANG="C",
        ):
            iterm_ctl.list_sessions()
            iterm_ctl.send_text(session, "hi", enter=True)
            iterm_ctl.read_contents(session)
        lc_alls = _tmux_call_lc_all(log)
        # list-panes, send-keys (text) + send-keys (Enter), capture-pane.
        self.assertEqual(len(lc_alls), 4)
        for value in lc_alls:
            self.assertEqual(value, "C.UTF-8")

    def test_tmux_missing_from_path_raises_clear_error_naming_tmux(self):
        # spec 3.4: tmux backend selected, tmux not on PATH -> clear
        # RuntimeError, never a traceback or a silent empty list.
        with hermetic_env(path=NO_TOOLS_DIR, ITERMON_BACKEND="tmux"):
            with self.assertRaises(RuntimeError) as ctx:
                iterm_ctl.list_sessions()
        self.assertIn("tmux", str(ctx.exception).lower())

    def test_unknown_backend_env_var_is_fail_closed_not_a_silent_fallback(self):
        with hermetic_env(ITERMON_BACKEND="notabackend"):
            with self.assertRaises(RuntimeError) as ctx:
                iterm_ctl.list_sessions()
        msg = str(ctx.exception).lower()
        self.assertIn("iterm2", msg)
        self.assertIn("tmux", msg)

    # -- $ITERMON_BACKEND value handling (BACKLOG round 2 finding): the code
    # used to reject ITERMON_BACKEND=auto while its own docstring ("'auto' --
    # whether it's the flag's default or an explicit --backend auto --
    # always falls through") and its own error message ("...(or 'auto')")
    # both said it was valid. Four cases pin the fixed contract: 'auto' and
    # empty/whitespace both defer to the platform default (unset and 'auto'
    # are the same resolution mode, not two different things); a genuine
    # typo still fails closed. --

    def test_backend_env_var_auto_falls_through_to_platform_default(self):
        # ITERMON_BACKEND=auto must resolve exactly like the flag's own
        # 'auto' (or the flag not being passed at all): not pinned, defer to
        # $ITERMON_BACKEND's *next* source -- which, with the env var itself
        # being the thing set to 'auto', is the platform default.
        with hermetic_env(ITERMON_BACKEND="auto"):
            resolved = iterm_ctl._resolve_backend_name()
        self.assertEqual(resolved, "iterm2" if sys.platform == "darwin" else "tmux")

    def test_backend_env_var_empty_string_is_treated_as_unset(self):
        # An empty value is how wrapper scripts and CI commonly express "not
        # set" (ITERMON_BACKEND=$SOMETHING with $SOMETHING unset) -- must
        # fall through to the platform default, not fail closed like a typo.
        with hermetic_env(ITERMON_BACKEND=""):
            resolved = iterm_ctl._resolve_backend_name()
        self.assertEqual(resolved, "iterm2" if sys.platform == "darwin" else "tmux")

    def test_backend_env_var_whitespace_only_is_also_treated_as_unset(self):
        # A trailing newline out of a wrapper script's `$(...)` capture, or a
        # stray space from a shell profile, should not be a crash.
        with hermetic_env(ITERMON_BACKEND="  \n"):
            resolved = iterm_ctl._resolve_backend_name()
        self.assertEqual(resolved, "iterm2" if sys.platform == "darwin" else "tmux")

    def test_backend_env_var_genuine_typo_still_fails_closed(self):
        # 'auto' and empty/whitespace are the only forgiven spellings -- a
        # real typo of a real backend name must still raise. Silently
        # falling back to the other backend on a misspelled name would be a
        # worse failure mode than a loud error naming the valid choices.
        with hermetic_env(ITERMON_BACKEND="tmuxx"):
            with self.assertRaises(RuntimeError) as ctx:
                iterm_ctl._resolve_backend_name()
        msg = str(ctx.exception).lower()
        self.assertIn("tmuxx", msg)
        self.assertIn("iterm2", msg)
        self.assertIn("tmux", msg)

    def test_cli_backend_flag_selects_tmux_without_env_var(self):
        # The --backend flag itself (spec 2.3, item 1 in the resolution
        # order) -- exercised through the real CLI entry point (main()),
        # with no $ITERMON_BACKEND set at all, proving the flag alone is
        # sufficient. main() stashes the flag in a module-level override, so
        # this registers a cleanup to reset it -- otherwise a later test in
        # this same process would silently inherit "tmux".
        self.addCleanup(iterm_ctl._set_cli_backend, None)
        listing_path = write_tmp(
            f"work:0.0{iterm_ctl.SEP}%0{iterm_ctl.SEP}/dev/ttys009{iterm_ctl.SEP}w{iterm_ctl.SEP}zsh{iterm_ctl.SEP}/tmp\n"
        )
        result = {}
        with hermetic_env(ITERMON_FAKE_TMUX_LISTING=listing_path):
            _capture_stdout(lambda: result.update(rc=iterm_ctl.main(["list", "--backend", "tmux"])))
        self.assertEqual(result["rc"], 0)

    def test_auto_never_prefers_tmux_on_darwin_even_with_dollar_tmux_set(self):
        # spec 2.3's ruling: auto on macOS always means iterm2, even inside a
        # tmux pane ($TMUX set). Only meaningful to assert on darwin itself;
        # skipped elsewhere since the platform default there is tmux anyway.
        if sys.platform != "darwin":
            self.skipTest("darwin-only: this pins the macOS-specific ruling")
        listing_path = write_tmp("")  # empty iTerm2 listing -> []
        with hermetic_env(TMUX="/private/tmp/tmux-501/default,1234,0"):
            sessions = iterm_ctl.list_sessions()  # must go to iterm2, not tmux
        self.assertEqual(sessions, [])  # iterm2 fake with no ITERMON_FAKE_LISTING


# --------------------------------------------------------------------------- #
# G9 -- tmux backend, live (spec section 8 AC-6..AC-12). Real tmux 3.6a
# against a throwaway, private-socket server -- never the operator's own tmux
# (same discipline as docs/spikes/2026-08-06-terminal-universality-spike.md
# and Dida's iTerm2 practice). Skips cleanly when tmux is not installed, and
# never touches the default socket: isolation comes from pointing
# $TMUX_TMPDIR at a fresh, throwaway directory for the whole class, which is
# where tmux resolves its (unqualified, no -S/-L given) default socket path
# from (see `tmux(1)`, TMUX_TMPDIR) -- so every plain `tmux ...` call the
# backend itself makes (spec section 3 never mentions -S) still lands on our
# private server, not the operator's.
# --------------------------------------------------------------------------- #
TMUX_BIN = shutil.which("tmux")


def _short_tmux_tmpdir(prefix: str) -> str:
    """A throwaway dir for $TMUX_TMPDIR, rooted at /tmp rather than
    tempfile.gettempdir() -- on macOS the latter is $TMPDIR, a long
    per-process path under /private/var/folders/..., and tmux appends
    "/tmux-<uid>/default" to whatever TMUX_TMPDIR is to build its actual
    AF_UNIX socket path. That combination silently exceeds the ~104-byte
    sockaddr_un limit and tmux fails with "File name too long" -- not a
    permissions or hermeticity problem, just a path-length one. /tmp is
    short (and itself a symlink on macOS, but the *string* passed to
    bind() is what's length-limited, not its resolved target)."""
    return tempfile.mkdtemp(prefix=prefix, dir="/tmp")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed -- skipping live tmux backend tests")
class G9TmuxBackendLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmux_tmpdir = _short_tmux_tmpdir("itermon-tmux-live-")
        cls.env = dict(os.environ)
        cls.env["PATH"] = REAL_PATH  # see REAL_PATH's comment -- ambient os.environ
                                      # here may already be inside main()'s outer
                                      # hermetic_env(), which would otherwise hand
                                      # this real `tmux new-session` call to the fake.
        cls.env["TMUX_TMPDIR"] = cls.tmux_tmpdir
        cls.env.pop("TMUX", None)  # never inherit "we're already inside a tmux" state
        # Three windows in one throwaway session, matching the spike's own
        # id-stability experiment (spike section 2.2). "first" runs a real
        # shell (sh -- POSIX, present everywhere this suite runs, and its
        # arithmetic expansion is all AC-7 below needs) rather than `sleep`:
        # a `sleep`-only pane never reads its stdin, so tmux's pty still
        # locally echoes whatever `send-keys` sends -- proving a send
        # happened, not that anything executed it (Dida's finding, round 2 of
        # this review). "second"/"third" stay on `sleep 300`: AC-8 needs an
        # inert pane to prove literal-mode delivery independent of anything a
        # shell might do with the keystrokes, and AC-9 only needs a pane that
        # survives, not one that runs anything in particular.
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", "live", "-n", "first", "sh"],
            env=cls.env, check=True,
        )
        subprocess.run(
            ["tmux", "new-window", "-t", "live", "-n", "second", "sleep 300"],
            env=cls.env, check=True,
        )
        subprocess.run(
            ["tmux", "new-window", "-t", "live", "-n", "third", "sleep 300"],
            env=cls.env, check=True,
        )

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "kill-server"], env=cls.env, capture_output=True)
        shutil.rmtree(cls.tmux_tmpdir, ignore_errors=True)

    def _list(self):
        with hermetic_env(
            path=os.path.dirname(TMUX_BIN), ITERMON_BACKEND="tmux", TMUX_TMPDIR=self.tmux_tmpdir
        ):
            return iterm_ctl.list_sessions()

    def test_ac6_list_shows_every_pane_fully_populated(self):
        sessions = self._list()
        self.assertEqual(len(sessions), 3)
        for s in sessions:
            self.assertTrue(s.index)
            self.assertRegex(s.id, r"^%\d+$")
            self.assertTrue(s.tty)
            self.assertTrue(s.name)
            self.assertTrue(s.job)  # "sleep"
            self.assertTrue(s.cwd)

    def test_lc_all_fix_hostile_ambient_locale_still_lists_every_pane(self):
        # BACKLOG round 4 (Ayala's finding) -- the live half of the guard;
        # see G8TmuxBackendHermetic.test_every_tmux_invocation_gets_lc_all_c
        # _utf8 for the hermetic half. Not a numbered spec AC -- this pins a
        # bug found and fixed during implementation (2026-08-07 worklog),
        # not a spec-mandated behavior, so it doesn't reuse an AC-N name.
        #
        # Forces a real, hostile, non-UTF-8-declaring locale (LC_ALL=C,
        # LC_CTYPE=C, LANG=C -- what a bare SSH session or a cron job
        # typically hands a script, exactly this feature's own target
        # environment) onto the *ambient* env that _tmux_env() starts from
        # (dict(os.environ)) before forcing LC_ALL=C.UTF-8, and proves a
        # real tmux server still hands back all three real panes -- not the
        # silent, no-error empty list this bug produces (Ayala's live
        # reproduction: 0 != 3, no exception, no stderr -- indistinguishable
        # from "no sessions" to a caller).
        with hermetic_env(
            path=os.path.dirname(TMUX_BIN), ITERMON_BACKEND="tmux",
            TMUX_TMPDIR=self.tmux_tmpdir,
            LC_ALL="C", LC_CTYPE="C", LANG="C",
        ):
            sessions = iterm_ctl.list_sessions()
        self.assertEqual(len(sessions), 3)
        self.assertEqual({s.name for s in sessions}, {"first", "second", "third"})

    def test_ac7_send_then_read_round_trips_through_the_real_cli(self):
        # Goes through iterm_ctl.main() -- the real CLI entry point, argv and
        # all -- not the internal functions directly. Backend selection here
        # comes from $ITERMON_BACKEND (set by hermetic_env below), not
        # --backend, so this test never touches the module-level
        # _CLI_BACKEND override and can't leak state into a later test.
        #
        # Why `echo ITERMON_$((6*7))` and not a fixed marker string (Dida's
        # finding, round 2 of this review): `assertIn(marker, screen)` with a
        # literal marker is satisfied by the pty's own local echo of what we
        # typed -- true whether or not anything downstream ever reads and
        # runs it. Against the old "sleep 300"-only fixture this test would
        # have stayed green even if send_text() typed into a black hole; it
        # only ever caught real breakage because "first" happened to be
        # proven separately, not because this assertion could tell execution
        # from echo. Unevaluated arithmetic closes that gap for free: the
        # bytes we send contain the literal substring "$((6*7))", never "42"
        # -- so a line containing "ITERMON_42" cannot be produced by echo
        # alone, only by a real shell reading the line and evaluating it.
        sessions = self._list()
        target = next(s for s in sessions if s.name == "first")
        marker_cmd = "echo ITERMON_$((6*7))"
        executed_marker = "ITERMON_42"
        result = {}
        with hermetic_env(path=os.path.dirname(TMUX_BIN), ITERMON_BACKEND="tmux",
                           TMUX_TMPDIR=self.tmux_tmpdir):
            _capture_stdout(
                lambda: result.update(rc=iterm_ctl.main(["send", "id:" + target.id, marker_cmd]))
            )
            self.assertEqual(result["rc"], 0)
            time.sleep(0.3)
            screen = iterm_ctl.read_contents(
                iterm_ctl.Session(target.index, target.id, target.tty, target.name)
            )
        lines = screen.splitlines()
        # Sanity check that the send actually reached the pane at all (typed
        # line, echoed literally, unevaluated -- present whether or not
        # anything executes it).
        self.assertTrue(
            any("$((6*7))" in line for line in lines),
            f"expected the typed command to appear literally, got: {lines!r}",
        )
        # The real assertion: a line that is NOT the typed line and DOES
        # contain the evaluated result. Only a shell that actually ran the
        # command can produce this -- pty echo of our input never can, since
        # our input never contains "42".
        output_lines = [ln for ln in lines if "$((6*7))" not in ln]
        self.assertTrue(
            any(executed_marker in line for line in output_lines),
            f"expected an executed-output line containing {executed_marker!r} "
            f"(distinct from the typed line) -- got: {lines!r}",
        )

    def test_ac8_literal_mode_counter_test_c_dash_c_is_typed_not_sent_as_ctrl_c(self):
        sessions = self._list()
        target = next(s for s in sessions if s.name == "second")
        with hermetic_env(path=os.path.dirname(TMUX_BIN), ITERMON_BACKEND="tmux",
                           TMUX_TMPDIR=self.tmux_tmpdir):
            session = iterm_ctl.Session(target.index, target.id, target.tty, target.name)
            iterm_ctl.send_text(session, "C-c", enter=False)
            time.sleep(0.3)
            screen = iterm_ctl.read_contents(session)
        # The three characters "C-c" must appear typed on the line -- if -l or
        # -- were dropped, tmux would interpret 'C-c' as the Ctrl-C key
        # instead, and nothing would be typed.
        self.assertIn("C-c", screen)

    def test_ac9_pane_id_survives_killing_a_neighbour(self):
        before = {s.name: s.id for s in self._list()}
        third_id = before["third"]
        subprocess.run(["tmux", "kill-window", "-t", "live:second"], env=self.env, check=True)
        after = self._list()
        after_ids = {s.id for s in after}
        self.assertIn(third_id, after_ids)
        still_there = next(s for s in after if s.id == third_id)
        self.assertEqual(still_there.name, "third")
        # recreate "second" so later test methods (unordered by design, but
        # unittest runs alphabetically -- ac9 sorts after ac8/ac7/ac6/ac12,
        # before ac10) don't depend on running before this one.
        subprocess.run(
            ["tmux", "new-window", "-t", "live", "-n", "second", "sleep 300"],
            env=self.env, check=True,
        )

    def test_ac10_send_and_read_against_missing_pane_id_is_the_existing_error_path(self):
        bogus = iterm_ctl.Session("live:9.9", "%9999", "/dev/ttysXXX", "ghost")
        with hermetic_env(path=os.path.dirname(TMUX_BIN), ITERMON_BACKEND="tmux",
                           TMUX_TMPDIR=self.tmux_tmpdir):
            with self.assertRaises(RuntimeError):
                iterm_ctl.read_contents(bogus)
            with self.assertRaises(RuntimeError):
                iterm_ctl.send_text(bogus, "hi", enter=True)

    def test_ac11_no_server_at_all_list_prints_empty_and_exits_0(self):
        empty_tmpdir = _short_tmux_tmpdir("itermon-tmux-live-empty-")
        try:
            with hermetic_env(path=os.path.dirname(TMUX_BIN), ITERMON_BACKEND="tmux",
                               TMUX_TMPDIR=empty_tmpdir):
                sessions = iterm_ctl.list_sessions()
                buf = _capture_stdout(lambda: iterm_ctl.print_table(
                    sessions, empty_message=iterm_ctl.TmuxBackend.empty_message
                ))
            self.assertEqual(sessions, [])
            self.assertIn("No tmux panes found", buf)
        finally:
            shutil.rmtree(empty_tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# G10 -- iterm_web.py: a 0-match send is a visible failure, not a silent
# success (docs/specs/stable-job-targets-and-zero-match-failure.md S2,
# AC-5/AC-6/AC-7). Exercises the real run_job() function and a real
# iterm_web.Handler HTTP server bound to an OS-assigned ephemeral port
# (never 8765 -- spec/dispatch constraint) inside this process's own
# hermetic env (fake osascript on PATH, spec section 3). JOBS_FILE and
# ACTIVITY_FILE are monkeypatched to a throwaway tempfile.mkdtemp() path for
# the duration of each test, in setUp/tearDown -- never the repo root's real
# iterm_jobs.json/activity.log (CLAUDE.md, dispatch rule 2). Importing
# iterm_web.py here never starts anything against the live admin (PID 1471,
# port 8765): that is a wholly separate OS process; this test process only
# ever calls run_job()/spins its own throwaway HTTP server in-process.
# --------------------------------------------------------------------------- #
def _raw_send_and_recv(port: int, request: bytes) -> bytes:
    """Send raw bytes over a fresh socket to 127.0.0.1:port and return
    whatever comes back before the peer closes (or a 5s timeout). Used by
    G11 wherever urllib can't produce the exact wire-level request a test
    needs -- a Host header that's absent entirely, or one that names a
    different host than the socket physically connects to (the
    DNS-rebinding shape itself, spec AC-3/AC-4)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        sock.settimeout(5)
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        return b"".join(chunks)


def _raw_request(port: int, method: str, path: str, headers: dict, body: bytes = b"") -> bytes:
    """Hand-built HTTP/1.1 request giving full control over headers --
    notably a Host/Origin pair independent of urllib's own header
    management. Always HTTP/1.1 with an explicit Content-Length (0 if
    `body` is empty) and Connection: close."""
    hdr_lines = [f"{k}: {v}" for k, v in headers.items()]
    if not any(k.lower() == "content-length" for k in headers):
        hdr_lines.append(f"Content-Length: {len(body)}")
    hdr_lines.append("Connection: close")
    request = (f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(hdr_lines) + "\r\n\r\n").encode() + body
    return _raw_send_and_recv(port, request)


def _raw_status(response: bytes) -> int:
    return int(response.split(b"\r\n", 1)[0].split(b" ", 2)[1])


def _raw_json_body(response: bytes) -> dict:
    _head, _, body = response.partition(b"\r\n\r\n")
    return json.loads(body.decode())


@contextlib.contextmanager
def _web_admin_server():
    """A real ThreadingHTTPServer on an OS-assigned ephemeral port (never
    8765), serving iterm_web.Handler, for the duration of the block. Shared
    by G10 and G11 (spec docs/specs/admin-api-origin-hardening.md section 0:
    "that harness already exists ... Extend it; do not invent a second
    one."). Runs in a daemon thread; the caller must issue its request(s)
    synchronously inside the `with`, while the surrounding hermetic_env() is
    still active, since the handler thread reads the same process-wide
    os.environ the request-issuing code set up."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), iterm_web.Handler)
    port = server.server_address[1]
    if port == 8765:
        raise AssertionError("must never bind the live admin's port")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class G10ZeroMatchIsAFailure(unittest.TestCase):
    def setUp(self):
        self._orig_jobs_file = iterm_web.JOBS_FILE
        self._orig_activity_file = iterm_web.ACTIVITY_FILE
        self._tmp_dir = tempfile.mkdtemp(prefix="itermon-web-test-")
        iterm_web.JOBS_FILE = os.path.join(self._tmp_dir, "iterm_jobs.json")
        iterm_web.ACTIVITY_FILE = os.path.join(self._tmp_dir, "activity.log")
        iterm_web._log.clear()

    def tearDown(self):
        iterm_web.JOBS_FILE = self._orig_jobs_file
        iterm_web.ACTIVITY_FILE = self._orig_activity_file
        iterm_web._log.clear()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _last_log_entry(self) -> dict:
        return iterm_web._log[-1]

    @contextlib.contextmanager
    def _running_server(self):
        with _web_admin_server() as port:
            self.assertNotEqual(port, 8765, "must never bind the live admin's port")
            yield port

    def _post(self, port: int, path: str, body: dict):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    # -- AC-5: run_job(), 0 matches --
    def test_ac5_run_job_zero_match_status_and_error_log_kind(self):
        with hermetic_env():  # ITERMON_FAKE_LISTING unset -> fake osascript lists 0 sessions
            result = iterm_web.run_job(
                {"target": "index:9.9.9", "command": "echo hi", "name": "nudge"}
            )
        self.assertEqual(result["status"], "MATCHED 0 SESSIONS — not delivered")
        entry = self._last_log_entry()
        self.assertEqual(entry["kind"], "error")
        self.assertIn("nudge", entry["message"])
        self.assertIn("index:9.9.9", entry["message"])
        self.assertIn("NOT DELIVERED", entry["message"])

    # -- AC-6: run_job(), 1 match -- byte-identical to before this change --
    def test_ac6_run_job_one_match_status_and_send_log_kind_unchanged(self):
        listing = f"1.1.1{iterm_ctl.SEP}ID-A{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}alpha\n"
        listing_path = write_tmp(listing)
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            result = iterm_web.run_job(
                {"target": "1.1.1", "command": "echo hi", "name": "nudge"}
            )
        self.assertEqual(result, {"status": "sent to 1 session(s)", "sent": 1})
        entry = self._last_log_entry()
        self.assertEqual(entry["kind"], "send")
        self.assertIn("sent", entry["message"])

    # -- AC-7: /api/send, 0 matches -- 200, sent==[], matched==0, log kind error --
    def test_ac7_api_send_zero_match_is_200_sent_empty_matched_0_error_log(self):
        listing_path = write_tmp("")  # empty listing -> 0 sessions
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            with self._running_server() as port:
                status, payload = self._post(
                    port, "/api/send", {"target": "index:9.9.9", "command": "echo hi"}
                )
        self.assertEqual(status, 200)
        self.assertEqual(payload["sent"], [])
        self.assertEqual(payload["matched"], 0)
        entry = self._last_log_entry()
        self.assertEqual(entry["kind"], "error")
        self.assertIn("NOT DELIVERED", entry["message"])

    def test_api_send_one_match_response_shape_unchanged_no_matched_field(self):
        # Regression: the pre-existing {"sent": hits} shape and 200 status
        # for a real delivery must be untouched -- "matched" is a NEW field
        # that only appears on the 0-match path (spec S2: "add ... never by
        # mutating an old one").
        listing = f"1.1.1{iterm_ctl.SEP}ID-A{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}alpha\n"
        listing_path = write_tmp(listing)
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            with self._running_server() as port:
                status, payload = self._post(
                    port, "/api/send", {"target": "1.1.1", "command": "echo hi"}
                )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["sent"]), 1)
        self.assertEqual(payload["sent"][0]["index"], "1.1.1")
        self.assertNotIn("matched", payload)
        entry = self._last_log_entry()
        self.assertEqual(entry["kind"], "send")


# --------------------------------------------------------------------------- #
# G11 -- Origin/Host allowlist guard (BACKLOG #12 --
# docs/specs/admin-api-origin-hardening.md). Same hermeticity rules as G10:
# JOBS_FILE/ACTIVITY_FILE monkeypatched to a throwaway tempfile.mkdtemp(),
# never the real ones; server always on an OS-assigned ephemeral port, never
# 8765 (_web_admin_server() asserts this); fake osascript on PATH via
# hermetic_env(), never a real one. AC-1's load-bearing assertion throughout
# is that the fake osascript's call log stays EMPTY on a rejected request --
# the status code alone proves nothing (this product's three worst defects
# were all "the test passes whether or not the code is right").
# --------------------------------------------------------------------------- #
class G11OriginHostGuard(unittest.TestCase):
    def setUp(self):
        self._orig_jobs_file = iterm_web.JOBS_FILE
        self._orig_activity_file = iterm_web.ACTIVITY_FILE
        self._orig_extra_origins = list(iterm_web.EXTRA_ALLOWED_ORIGINS)
        self._orig_window = iterm_web.REJECTION_LOG_WINDOW_SECONDS
        self._orig_clock = iterm_web._REJECTION_CLOCK
        self._tmp_dir = tempfile.mkdtemp(prefix="itermon-web-guard-test-")
        iterm_web.JOBS_FILE = os.path.join(self._tmp_dir, "iterm_jobs.json")
        iterm_web.ACTIVITY_FILE = os.path.join(self._tmp_dir, "activity.log")
        iterm_web._log.clear()
        iterm_web._rejection_state = {"last_logged_at": None, "suppressed": 0}

    def tearDown(self):
        iterm_web.JOBS_FILE = self._orig_jobs_file
        iterm_web.ACTIVITY_FILE = self._orig_activity_file
        iterm_web.EXTRA_ALLOWED_ORIGINS = self._orig_extra_origins
        iterm_web.REJECTION_LOG_WINDOW_SECONDS = self._orig_window
        iterm_web._REJECTION_CLOCK = self._orig_clock
        iterm_web._log.clear()
        iterm_web._rejection_state = {"last_logged_at": None, "suppressed": 0}
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    @staticmethod
    def _three_session_listing() -> str:
        SEP = iterm_ctl.SEP
        return (
            f"1.1.1{SEP}ID-A{SEP}/dev/ttys001{SEP}alpha\n"
            f"1.1.2{SEP}ID-B{SEP}/dev/ttys002{SEP}beta\n"
            f"1.1.3{SEP}ID-C{SEP}/dev/ttys003{SEP}gamma\n"
        )

    def _post(self, port, path, body, headers=None):
        data = json.dumps(body).encode()
        base_headers = {"Content-Type": "application/json"}
        base_headers.update(headers or {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data, headers=base_headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # -- AC-1: the exploit is closed --
    def test_ac1_exploit_is_closed_zero_osascript_calls(self):
        listing_path = write_tmp(self._three_session_listing())
        fake_log = write_tmp("")
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path, ITERMON_FAKE_LOG=fake_log):
            with _web_admin_server() as port:
                status, payload = self._post(
                    port, "/api/send",
                    {"target": "__all__", "command": "echo pwned", "submit": True},
                    headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
                )
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "forbidden"})
        with open(fake_log, encoding="utf-8") as f:
            self.assertEqual(
                f.read(), "",
                "fake osascript recorded a call -- the exploit was NOT closed "
                "(the status-code assertion alone does not satisfy AC-1)",
            )

    # -- AC-2: Origin: null is rejected --
    def test_ac2_origin_null_is_rejected(self):
        listing_path = write_tmp(self._three_session_listing())
        fake_log = write_tmp("")
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path, ITERMON_FAKE_LOG=fake_log):
            with _web_admin_server() as port:
                status, payload = self._post(
                    port, "/api/send", {"target": "__all__", "command": "echo pwned"},
                    headers={"Origin": "null"},
                )
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "forbidden"})
        with open(fake_log, encoding="utf-8") as f:
            self.assertEqual(f.read(), "")

    # -- AC-3: DNS rebinding is closed, across every endpoint that touches osascript --
    def test_ac3_dns_rebinding_bad_host_rejected_across_endpoints(self):
        listing_path = write_tmp(self._three_session_listing())
        fake_log = write_tmp("")
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path, ITERMON_FAKE_LOG=fake_log):
            with _web_admin_server() as port:
                bad_host = f"evil.example:{port}"
                cases = [
                    ("GET", "/api/sessions", b""),
                    ("GET", "/api/logs", b""),
                    ("GET", "/api/jobs", b""),
                    ("POST", "/api/read", json.dumps({"target": "__all__"}).encode()),
                ]
                for method, path, body in cases:
                    headers = {"Host": bad_host}
                    if body:
                        headers["Content-Type"] = "application/json"
                    resp = _raw_request(port, method, path, headers, body=body)
                    self.assertEqual(
                        _raw_status(resp), 403, f"{method} {path} with a bad Host must be rejected"
                    )
        with open(fake_log, encoding="utf-8") as f:
            self.assertEqual(
                f.read(), "", "list_sessions()/read_contents() must never run for a rejected Host"
            )

    # -- AC-4: missing Host entirely is rejected --
    def test_ac4_missing_host_http10_is_rejected(self):
        with hermetic_env():
            with _web_admin_server() as port:
                resp = _raw_send_and_recv(port, b"GET /api/sessions HTTP/1.0\r\n\r\n")
        self.assertEqual(_raw_status(resp), 403)

    # -- AC-5: the guard runs before any work (jobs/create) --
    def test_ac5_guard_runs_before_any_work_no_job_created(self):
        with hermetic_env():
            with _web_admin_server() as port:
                status, _payload = self._post(
                    port, "/api/jobs/create",
                    {"name": "n", "target": "__all__", "command": "echo hi", "schedule": "0 * * * *"},
                    headers={"Origin": "https://evil.example"},
                )
        self.assertEqual(status, 403)
        self.assertFalse(
            os.path.exists(iterm_web.JOBS_FILE), "JOBS_FILE must never be written by a rejected request"
        )
        self.assertEqual(iterm_web.load_jobs(), [])

    # -- AC-6: no Origin header = unchanged (byte-identical to pre-fix) --
    def test_ac6_no_origin_header_send_is_unchanged(self):
        listing = f"1.1.1{iterm_ctl.SEP}ID-A{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}alpha\n"
        listing_path = write_tmp(listing)
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            with _web_admin_server() as port:
                status, payload = self._post(port, "/api/send", {"target": "1.1.1", "command": "echo hi"})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["sent"]), 1)
        self.assertEqual(payload["sent"][0]["index"], "1.1.1")
        self.assertEqual(iterm_web._log[-1]["kind"], "send")

    # AC-7 (0-match contract still holds) is deliberately NOT duplicated here
    # -- spec: "G10's AC-7 must still pass unmodified." It does (see G10's
    # test_ac7_api_send_zero_match_is_200_sent_empty_matched_0_error_log,
    # untouched by this branch); re-testing it here would just be a second
    # copy of the same assertion, not new coverage.

    # -- AC-8: every do_* verb is guarded, enumerated at runtime --
    def test_ac8_every_do_verb_is_guarded(self):
        with hermetic_env():
            with _web_admin_server() as port:
                verbs = sorted(
                    name[len("do_"):]
                    for name in dir(iterm_web.Handler)
                    if name.startswith("do_") and callable(getattr(iterm_web.Handler, name))
                )
                self.assertIn("GET", verbs)
                self.assertIn("POST", verbs)
                for verb in verbs:
                    resp = _raw_request(
                        port, verb, "/", {"Host": f"127.0.0.1:{port}", "Origin": "https://evil.example"}
                    )
                    self.assertEqual(_raw_status(resp), 403, f"do_{verb} is not guarded")

    # -- AC-9: same-origin is allowed, both the 127.0.0.1 and localhost spellings --
    def test_ac9_same_origin_127_is_allowed(self):
        listing = f"1.1.1{iterm_ctl.SEP}ID-A{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}alpha\n"
        listing_path = write_tmp(listing)
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            with _web_admin_server() as port:
                status, payload = self._post(
                    port, "/api/send", {"target": "1.1.1", "command": "echo hi"},
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["sent"]), 1)

    def test_ac9_same_origin_localhost_is_allowed(self):
        listing = f"1.1.1{iterm_ctl.SEP}ID-A{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}alpha\n"
        listing_path = write_tmp(listing)
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            with _web_admin_server() as port:
                body = json.dumps({"target": "1.1.1", "command": "echo hi"}).encode()
                headers = {
                    "Host": f"localhost:{port}",
                    "Origin": f"http://localhost:{port}",
                    "Content-Type": "application/json",
                }
                resp = _raw_request(port, "POST", "/api/send", headers, body=body)
        self.assertEqual(_raw_status(resp), 200)
        self.assertEqual(len(_raw_json_body(resp)["sent"]), 1)

    # -- AC-10: the allowlist follows the bound port, not a hard-coded 8765 --
    def test_ac10_allowlist_follows_bound_port_not_8765(self):
        listing = f"1.1.1{iterm_ctl.SEP}ID-A{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}alpha\n"
        listing_path = write_tmp(listing)
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            with _web_admin_server() as port:
                self.assertNotEqual(port, 8765)
                ok_status, ok_payload = self._post(
                    port, "/api/send", {"target": "1.1.1", "command": "echo hi"},
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                bad_status, _bad_payload = self._post(
                    port, "/api/send", {"target": "1.1.1", "command": "echo hi"},
                    headers={"Origin": "http://127.0.0.1:8765"},
                )
        self.assertEqual(ok_status, 200)
        self.assertEqual(len(ok_payload["sent"]), 1)
        self.assertEqual(bad_status, 403)

    # -- AC-11: --allow-origin works and is validated --
    def test_ac11_allow_origin_accepts_bare_scheme_host_port(self):
        iterm_web.validate_allow_origin("http://192.168.1.5:8765")  # must not raise
        iterm_web.validate_allow_origin(  # must not raise -- BACKLOG #13's future home
            "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
        )

    def test_ac11_allow_origin_refuses_star(self):
        with self.assertRaises(ValueError):
            iterm_web.validate_allow_origin("*")

    def test_ac11_allow_origin_refuses_path_or_trailing_slash(self):
        with self.assertRaises(ValueError):
            iterm_web.validate_allow_origin("http://192.168.1.5:8765/admin")
        with self.assertRaises(ValueError):
            iterm_web.validate_allow_origin("http://192.168.1.5:8765/")

    def test_ac11_extra_origin_is_honored_end_to_end(self):
        listing = f"1.1.1{iterm_ctl.SEP}ID-A{iterm_ctl.SEP}/dev/ttys001{iterm_ctl.SEP}alpha\n"
        listing_path = write_tmp(listing)
        iterm_web.EXTRA_ALLOWED_ORIGINS = ["chrome-extension://abcdefghijklmnopabcdefghijklmnop"]
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path):
            with _web_admin_server() as port:
                status, payload = self._post(
                    port, "/api/send", {"target": "1.1.1", "command": "echo hi"},
                    headers={"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"},
                )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["sent"]), 1)

    def test_ac11_cli_refuses_star_at_startup_nonzero_exit(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "iterm_web.py"), "--allow-origin", "*"],
            capture_output=True, text=True, timeout=10, cwd=REPO_ROOT, env=_build_hermetic_env(),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("allow-origin", proc.stderr.lower())

    def test_ac11_cli_refuses_path_at_startup_nonzero_exit(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "iterm_web.py"),
             "--allow-origin", "http://x.example/path"],
            capture_output=True, text=True, timeout=10, cwd=REPO_ROOT, env=_build_hermetic_env(),
        )
        self.assertNotEqual(proc.returncode, 0)

    # -- AC-12: rejection logging is rate-limited, with a suppressed count --
    def test_ac12_rejection_logging_rate_limited_with_suppressed_count(self):
        # A manually-advanced fake clock -- never a real sleep (spec 4.3).
        # Values: first rejection at t=0.0 (logs immediately); three more at
        # 0.01/0.02/0.03 (all within the 0.05s window -- suppressed); a
        # fifth at t=0.06 (window has elapsed since t=0.0 -- logs again,
        # naming the 3 suppressed in between).
        clock_values = iter([0.0, 0.01, 0.02, 0.03, 0.06])
        iterm_web._REJECTION_CLOCK = lambda: next(clock_values)
        iterm_web.REJECTION_LOG_WINDOW_SECONDS = 0.05
        with hermetic_env():
            with _web_admin_server() as port:
                for _ in range(5):
                    self._post(
                        port, "/api/send", {"target": "x", "command": "y"},
                        headers={"Origin": "https://evil.example"},
                    )
        error_entries = [e for e in iterm_web._log if e["kind"] == "error"]
        self.assertEqual(len(error_entries), 2, f"expected exactly 2 log entries, got {error_entries}")
        self.assertIn("+3 more suppressed", error_entries[1]["message"])

    # -- AC-13: no new per-request cost -- rejected requests spawn nothing --
    def test_ac13_rejected_requests_spawn_nothing(self):
        listing_path = write_tmp(self._three_session_listing())
        fake_log = write_tmp("")
        # Large window so the whole burst logs at most once (the ONE
        # deliberate, capped write spec 4.3 allows) -- this test is about
        # subprocess/JOBS_FILE, not about re-proving AC-12's rate limiting.
        iterm_web.REJECTION_LOG_WINDOW_SECONDS = 9999
        with hermetic_env(ITERMON_FAKE_LISTING=listing_path, ITERMON_FAKE_LOG=fake_log):
            with _web_admin_server() as port:
                for _ in range(20):
                    self._post(
                        port, "/api/send", {"target": "__all__", "command": "echo pwned"},
                        headers={"Origin": "https://evil.example"},
                    )
        with open(fake_log, encoding="utf-8") as f:
            self.assertEqual(f.read(), "", "a rejected request must never spawn osascript")
        self.assertFalse(os.path.exists(iterm_web.JOBS_FILE), "a rejected request must never touch JOBS_FILE")

    # -- AC-14: packaging unchanged, no version bump --
    def test_ac14_packaging_unchanged_files_and_no_version_bump(self):
        with open(os.path.join(REPO_ROOT, "package.json"), encoding="utf-8") as f:
            pkg = json.load(f)
        self.assertEqual(
            pkg["files"],
            ["iterm_ctl.py", "iterm_web.py", "iterm_mcp.py", "start.sh", "README.md", "LICENSE"],
        )
        self.assertEqual(pkg["version"], "1.3.0", "no version bump on this branch (spec section 8)")
        if shutil.which("npm"):
            proc = subprocess.run(
                ["npm", "pack", "--dry-run", "--json"],
                capture_output=True, text=True, cwd=REPO_ROOT, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            names = sorted(entry["path"] for entry in data[0]["files"])
            expected = sorted(
                ["LICENSE", "README.md", "iterm_ctl.py", "iterm_mcp.py", "iterm_web.py",
                 "package.json", "start.sh"]
            )
            self.assertEqual(names, expected)

    # -- AC-D1/AC-D2: documentation --
    def test_acd1_readme_no_auth_claim_replaced_with_accurate_statement(self):
        with open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("Bound to `127.0.0.1` only (local, no auth).", readme)
        self.assertIn("no authentication", readme.lower())
        self.assertIn("--allow-origin", readme)

    def test_acd2_changelog_unreleased_has_security_section(self):
        with open(os.path.join(REPO_ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            changelog = f.read()
        unreleased_idx = changelog.index("## [Unreleased]")
        next_heading_idx = changelog.index("## [1.3.0]")
        unreleased_block = changelog[unreleased_idx:next_heading_idx]
        self.assertIn("### Security", unreleased_block)
        self.assertIn("curl", unreleased_block.lower())


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
        G8TmuxBackendHermetic,
        G9TmuxBackendLive,
        G10ZeroMatchIsAFailure,
        G11OriginHostGuard,
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
