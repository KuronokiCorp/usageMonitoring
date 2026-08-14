#!/usr/bin/env python3
"""Mutation-testing harness for the itermon test suite (BACKLOG #3, spec
section 5, "non-vacuity"). Run with:

    python3 tests/counter_test.py

This is the part L6 is actually about: a test suite that is green proves
nothing by itself unless it can also be shown to go red. This script:

  1. Copies the runtime files this suite pins (iterm_ctl.py, iterm_mcp.py,
     iterm_web.py) plus the tests/ tree into a throwaway tempfile.mkdtemp()
     directory -- never touches the working tree.
  2. Confirms the suite is green against an *unmutated* copy first (a false
     "every mutation was caught" result is meaningless if the baseline was
     already red for an unrelated reason).
  3. Applies each mutation from MUTATIONS, one at a time, to a fresh copy,
     and runs the suite against the mutated copy.
  4. Asserts each mutation makes the suite FAIL, and records which test(s)
     caught it.
  5. Exits 0 only if the baseline was green AND every mutation in MUTATIONS
     was caught (see len(MUTATIONS) below -- not hand-counted in this
     docstring, so it can't go stale again). Any survivor is reported by
     name and the harness exits 1.

Exits non-zero (and says why) if the working tree differs before vs. after --
this harness must never be the thing that dirties the repo it's testing.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# chr()-built building blocks so the mutation text below never needs
# hand-counted, multiply-escaped backslash literals in this file's own
# source -- see the M2 mutation, which patches a line that itself contains
# AppleScript-escaping backslashes.
BS = chr(92)  # \
DQ = chr(34)  # "
SQ = chr(39)  # '


def _m2_old_return_line() -> str:
    # Exact current source of iterm_ctl.as_str()'s return line:
    #   return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return (
        "    return "
        + SQ + DQ + SQ
        + " + value.replace("
        + DQ + BS + BS + DQ
        + ", "
        + DQ + BS + BS + BS + BS + DQ
        + ").replace("
        + SQ + DQ + SQ
        + ", "
        + SQ + BS + BS + DQ + SQ
        + ") + "
        + SQ + DQ + SQ
        + "\n"
    )


def _m2_new_return_line() -> str:
    # Mutated: drops the backslash-escape .replace() call entirely.
    #   return '"' + value.replace('"', '\\"') + '"'
    return (
        "    return "
        + SQ + DQ + SQ
        + " + value.replace("
        + SQ + DQ + SQ
        + ", "
        + SQ + BS + BS + DQ + SQ
        + ") + "
        + SQ + DQ + SQ
        + "\n"
    )


MUTATIONS = [
    dict(
        id="M1",
        file="iterm_ctl.py",
        group="G1 (resolve_targets)",
        old=(
            "    # bare string -> treat as name substring\n"
            "    return [s for s in sessions if target.lower() in s.name.lower()]\n"
        ),
        new=(
            "    # bare string -> treat as name substring\n"
            "    return [s for s in sessions if re.search(target, s.name, re.IGNORECASE)]\n"
        ),
    ),
    dict(
        id="M2",
        file="iterm_ctl.py",
        group="G2 (as_str escaping)",
        old=_m2_old_return_line(),
        new=_m2_new_return_line(),
    ),
    dict(
        id="M3",
        file="iterm_ctl.py",
        group="G4 (send_text newline)",
        old=(
            "    def send_text(self, session: Session, text: str, enter: bool) -> None:\n"
            "        newline = \"yes\" if enter else \"no\"\n"
        ),
        new=(
            "    def send_text(self, session: Session, text: str, enter: bool) -> None:\n"
            "        newline = \"no\" if enter else \"yes\"\n"
        ),
    ),
    dict(
        id="M4",
        file="iterm_ctl.py",
        group="G5 (_annotate_jobs foreground selection)",
        old=(
            '            if "+" in stat:  # process group in the foreground\n'
            "                fg_pid, fg_comm = pid, comm\n"
        ),
        new=(
            '            if "+" in stat:  # process group in the foreground\n'
            "                if fg_comm:\n"
            "                    continue\n"
            "                fg_pid, fg_comm = pid, comm\n"
        ),
    ),
    dict(
        id="M6",
        file="iterm_ctl.py",
        group="G1 (resolve_targets name: search vs match, BACKLOG 3a)",
        old=(
            "    if target.startswith(\"name:\"):\n"
            "        pat = re.compile(target[5:], re.IGNORECASE)\n"
            "        return [s for s in sessions if pat.search(s.name)]\n"
        ),
        new=(
            "    if target.startswith(\"name:\"):\n"
            "        pat = re.compile(target[5:], re.IGNORECASE)\n"
            "        return [s for s in sessions if pat.match(s.name)]\n"
        ),
    ),
    dict(
        id="M7",
        file="iterm_ctl.py",
        group="G8 (_tmux_env LC_ALL fix, BACKLOG round 4, Ayala's finding)",
        old=(
            "    env = dict(os.environ)\n"
            "    env[\"LC_ALL\"] = \"C.UTF-8\"\n"
            "    return env\n"
        ),
        new=(
            "    env = dict(os.environ)\n"
            "    return env\n"
        ),
    ),
    dict(
        id="M8",
        file="iterm_web.py",
        group="G10 (run_job 0-match failure, spec stable-job-targets-and-zero-match-failure S2)",
        old=(
            '    hits = do_send(target, command, submit)\n'
            "    if len(hits) == 0:\n"
            "        log_event(\n"
            '            "error",\n'
            "            f'job \"{name}\" ({target}) matched 0 sessions — NOT DELIVERED: {command!r}',\n"
            "        )\n"
            '        return {"status": "MATCHED 0 SESSIONS — not delivered", "sent": 0}\n'
            '    who = ", ".join(h["index"] for h in hits)\n'
        ),
        new=(
            '    hits = do_send(target, command, submit)\n'
            '    who = ", ".join(h["index"] for h in hits) or "no match"\n'
        ),
    ),
    dict(
        id="M5",
        file="iterm_mcp.py",
        group="G6 (MCP unknown-tool soft error, bb119d8)",
        old=(
            "        if handler is None:\n"
            '            available = ", ".join(HANDLERS)\n'
            "            text = f\"Unknown tool: {params.get('name')!r}. Available tools: {available}\"\n"
            "            is_error = True\n"
            "        else:\n"
            "            try:\n"
            "                text = handler(params.get(\"arguments\") or {})\n"
            "                is_error = False\n"
            "            except Exception as exc:  # surfaced to the model, not the transport\n"
            "                text = f\"Error: {exc}\"\n"
            "                is_error = True\n"
        ),
        new=(
            "        if handler is None:\n"
            "            raise ValueError(f\"unknown tool: {params.get('name')!r}\")\n"
            "        try:\n"
            "            text = handler(params.get(\"arguments\") or {})\n"
            "            is_error = False\n"
            "        except Exception as exc:  # surfaced to the model, not the transport\n"
            "            text = f\"Error: {exc}\"\n"
            "            is_error = True\n"
        ),
    ),
]


def _fresh_copy() -> str:
    """A throwaway copy of the repo files this suite touches: iterm_ctl.py,
    iterm_mcp.py, and (as of docs/specs/stable-job-targets-and-zero-match-
    failure.md's G10 tests) iterm_web.py -- run_job()/the /api/send handler
    are exercised for real, in-process, against a throwaway HTTP server on
    an ephemeral port; only start.sh stays copied inert (nothing in the
    suite reaches it, kept only so the tree shape is honest), plus the
    tests/ directory itself."""
    tmp = tempfile.mkdtemp(prefix="itermon-counter-test-")
    for name in ("iterm_ctl.py", "iterm_mcp.py", "iterm_web.py", "start.sh"):
        src = os.path.join(REPO_ROOT, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(tmp, name))
    shutil.copytree(os.path.join(REPO_ROOT, "tests"), os.path.join(tmp, "tests"))
    return tmp


def _apply_mutation(tmp: str, mutation: dict) -> None:
    path = os.path.join(tmp, mutation["file"])
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if mutation["old"] not in content:
        raise AssertionError(
            f"{mutation['id']}: anchor text not found in {mutation['file']} -- "
            "the source has drifted from what this harness expects; fix the "
            "mutation table, don't silently skip it."
        )
    mutated = content.replace(mutation["old"], mutation["new"], 1)
    if mutated == content:
        raise AssertionError(f"{mutation['id']}: replace() was a no-op")
    with open(path, "w", encoding="utf-8") as f:
        f.write(mutated)


def _run_suite(tmp: str):
    proc = subprocess.run(
        [sys.executable, os.path.join(tmp, "tests", "run_tests.py")],
        capture_output=True,
        text=True,
        cwd=tmp,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _failed_test_names(stderr: str) -> list:
    return sorted(set(re.findall(r"^(?:FAIL|ERROR): (\S+ \([^)]*\))", stderr, re.M)))


def main() -> int:
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout

    ok = True

    baseline = _fresh_copy()
    try:
        code, _out, err = _run_suite(baseline)
        if code != 0:
            print("BASELINE (unmutated copy) is NOT green -- the harness itself is")
            print("meaningless until this is fixed. Suite output:")
            print(err)
            ok = False
        else:
            print("baseline (unmutated copy): green, as expected.")
    finally:
        shutil.rmtree(baseline, ignore_errors=True)

    print()
    survivors = []
    for mutation in MUTATIONS:
        tmp = _fresh_copy()
        try:
            _apply_mutation(tmp, mutation)
            code, _out, err = _run_suite(tmp)
            caught_by = _failed_test_names(err)
            if code == 0:
                print(f"{mutation['id']} ({mutation['group']}): SURVIVED -- suite stayed green. HOLE.")
                survivors.append(mutation["id"])
                ok = False
            else:
                names = ", ".join(caught_by) if caught_by else "(suite failed, but no FAIL/ERROR line matched -- check output format)"
                print(f"{mutation['id']} ({mutation['group']}): caught by {names}")
                if not caught_by:
                    ok = False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    after = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout
    print()
    if before != after:
        print("WORKING TREE CHANGED during this run -- that must never happen.")
        print("before:\n" + before)
        print("after:\n" + after)
        ok = False
    else:
        print("working tree unchanged before/after (git status --porcelain identical). OK")

    print()
    if ok:
        print(f"counter_test: OK -- baseline green, all {len(MUTATIONS)} mutations caught.")
        return 0
    if survivors:
        print(f"counter_test: FAIL -- surviving mutation(s): {', '.join(survivors)}")
    else:
        print("counter_test: FAIL -- see above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
