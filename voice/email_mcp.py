"""An MCP server (stdio) that gives Claude Code every email account set up
with glasses-email: search and read all inboxes at once, draft, and send from
any of them. Sending is never pre-approved, so Claude Code asks first.

The voice worker starts it for its Claude Code sessions. To use it in your
own Claude Code sessions too:
    claude mcp add email --scope user -- ~/.claude-glasses-bridge/voice-venv/bin/python ~/claude-glasses-bridge/voice/email_mcp.py
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from email_accounts import Mail  # noqa: E402

PROTOCOL = "2025-06-18"

ACCOUNT = {"type": "string", "description": "An email address from list_accounts, or part of one (e.g. 'gmail')."}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_accounts",
        "description": "The user's email accounts that can be searched, read and sent from.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_email",
        "description": (
            "Search the user's email, in every account at once unless one is named. Returns sender, subject, "
            "date, unread state and a short snippet per message, with an id for read_email. On Gmail accounts "
            "the query uses Gmail search syntax (from:, subject:, has:attachment ...); elsewhere it matches text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {**ACCOUNT, "description": "Which account, or 'all' (the default)."},
                "query": {"type": "string", "description": "What to look for; empty lists the newest mail."},
                "unread_only": {"type": "boolean"},
                "days": {"type": "integer", "description": "How far back to look (default 7)."},
                "limit": {"type": "integer", "description": "Most messages per account (default 10)."},
                "folder": {"type": "string", "description": "Default INBOX."},
            },
        },
    },
    {
        "name": "read_email",
        "description": "The full text of one message, by the id search_email gave.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "mark_read",
        "description": "Mark one message as read, by id.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "draft_email",
        "description": "Save a draft in one of the user's accounts, without sending it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_account": ACCOUNT,
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain text."},
                "cc": {"type": "string"},
                "reply_to_id": {"type": "string", "description": "id of the message this replies to."},
            },
            "required": ["from_account", "to", "body"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Send an email from one of the user's accounts. Only after the user has heard what will be sent "
            "and said yes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_account": ACCOUNT,
                "to": {"type": "string", "description": "Comma-separated addresses."},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain text."},
                "cc": {"type": "string"},
                "bcc": {"type": "string"},
                "reply_to_id": {"type": "string", "description": "id of the message this replies to."},
            },
            "required": ["from_account", "to", "body"],
        },
    },
]


class Server:
    def __init__(self, mail: Mail | None = None) -> None:
        self.mail = mail or Mail()
        self.calls: dict[str, Callable[..., Any]] = {
            "list_accounts": lambda: [
                {"address": a.address, "provider": a.provider, "name": a.name} for a in self.mail.accounts()
            ],
            "search_email": self.mail.search,
            "read_email": self.mail.read,
            "mark_read": self.mail.mark_read,
            "draft_email": self.mail.draft,
            "send_email": self.mail.send,
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        if "id" not in message:
            return None  # a notification, such as notifications/initialized
        mid = message["id"]
        if method == "initialize":
            version = (message.get("params") or {}).get("protocolVersion") or PROTOCOL
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "glasses-email", "version": "1.0"},
                "instructions": "The user's email accounts. Ask before sending anything.",
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name", "")
            call = self.calls.get(name)
            if call is None:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
            try:
                value = call(**(params.get("arguments") or {}))
                text = value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False)
                return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True,
                }}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                reply = self.handle(message)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                reply = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32603, "message": "internal error"}}
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


if __name__ == "__main__":
    Server().serve()
