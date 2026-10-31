"""Times Claude Code exactly the way the voice worker runs it, for glasses-doctor.

Prints one JSON object per line: {"test", "ok", "first_s", "total_s", "reply", "tools", ...}.
Anything that would need a yes is turned down, so the tests change nothing.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_lane import ApiError, Done, Lane, Permission, Text, ToolStart  # noqa: E402
from worker import brain_config, read_persona  # noqa: E402

TESTS = [
    ("simple question", "Health check from glasses-doctor: reply with just the word OK."),
    (
        "email check",
        "Health check from glasses-doctor: check my Gmail for unread emails from the last day and reply "
        "with just how many there are. Don't open any of them.",
    ),
]


async def timed(lane: Lane, text: str, limit: float) -> dict:
    started = time.monotonic()
    turn = await lane.ask(text, text)
    first = None
    parts: list[str] = []
    tools: list[str] = []
    done: Done | None = None
    api_error: ApiError | None = None

    async def follow() -> None:
        nonlocal first, done, api_error
        async for event in turn.events():
            if isinstance(event, Text):
                first = first or time.monotonic()
                parts.append(event.text)
            elif isinstance(event, ToolStart):
                tools.append(event.name)
            elif isinstance(event, Permission):
                await lane.answer(event, False, "This is a health check; don't do anything that needs approval.")
            elif isinstance(event, ApiError):
                api_error = event
            elif isinstance(event, Done):
                done = event

    try:
        await asyncio.wait_for(follow(), limit)
    except asyncio.TimeoutError:
        await lane.interrupt()
    return {
        "ok": bool(done and done.ok and not api_error),
        "first_s": round(first - started, 1) if first else None,
        "total_s": round(time.monotonic() - started, 1) if done else None,
        "reply": (done.text if done and done.text else "".join(parts)).strip()[:300],
        "tools": tools,
        "error": (
            f"{api_error.kind}: {api_error.text}" if api_error
            else None if done and done.ok
            else (done.text or done.error or "failed") if done
            else f"no answer within {limit:.0f}s"
        ),
        "api_error": api_error.kind if api_error else None,
    }


async def main() -> None:
    config = brain_config(read_persona()).lane
    lane = Lane(config, "doctor")
    await lane.start()
    try:
        with __import__("contextlib").suppress(asyncio.TimeoutError):
            await asyncio.wait_for(lane.ready.wait(), 60)
        print(json.dumps({"test": "startup", "ok": lane.alive, "total_s": round(lane.ready_after or 0, 1),
                          "model": config.model, "effort": config.effort, "cwd": config.cwd}), flush=True)
        for name, text in TESTS:
            if not lane.alive:
                break
            result = await timed(lane, text, 120)
            result["test"] = name
            if name == "simple question":
                result["connectors"] = lane.mcp_status
                result["credentials"] = lane.api_key_source
            print(json.dumps(result), flush=True)
    finally:
        await lane.close()


if __name__ == "__main__":
    asyncio.run(main())
