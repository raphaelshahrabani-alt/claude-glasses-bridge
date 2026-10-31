"""Times Claude Code exactly the way the voice worker runs it, for glasses-doctor.

Prints one JSON object per line: {"test", "ok", "first_s", "total_s", "reply", "tools", ...}.
Anything that would need a yes is turned down, so the tests change nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_lane import ApiError, Done, Lane, Permission, Text, ToolStart  # noqa: E402
from worker import brain_config, read_persona  # noqa: E402

TALK_TESTS = [
    ("quick answer", "Health check from glasses-doctor: just say OK."),
    ("hand-off", "Check my email for me."),
]
WORK_TESTS = [
    ("simple question", "Health check from glasses-doctor: reply with just the word OK."),
    (
        "email check",
        "Health check from glasses-doctor: count my unread emails from the last day, in every account I have, "
        "and reply with just the count per account. Don't open any of them.",
    ),
    (
        "web search",
        "Health check from glasses-doctor: search the web for one top business headline from today, then "
        "reply in one sentence and name the source.",
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


async def run(config, name: str, tests: list[tuple[str, str]], extra: dict) -> None:
    lane = Lane(config, name)
    await lane.start()
    try:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(lane.ready.wait(), 60)
        print(json.dumps({"test": f"{name} startup", "ok": lane.alive, "total_s": round(lane.ready_after or 0, 1),
                          "model": config.model, "effort": config.effort, "cwd": config.cwd, **extra}), flush=True)
        for i, (test, text) in enumerate(tests):
            if not lane.alive:
                break
            result = await timed(lane, text, 120)
            result["test"] = test
            if test == "hand-off":
                result["handed_off"] = result["reply"].lstrip().lower().startswith("[work]")
            if i == 0:
                result["credentials"] = lane.api_key_source
                if not config.talk_only:
                    result["connectors"] = lane.mcp_status
            print(json.dumps(result), flush=True)
    finally:
        await lane.close()


async def main() -> None:
    config = brain_config(read_persona())
    if config.talk is not None:
        await run(config.talk, "quick voice", TALK_TESTS, {})
    await run(config.lane, "work model", WORK_TESTS, {})


if __name__ == "__main__":
    asyncio.run(main())
