# Spec — MCP: unknown tool name returns an `isError` content result, not JSON-RPC `-32603`

- **Owner (PM):** Messi (usagemonitoring-product-manager, v275001)
- **Developer:** Gerrard (v475001) · **Tester:** Dida (v475002) · **Reviewer:** Ayala (code-reviewer)
- **Date:** 2026-07-29
- **Branch:** `feature/mcp-unknown-tool-error` → `develop`
- **Backlog item:** #2 *(future, non-blocking)* — found during Dida's v1.2.0 verification; pre-existing, not a defect
- **Release:** none. Lands on `develop` only; no version bump, no publish this cycle (rule 6).

## Problem

In `iterm_mcp.py`, the `tools/call` branch of `handle()` has two different error styles:

```python
    if method == "tools/call":
        params = msg.get("params", {})
        handler = HANDLERS.get(params.get("name"))
        if handler is None:
            raise ValueError(f"unknown tool: {params.get('name')!r}")   # line 140 — escapes the try
        try:
            text = handler(params.get("arguments") or {})
            is_error = False
        except Exception as exc:  # surfaced to the model, not the transport
            text = f"Error: {exc}"
            is_error = True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}  # line 147
```

The `raise` on line 140 sits **outside** the `try`, so it propagates to `main()`'s generic
handler and comes back as a raw JSON-RPC error:

```python
        except Exception as exc:
            reply["error"] = {"code": -32603, "message": str(exc)}       # line 168
```

So a *known* tool that blows up gets the soft, model-readable `isError` content result
(line 147), while an *unknown tool name* gets a transport-level `-32603`. MCP's own
convention — and what several clients handle most gracefully — is the soft form: tool-level
problems belong in the result, transport/protocol problems belong in `error`. A client that
doesn't render raw JSON-RPC errors well shows the user nothing useful, and the calling model
gets no text it can recover from.

This is a convention alignment, not a bug fix. Nothing crashes today.

## Required behavior

`tools/call` with a name not present in `HANDLERS` must return a normal JSON-RPC **result**
(no `error` key) of exactly the existing content shape:

```json
{"jsonrpc":"2.0","id":<id>,"result":{"content":[{"type":"text","text":"<message>"}],"isError":true}}
```

`<message>` must (a) say the tool is unknown, (b) include the offending name as given, and
(c) list the tools that *do* exist, so the model can self-correct without a `tools/list`
round-trip. Required form:

```
Unknown tool: 'frobnicate'. Available tools: list_sessions, read_screen, send_command
```

Implementation notes (developer's call on style, these are the constraints):

- Derive the available-tools list from `HANDLERS` at runtime (e.g. `", ".join(HANDLERS)`) —
  never a hard-coded literal, or it silently rots the next time a tool is added.
- Render the bad name with `!r` (matching the existing message) so `''`, whitespace, or a
  non-string name is unambiguous in the output.
- Keep the returned dict shape identical to line 147's — one construction site if practical.

### Edge case I am deciding here, so nobody has to guess

**`params` with no `name` key at all** (or `name: null`) hits this same branch, because
`HANDLERS.get(None)` is `None`. It **stays on this branch** and gets the same soft
`isError` result, rendering as `Unknown tool: None. Available tools: ...`. I considered
splitting it out as a JSON-RPC `-32602` invalid-params error and rejected it: that would be
a *new* behavior on a path the backlog item never asked about, it widens the blast radius of
a cosmetic change, and "you didn't name a tool" is exactly as recoverable-by-the-model as
"you named a tool that doesn't exist." Dida should test it; Ayala should read it as
intentional, not an oversight.

## Out of scope (explicitly — do not touch)

- **Unknown *method*** (`LookupError` → `-32601`, line 148/166). That is genuinely a
  protocol-level error and JSON-RPC `-32601` is correct. Unchanged.
- **Tool execution exceptions** (line 144–146) — already the soft form. Unchanged.
- Malformed JSON lines, notification handling (`"id" not in msg`), the generic `-32603`
  fallback in `main()` for anything else. All unchanged — the fallback stays in place, it
  simply stops being reachable via the unknown-tool path.
- The three tools' behavior, `iterm_ctl.py`, `iterm_web.py`, the CLI, `start.sh`.
- `package.json` `version` / `SERVER_INFO.version` — **no bump in this feature branch.**
- README: no change required (it documents tools and setup, not wire-level error shapes).

## Acceptance criteria

1. **Unknown tool → soft result.** `tools/call` with `{"name": "frobnicate", "arguments": {}}`
   returns a reply containing a `result` key and **no `error` key**, with
   `result.isError === true` and `result.content[0].text` containing both `frobnicate` and
   all three real tool names.
2. **Other error paths unchanged.** Verified individually:
   - unknown *method* (e.g. `"resources/list"`) still returns `error.code === -32601`;
   - a known tool whose execution raises still returns a `result` with `isError: true` and
     text starting `Error: ` (on a non-macOS host, calling `list_sessions` produces this
     naturally — no iTerm2 to talk to);
   - a malformed/non-JSON stdin line is still skipped silently with no reply;
   - a notification (no `id`) still gets no reply.
3. **No regression.** `initialize` still returns `serverInfo {name: itermon, version: 1.2.0}`
   and the tools capability; `tools/list` still returns the three tools with schemas;
   `list_sessions` / `read_screen` / `send_command` dispatch is untouched.
4. **Safety (product tester rule).** Verification must never send text to, or disturb, a live
   iTerm2 session. Every criterion above is reachable with `send_command` never being executed
   successfully — use the unknown-tool and non-macOS-failure paths. If verifying on macOS with
   iTerm2 present, use a dedicated throwaway session, never the operator's own.

### Regression baseline — read this, Dida

**This repo has no automated test suite** (no `test/`, no `npm test` script — confirmed
2026-07-29). "No regression to a currently-passing test" therefore means the v1.2.0
JSON-RPC stdio smoke test you ran on 2026-07-24 (`initialize` / `ping` / `tools/list` /
`tools/call`), re-run in full — not just the new case. Pipe one JSON message per line into
`python3 iterm_mcp.py` and assert on the parsed replies. If you write that smoke check down
as a re-runnable script rather than ad-hoc shell, say so in your report and I will consider
adopting it as this product's first committed test — but **do not** add a test framework,
dependency, or `npm test` wiring under this spec; propose it to me separately.

## Compatibility judgment (mine, on the record)

This changes the wire shape of one error path, so it is a **behavior change**, not a pure
internal refactor. I am accepting it deliberately:

- The `itermon` CLI, the web admin, and all three MCP tools' success paths are untouched —
  zero impact on the surface users actually script against.
- The only consumer that could notice is code asserting on `error.code === -32603` for a
  *misspelled tool name* — a path that exists to be recovered from, not depended on.
- It moves us **toward** the MCP convention, so clients that behave correctly today keep
  behaving correctly, and clients that render raw JSON-RPC errors badly get better.

Semver at release time: **patch** if it ships alone, or folded silently into the next minor
if it rides along with a feature. Not breaking. Whoever prepares the release (Lampard) must
still call it out in the release notes — a behavior change gets mentioned even when it is
small, or the changelog is lying by omission.

## Gates (rule 15)

`feature/mcp-unknown-tool-error` cut off `develop`. Merge to `develop` **only** after Dida
PASS **and** Ayala APPROVE. `develop` → `main` and any npm publish stay CEO-gated (rule 6)
and are explicitly **not** part of this item. I (Messi) do the final ACCEPT / REQUEST CHANGES
before it counts as done (rule 17).
