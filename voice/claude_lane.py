"""One Claude Code process, kept running for a whole call.

Claude Code runs headless (`claude -p`) with streaming input and output: each
thing the user says is written to its stdin as a JSON line, and its answer
comes back token by token, so the first words can be spoken while the rest is
still being written. The process keeps its own conversation history, so it
remembers everything said earlier in the call.

Nothing here knows about LiveKit; brain.py decides what to say and when.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Union

log = logging.getLogger("glasses.voice")

# Read-only tools of the Gmail, Calendar and Drive connectors, under both the
# names the small-business plugin gives them and the names claude.ai connectors
# get. Pre-approving them skips a permission check on every email question.
READ_ONLY_TOOLS = [
    f"mcp__{server}__{tool}"
    for server in ("plugin_small-business_gmail", "claude_ai_Gmail", "Gmail")
    for tool in ("search_threads", "get_thread", "get_message", "list_labels", "list_drafts", "get_draft")
] + [
    f"mcp__{server}__{tool}"
    for server in ("plugin_small-business_google-calendar", "claude_ai_Google_Calendar", "Google_Calendar")
    for tool in ("list_events", "search_events", "get_event", "list_calendars", "suggest_time")
] + [
    f"mcp__{server}__{tool}"
    for server in ("plugin_small-business_google-drive", "claude_ai_Google_Drive", "Google_Drive")
    for tool in ("search_files", "read_file_content", "get_file_metadata", "list_recent_files")
] + [
    # glasses-email (email_mcp.py): everything but sending
    f"mcp__email__{tool}" for tool in ("list_accounts", "search_email", "read_email", "mark_read", "draft_email")
] + ["WebSearch", "WebFetch"]


# Acting in the user's name always gets a spoken "is that okay?", whatever the
# permission mode: auto mode alone would let a request that merely claims the
# user agreed go through.
ALWAYS_ASK = ["mcp__email__send_email"] + [
    f"mcp__{server}__{tool}"
    for server in ("claude_ai_Gmail", "Gmail")
    for tool in ("send_message", "reply", "forward", "send_draft")
]


@dataclass
class LaneConfig:
    cwd: str
    claude: str = "claude"
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    add_dirs: list[str] = field(default_factory=list)
    system_prompt_file: str | None = None
    allowed_tools: list[str] = field(default_factory=lambda: list(READ_ONLY_TOOLS))
    ask_tools: list[str] = field(default_factory=lambda: list(ALWAYS_ASK))
    resume: str | None = None
    env: dict[str, str] | None = None
    # With ANTHROPIC_API_KEY in the environment, `claude -p` always bills that
    # key instead of the user's Claude subscription, so it is left out unless asked for.
    use_api_key: bool = False
    # The quick voice up front: no tools, no connectors, and its own short
    # system prompt instead of Claude Code's, so it answers fast.
    talk_only: bool = False
    # Claude in Chrome: browse and act in the user's own Chrome.
    chrome: bool = False
    # More MCP servers (such as glasses-email), on top of the user's own.
    mcp_config: str | None = None

    def command(self) -> list[str]:
        cmd = [
            self.claude, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            # Permission prompts come to us as control requests, to be asked aloud.
            "--permission-prompt-tool", "stdio",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.talk_only:
            cmd += ["--tools", "", "--strict-mcp-config", "--setting-sources", ""]
            if self.system_prompt_file:
                cmd += ["--system-prompt-file", self.system_prompt_file]
            return cmd + (["--resume", self.resume] if self.resume else [])
        for d in self.add_dirs:
            cmd += ["--add-dir", d]
        if self.chrome:
            cmd += ["--chrome"]
        if self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config]
        if self.system_prompt_file:
            cmd += ["--append-system-prompt-file", self.system_prompt_file]
        if self.allowed_tools:
            cmd += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.ask_tools:
            cmd += ["--settings", json.dumps({"permissions": {"ask": self.ask_tools}})]
        if self.resume:
            cmd += ["--resume", self.resume]
        return cmd


@dataclass
class Text:
    """A piece of what Claude is writing to the user."""
    text: str


@dataclass
class BlockEnd:
    """Claude finished a paragraph of text (a content block)."""


@dataclass
class ToolStart:
    name: str


@dataclass
class Permission:
    request_id: str
    tool_name: str
    input: dict[str, Any]
    description: str

    def spoken(self) -> str:
        """What to ask the user, in a few words."""
        tool = self.tool_name.split("__")[-1]
        get = lambda key: str(self.input.get(key) or "").strip()  # noqa: E731
        if tool in ("send_email", "send_message", "reply", "forward") and get("to"):
            sender = get("from_account")
            what = f"send an email{' from ' + sender if sender else ''} to {get('to')}"
            if get("subject"):
                what += f", subject: {get('subject')[:80]}"
            return what
        what = self.description.strip().rstrip(".")
        if not what:
            detail = get("command") or get("file_path") or get("url") or get("text")
            what = f"use {tool.replace('_', ' ')}"
            if detail:
                what += f": {detail[:80]}"
        return what


@dataclass
class ApiError:
    """Claude Code could not get an answer from Claude: billing, login, limits."""
    kind: str  # billing_error, authentication_failed, rate_limit, server_error, invalid_request, unknown
    text: str


@dataclass
class Done:
    text: str
    ok: bool
    session_id: str | None = None
    duration_ms: int | None = None
    error: str | None = None


Event = Union[Text, BlockEnd, ToolStart, Permission, ApiError, Done]


class Turn:
    """One request and everything Claude does about it, until its result."""

    def __init__(self, lane: "Lane", request: str) -> None:
        self.lane = lane
        self.request = request
        self.started = time.monotonic()
        self.result: Done | None = None
        self._events: asyncio.Queue[Event] = asyncio.Queue()

    @property
    def finished(self) -> bool:
        return self.result is not None

    def _put(self, event: Event) -> None:
        if isinstance(event, Done):
            if self.result is not None:
                return
            self.result = event
        self._events.put_nowait(event)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            event = await self._events.get()
            yield event
            if isinstance(event, Done):
                return


class Lane:
    """A Claude Code process that takes one request at a time."""

    def __init__(self, config: LaneConfig, name: str = "lane") -> None:
        self.config = config
        self.name = name
        self.session_id: str | None = None
        self.api_key_source: str | None = None
        self.mcp_status: dict[str, str] = {}
        self.turn: Turn | None = None
        self.ready = asyncio.Event()
        self.started_at = 0.0
        self.ready_after: float | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._counter = 0
        self._streaming_seen = False
        self._block_types: dict[int, str] = {}
        self._pending_permissions: set[str] = set()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def busy(self) -> bool:
        return self.turn is not None and not self.turn.finished

    async def start(self) -> None:
        cmd = self.config.command()
        env = {**os.environ, **(self.config.env or {})}
        # Keep a parent Claude Code session's identity out of this one.
        for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(key, None)
        if not self.config.use_api_key and env.pop("ANTHROPIC_API_KEY", None):
            log.info("%s: leaving ANTHROPIC_API_KEY out, so Claude Code uses your Claude login", self.name)
        self.started_at = time.monotonic()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.config.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        log.info("%s: started claude (pid %s) in %s", self.name, self._proc.pid, self.config.cwd)
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._read_stderr())
        await self._send({
            "type": "control_request",
            "request_id": self._next_id("init"),
            "request": {"subtype": "initialize", "hooks": None},
        })

    async def ask(self, content: str | list[dict[str, Any]], request: str) -> Turn:
        """Send what the user said. content is plain text or a list of content
        blocks (text and images); request is the user's words, for logs."""
        if self.busy:
            raise RuntimeError(f"{self.name} is busy")
        if not self.alive:
            raise RuntimeError(f"{self.name} is not running")
        turn = Turn(self, request)
        self.turn = turn
        self._block_types.clear()
        await self._send({
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        })
        return turn

    async def interrupt(self) -> None:
        if self.alive:
            await self._send({
                "type": "control_request",
                "request_id": self._next_id("interrupt"),
                "request": {"subtype": "interrupt"},
            })

    async def answer(self, permission: Permission, allow: bool, message: str = "") -> None:
        if permission.request_id not in self._pending_permissions:
            return
        self._pending_permissions.discard(permission.request_id)
        response: dict[str, Any] = (
            {"behavior": "allow", "updatedInput": permission.input}
            if allow
            else {"behavior": "deny", "message": message or "The user said no."}
        )
        await self._send({
            "type": "control_response",
            "response": {"subtype": "success", "request_id": permission.request_id, "response": response},
        })

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
        for task in (self._reader, self._stderr):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    def _next_id(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}_{self._counter}_{os.urandom(3).hex()}"

    async def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise RuntimeError(f"{self.name} is not running")
        async with self._write_lock:
            proc.stdin.write((json.dumps(message) + "\n").encode())
            await proc.stdin.drain()

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for raw in self._proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.warning("%s stderr: %s", self.name, line[:500])

    async def _read(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            async for raw in self._proc.stdout:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("%s: unreadable line: %r", self.name, raw[:200])
                    continue
                try:
                    await self._handle(message)
                except Exception:
                    log.exception("%s: could not handle %s", self.name, message.get("type"))
        finally:
            code = await self._proc.wait()
            log.warning("%s: claude exited with code %s", self.name, code)
            self.ready.set()
            if self.turn and not self.turn.finished:
                self.turn._put(Done(text="", ok=False, error=f"Claude Code stopped (exit code {code})"))

    def _emit(self, event: Event) -> None:
        if self.turn and not self.turn.finished:
            self.turn._put(event)

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        top_level = message.get("parent_tool_use_id") is None

        if kind == "control_response":
            request_id = (message.get("response") or {}).get("request_id", "")
            if request_id.startswith("init_"):
                self._mark_ready()
            return

        if kind == "system":
            if message.get("subtype") == "init":
                self.session_id = message.get("session_id") or self.session_id
                source = message.get("apiKeySource")
                if source and source != self.api_key_source:
                    self.api_key_source = source
                    log.info("%s: Claude credentials: %s", self.name, source)
                for server in message.get("mcp_servers") or []:
                    self.mcp_status[server.get("name", "?")] = server.get("status", "?")
                failed = [n for n, s in self.mcp_status.items() if s not in ("connected", "pending")]
                if failed:
                    log.warning("%s: connectors not connected: %s", self.name, ", ".join(failed))
                self._mark_ready()
            elif message.get("subtype") == "api_retry":
                log.warning("%s: Claude API retry: %s", self.name, json.dumps(message)[:300])
            return

        if kind == "stream_event":
            if not top_level:
                return  # a subagent's output, not words for the user
            event = message.get("event") or {}
            etype = event.get("type")
            index = event.get("index", 0)
            if etype == "content_block_start":
                self._streaming_seen = True
                block = event.get("content_block") or {}
                self._block_types[index] = block.get("type", "")
                if block.get("type") in ("tool_use", "server_tool_use"):
                    self._emit(ToolStart(block.get("name", "tool")))
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self._streaming_seen = True
                    self._emit(Text(delta["text"]))
            elif etype == "content_block_stop":
                if self._block_types.get(index) == "text":
                    self._emit(BlockEnd())
            return

        if kind == "assistant":
            content = (message.get("message") or {}).get("content") or []
            if top_level and message.get("error"):
                text = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                log.error("%s: Claude API error (%s): %s", self.name, message["error"], text[:300])
                self._emit(ApiError(str(message["error"]), text))
                return
            # Older Claude Code without partial messages: whole blocks only.
            if not top_level or self._streaming_seen:
                return
            for block in content:
                if block.get("type") == "text" and block.get("text"):
                    self._emit(Text(block["text"]))
                    self._emit(BlockEnd())
                elif block.get("type") == "tool_use":
                    self._emit(ToolStart(block.get("name", "tool")))
            return

        if kind == "control_request":
            request = message.get("request") or {}
            request_id = message.get("request_id", "")
            if request.get("subtype") == "can_use_tool":
                permission = Permission(
                    request_id=request_id,
                    tool_name=request.get("tool_name", "tool"),
                    input=request.get("input") or {},
                    description=request.get("description") or request.get("title") or "",
                )
                self._pending_permissions.add(request_id)
                if self.busy:
                    self._emit(permission)
                else:
                    await self.answer(permission, False, "No one is there to approve this.")
            else:
                await self._send({
                    "type": "control_response",
                    "response": {"subtype": "error", "request_id": request_id,
                                 "error": f"unsupported: {request.get('subtype')}"},
                })
            return

        if kind == "result":
            self.session_id = message.get("session_id") or self.session_id
            ok = message.get("subtype") == "success" and not message.get("is_error")
            result = message.get("result")
            done = Done(
                text=result if isinstance(result, str) else "",
                ok=ok,
                session_id=self.session_id,
                duration_ms=message.get("duration_ms"),
                error=None if ok else str(message.get("subtype") or "error"),
            )
            self._emit(done)
            return

        if kind == "rate_limit_event":
            info = message.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed"):
                log.warning("%s: rate limit: %s", self.name, json.dumps(info)[:300])

    def _mark_ready(self) -> None:
        if not self.ready.is_set():
            self.ready_after = time.monotonic() - self.started_at
            log.info("%s: ready after %.1fs", self.name, self.ready_after)
            self.ready.set()
