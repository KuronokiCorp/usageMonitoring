#!/usr/bin/env python3
"""itermon MCP server — expose iTerm2 session monitoring/control over MCP stdio.

Zero dependencies (pure standard library), same as the rest of itermon.
Speaks JSON-RPC 2.0, one message per line, per the MCP stdio transport.

Register in a project .mcp.json:

    {
      "mcpServers": {
        "itermon": {
          "command": "python3",
          "args": ["/absolute/path/to/iterm_mcp.py"]
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import sys

# When installed as a global npm bin, this file is invoked through a symlink
# (node_modules/.bin/itermon-mcp). Ensure the real directory holding
# iterm_ctl.py is importable regardless of how we were launched.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import iterm_ctl

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "itermon", "version": "1.2.0"}

TARGET_HELP = (
    "Session selector: an index like '2.1.1' (window.tab.session), 'id:<uuid-prefix>', "
    "'tty:<suffix>', 'name:<regex>', or a bare string matched against session titles."
)

TOOLS = [
    {
        "name": "list_sessions",
        "description": (
            "List every iTerm2 session across all windows/tabs with index, UUID, tty, "
            "title, and foreground job."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_screen",
        "description": "Read the visible screen contents of the matching iTerm2 session(s).",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": TARGET_HELP}},
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_command",
        "description": (
            "Type text into the matching iTerm2 session(s), optionally pressing Enter. "
            "Refuses to run when the target matches nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": TARGET_HELP},
                "command": {"type": "string", "description": "Text to send to the session."},
                "enter": {
                    "type": "boolean",
                    "description": "Press Enter after the text (default true).",
                },
            },
            "required": ["target", "command"],
            "additionalProperties": False,
        },
    },
]


def _sessions_for(target: str) -> list:
    sessions = iterm_ctl.list_sessions()
    matches = iterm_ctl.resolve_targets(sessions, target, all_flag=False)
    if not matches:
        raise ValueError(f"no session matches target {target!r}")
    return matches


def tool_list_sessions(_args: dict) -> str:
    sessions = iterm_ctl.list_sessions()
    if not sessions:
        return "No iTerm2 sessions found (is iTerm2 running?)."
    rows = [
        {"index": s.index, "id": s.id, "tty": s.tty, "name": s.name, "job": s.job}
        for s in sessions
    ]
    return json.dumps(rows, ensure_ascii=False, indent=2)


def tool_read_screen(args: dict) -> str:
    parts = []
    for s in _sessions_for(args["target"]):
        parts.append(f"== {s.index} {s.name} ({s.tty})\n{iterm_ctl.read_contents(s).rstrip()}")
    return "\n\n".join(parts)


def tool_send_command(args: dict) -> str:
    enter = args.get("enter", True)
    matches = _sessions_for(args["target"])
    for s in matches:
        iterm_ctl.send_text(s, args["command"], enter)
    sent_to = ", ".join(f"{s.index} {s.name}" for s in matches)
    return f"Sent to {len(matches)} session(s): {sent_to}"


HANDLERS = {
    "list_sessions": tool_list_sessions,
    "read_screen": tool_read_screen,
    "send_command": tool_send_command,
}


def handle(msg: dict):
    method = msg.get("method")
    if method == "initialize":
        return {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = msg.get("params", {})
        handler = HANDLERS.get(params.get("name"))
        if handler is None:
            raise ValueError(f"unknown tool: {params.get('name')!r}")
        try:
            text = handler(params.get("arguments") or {})
            is_error = False
        except Exception as exc:  # surfaced to the model, not the transport
            text = f"Error: {exc}"
            is_error = True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    raise LookupError(f"method not found: {method!r}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:  # notification (e.g. notifications/initialized) — no reply
            continue
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        try:
            reply["result"] = handle(msg)
        except LookupError as exc:
            reply["error"] = {"code": -32601, "message": str(exc)}
        except Exception as exc:
            reply["error"] = {"code": -32603, "message": str(exc)}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
