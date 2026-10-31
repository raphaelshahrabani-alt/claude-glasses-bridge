"""Decides what the glasses say, and when.

Everything the user says goes straight to Claude Code. The first thing Claude
writes is streamed back and spoken at once; when it then goes off to use tools
(email, files, commands), the call is free again: the user can keep talking,
and the result is spoken when it's ready.

To keep talking while Claude works, the brain keeps more than one Claude Code
process ("lines") per call. A new request goes to a free line, together with
whatever was said on the other lines since that line last heard from the user,
so every line knows the conversation.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_lane import ApiError, BlockEnd, Done, Lane, LaneConfig, Permission, Text, ToolStart, Turn

log = logging.getLogger("glasses.voice")

FILLER = re.compile(r"^\W*(?:uh+|um+|hm+|mm+|mhm|uh[- ]?huh|mm[- ]?hmm|ah+|oh+|er+)\W*$", re.I)
CANCEL = re.compile(
    r"^\W*(?:stop|cancel|never ?mind|forget (?:it|that|about it)|stop (?:that|it|working|working on (?:it|that))"
    r"|cancel (?:that|it))\W*$",
    re.I,
)
YES = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|ya|sure|ok(?:ay)?|go ahead|go for it|do it|approved?|allow(?: it)?"
    r"|sounds good|please do|affirmative|correct)\b",
    re.I,
)
NO = re.compile(r"^\W*(?:no|nope|nah|don'?t|do not|deny|negative|stop|cancel|wait)\b", re.I)
LOOK = re.compile(
    r"\b(?:look(?:ing)? at|take a look|have a look|what am i looking at|what do you see|can you see|do you see"
    r"|what(?:'s| is) (?:this|that)\b|read (?:this|that|it)\b|what does (?:this|that|it) say|in front of me"
    r"|on my screen|take a (?:picture|photo)|check this out|look here|see this|see that)",
    re.I,
)

# Requests that are obviously real work skip the quick voice: no tools, and
# not the model to trust with anything current or important.
WORK_HINTS = re.compile(
    r"\b(?:e-?mails?|inbox|mail|calendar|meetings?|schedule|appointments?|remind(?:er)?s?"
    r"|news|headlines?|latest|today|tonight|tomorrow|yesterday|this (?:week|month|morning|weekend)"
    r"|right now|currently|prices?|stocks?|markets?|weather|forecast|scores?"
    r"|search|look (?:it |that |this )?up|google|find|research|websites?|online|browser|chrome|browse"
    r"|code|repo|github|deploy|build|fix|bugs?|files?|folders?"
    r"|send|draft|reply|respond|forward|kiosks?|business|revenue|sales|profits?|margins?|invest\w*"
    r"|strategy|compare|analy[sz]e|write|create|order|buy|book)\b",
    re.I,
)

ACKS = ["On it.", "Let me check.", "One sec.", "Okay, looking into it."]
HANDOFF = "[work]"
WARM_UP = "(Automatic warm-up at the start of a call, not from the user. Reply with just: ok)"
STILL = ["Still on it.", "Still working on that.", "Still going, almost there I hope."]


# What to say when Claude Code can't reach Claude, in full and when it happens again.
API_ERRORS = {
    "billing_error": (
        "Claude Code can't answer: the Anthropic account it's using is out of credit. "
        "Run glasses doctor on your Mac to see which account that is.",
        "the Anthropic account is out of credit",
    ),
    "authentication_failed": (
        "Claude Code isn't logged in. On your Mac, open Terminal, run claude, and type slash login.",
        "Claude Code isn't logged in",
    ),
    "rate_limit": (
        "You've hit your Claude usage limit for now. It should reset in a few hours.",
        "you're still at your Claude usage limit",
    ),
    "server_error": (
        "Claude's servers are having trouble. Try again in a minute.",
        "Claude's servers are still having trouble",
    ),
}


def api_error_kind(text: str) -> str | None:
    """For a Claude Code too old to label its API errors."""
    lowered = text.lower()
    if "credit balance" in lowered:
        return "billing_error"
    if "invalid api key" in lowered or "/login" in lowered or "not logged in" in lowered:
        return "authentication_failed"
    if "usage limit" in lowered or "rate limit" in lowered:
        return "rate_limit"
    return None


def yes_no(words: str) -> bool | None:
    if YES.match(words):
        return True
    if NO.match(words):
        return False
    return None


def wants_look(words: str) -> bool:
    return bool(LOOK.search(words))


@dataclass
class BrainConfig:
    lane: LaneConfig
    data_dir: Path
    max_lanes: int = 3
    first_words_s: float = 6.0
    progress_s: float = 25.0
    max_progress_notices: int = 4
    permission_timeout_s: float = 120.0
    finish_after_hangup_s: float = 600.0
    resume_minutes: float = 30.0
    max_resume_turns: int = 40
    earlier_chars: int = 3000
    # The quick voice up front (no tools), which hands real work to the lines above.
    talk: LaneConfig | None = None
    talk_decide_s: float = 4.0
    # Send each new line a tiny first message, so the user's first question finds it warm.
    warm_up: bool = False


@dataclass
class Said:
    role: str  # "user" or "assistant"
    text: str
    lane: str
    at: float = field(default_factory=time.time)


class CallLog:
    """Readable record of every call: ~/.claude-glasses-bridge/calls/<date>.md"""

    def __init__(self, directory: Path, now: Callable[[], datetime] = datetime.now) -> None:
        self.directory = directory
        self.now = now

    def path(self, day: datetime | None = None) -> Path:
        return self.directory / f"{(day or self.now()):%Y-%m-%d}.md"

    def _append(self, text: str) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path().open("a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            log.exception("could not write the call log")

    def start(self, note: str = "") -> None:
        self._append(f"\n## Call at {self.now():%H:%M}{' (' + note + ')' if note else ''}\n\n")

    def say(self, who: str, text: str) -> None:
        one_line = " ".join(text.split())
        self._append(f"{self.now():%H:%M:%S}  {who}: {one_line}\n")

    def note(self, text: str) -> None:
        self._append(f"{self.now():%H:%M:%S}  ({text})\n")

    def recent(self, max_chars: int) -> str:
        """The end of today's and yesterday's calls, oldest first."""
        lines: list[str] = []
        today = self.now()
        for day in (today - timedelta(days=1), today):
            try:
                text = self.path(day).read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                if "  You: " in line or "  Claude: " in line:
                    lines.append(f"{day:%a} {line}")
        out: list[str] = []
        size = 0
        for line in reversed(lines):
            size += len(line) + 1
            if size > max_chars:
                break
            out.append(line)
        return "\n".join(reversed(out))


class Runner:
    """Follows one turn: streams its first words live, speaks the rest later."""

    def __init__(self, brain: "Brain", turn: Turn, acked: bool = False) -> None:
        self.brain = brain
        self.turn = turn
        # The quick voice already said "on it": no second acknowledgment.
        self.acked = acked
        self.lane = turn.lane
        self.live_q: asyncio.Queue[str | None] = asyncio.Queue()
        self.attached = True
        self.live_ended = False
        self.live_text: list[str] = []
        self.segment: list[str] = []
        self.drop_until_tool = False
        self.cancelled = False
        self.api_error: ApiError | None = None
        self.tools = 0
        self.first_words_at: float | None = None
        self.last_spoke = time.monotonic()
        self.notices = 0
        self.task = asyncio.create_task(self._run())
        self.watch = asyncio.create_task(self._watch())

    async def live(self) -> AsyncIterator[str]:
        while True:
            piece = await self.live_q.get()
            if piece is None:
                return
            yield piece

    def _live(self, text: str) -> None:
        if self.first_words_at is None:
            self.first_words_at = time.monotonic()
            log.info("%s: first words after %.1fs", self.lane.name, self.first_words_at - self.turn.started)
        self.live_text.append(text)
        self.last_spoke = time.monotonic()
        self.live_q.put_nowait(text)

    def _end_live(self) -> None:
        if self.attached and not self.live_ended:
            self.live_ended = True
            self.attached = False
            self.live_q.put_nowait(None)
            self._record_live()

    def detach(self) -> None:
        """The live stream went away (the user talked over it); speak the rest later."""
        if not self.live_ended:
            self.live_ended = True
            self.attached = False
            if self._record_live():
                # They heard part of this paragraph and cut it off; don't come back to it.
                self.drop_until_tool = True

    def _record_live(self) -> bool:
        said = "".join(self.live_text).strip()
        if said:
            self.brain._remember("assistant", said, self.lane.name)
            self.brain.calls.say("Claude", said)
        return bool(said)

    async def _say(self, text: str) -> None:
        self.last_spoke = time.monotonic()
        await self.brain._say(text, self.lane.name)

    async def _run(self) -> None:
        try:
            async for event in self.turn.events():
                if isinstance(event, Text):
                    if self.attached:
                        self._live(event.text)
                    elif not self.drop_until_tool:
                        self.segment.append(event.text)
                elif isinstance(event, BlockEnd):
                    if self.attached and self.live_text:
                        self._end_live()
                elif isinstance(event, ToolStart):
                    self.tools += 1
                    log.info("%s: using %s", self.lane.name, event.name)
                    if self.attached:
                        if not self.live_text and not self.acked:
                            self._live(self.brain._pick(ACKS))
                        self._end_live()
                    else:
                        narration = "".join(self.segment).strip()
                        self.segment.clear()
                        self.drop_until_tool = False
                        if narration and time.monotonic() - self.last_spoke > self.brain.config.progress_s:
                            await self._say(narration)
                elif isinstance(event, Permission):
                    if self.attached:
                        if not self.live_text and not self.acked:
                            self._live(self.brain._pick(ACKS))
                        self._end_live()
                    await self.brain._ask_permission(event, self.lane)
                elif isinstance(event, ApiError):
                    self.api_error = event
                    explained = self.brain._explain(event.kind, event.text)
                    if self.attached:
                        self.live_text.clear()
                        self._live(explained)
                        self._end_live()
                    else:
                        await self._say(explained)
                elif isinstance(event, Done):
                    await self._finish(event)
        except Exception:
            log.exception("%s: turn failed", self.lane.name)
            if self.attached:
                self._live("Sorry, something went wrong on my end.")
            self._end_live()
        finally:
            self.watch.cancel()
            self._end_live()
            self.brain._turn_finished(self)

    async def _finish(self, done: Done) -> None:
        took = time.monotonic() - self.turn.started
        log.info(
            "%s: done after %.1fs, %d tool call(s)%s",
            self.lane.name, took, self.tools, "" if done.ok else f", failed: {done.error}",
        )
        final = "" if self.drop_until_tool else "".join(self.segment).strip()
        if self.cancelled or self.api_error:
            self._end_live()
            return
        if self.attached:
            if not self.live_text:
                self._live(final or done.text.strip() or self._failure(done))
            self._end_live()
            return
        if not final and not done.ok and not self.drop_until_tool:
            final = self._failure(done)
        if final:
            await self._say(final)

    def _failure(self, done: Done) -> str:
        return Runner._failure_text(self.brain, done)

    @staticmethod
    def _failure_text(brain: "Brain", done: Done) -> str:
        if done.error and "stopped" in done.error:
            return "Sorry, I lost my connection to Claude Code. Ask me again and I'll start fresh."
        kind = api_error_kind(done.text)
        if kind:
            return brain._explain(kind, done.text)
        return "Sorry, that didn't work."

    async def _watch(self) -> None:
        """Say something when the line has been quiet for too long."""
        try:
            gap = self.brain.config.progress_s
            while True:
                await asyncio.sleep(0.5)
                now = time.monotonic()
                if self.attached and not self.live_text:
                    if now - self.turn.started >= self.brain.config.first_words_s:
                        if not self.acked:
                            self._live(self.brain._pick(ACKS))
                        self._end_live()
                    continue
                if self.attached or self.notices >= self.brain.config.max_progress_notices:
                    continue
                if now - self.last_spoke >= gap and not self.brain._permissions:
                    self.notices += 1
                    gap *= 2
                    await self._say(self.brain._pick(STILL))
        except asyncio.CancelledError:
            pass


class Brain:
    def __init__(
        self,
        config: BrainConfig,
        speak: Callable[[str], Awaitable[None] | None] | None = None,
        lane_factory: Callable[[LaneConfig, str], Lane] = Lane,
    ) -> None:
        self.config = config
        self.speak = speak
        self.lane_factory = lane_factory
        self.lanes: list[Lane] = []
        self.runners: set[Runner] = set()
        self.history: list[Said] = []
        self.seen: dict[str, int] = {}
        self.closing = False
        self.calls = CallLog(config.data_dir / "calls")
        self.earlier = ""
        self.resumed = False
        self._permissions: deque[tuple[Permission, Lane, asyncio.Task[None]]] = deque()
        self._lane_counter = 0
        self._spawning: asyncio.Task[Lane | None] | None = None
        self._pick_counter = 0
        self._turns_in_primary = 0
        self._last_api_error: tuple[str, float] | None = None
        self.talk_lane: Lane | None = None
        self._talk_failures = 0
        self._warming: set[Lane] = set()
        self._background: set[asyncio.Task[Any]] = set()

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        state = self._load_state()
        primary = self.config.lane
        recent = state.get("updated", 0) > time.time() - self.config.resume_minutes * 60
        if (
            recent
            and state.get("session")
            and state.get("cwd") == primary.cwd
            and state.get("turns", 0) < self.config.max_resume_turns
        ):
            primary = replace(primary, resume=state["session"])
            self.resumed = True
            self._turns_in_primary = state.get("turns", 0)
        self.earlier = self.calls.recent(self.config.earlier_chars)
        talk = asyncio.create_task(self._start_talk())
        lane = await self._spawn(primary)
        if lane is not None and self.resumed:
            # A session that can't be resumed makes Claude Code exit right away.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(lane.ready.wait(), 20)
            if not lane.alive:
                log.warning("could not pick up session %s; starting a new one", primary.resume)
                self.lanes.remove(lane)
                await lane.close()
                lane = None
        if lane is None and self.resumed:
            self.resumed = False
            self._turns_in_primary = 0
            lane = await self._spawn(self.config.lane)
        await talk
        if lane is not None:
            self._warm(lane)
        self.calls.start("picking up the last conversation" if self.resumed else "")

    def parked(self) -> list[str]:
        """Results that finished after an earlier call ended; reading them clears them."""
        path = self.config.data_dir / "parked.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            path.unlink()
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                out.append(json.loads(line)["text"])
        return [t for t in out if t]

    async def close(self) -> None:
        """The call ended. Unfinished work keeps going for a while; what it says is kept for next time."""
        self.closing = True
        while self._permissions:
            permission, lane, timer = self._permissions.popleft()
            timer.cancel()
            with contextlib.suppress(Exception):
                await lane.answer(permission, False, "The user hung up, so nobody can approve this. Skip it.")
        busy = [r.task for r in self.runners if not r.task.done()]
        if busy:
            log.info("call ended with %d request(s) still running; letting them finish", len(busy))
            self.calls.note(f"call ended; finishing {len(busy)} request(s) in the background")
            _, pending = await asyncio.wait(busy, timeout=self.config.finish_after_hangup_s)
            for task in pending:
                task.cancel()
        if self._spawning and not self._spawning.done():
            with contextlib.suppress(Exception):
                await self._spawning
        for task in self._background:
            task.cancel()
        lanes = self.lanes + ([self.talk_lane] if self.talk_lane else [])
        await asyncio.gather(*(lane.close() for lane in lanes), return_exceptions=True)
        self._save_state()

    # ---- the conversation ------------------------------------------------

    async def handle(
        self, words: str, look: Callable[[], Awaitable[str | None]] | None = None
    ) -> AsyncIterator[str]:
        """Called with each thing the user says. Yields what to say right now;
        anything Claude says after that is spoken through speak()."""
        words = " ".join(words.split())
        if not words or FILLER.match(words):
            return
        self.calls.say("You", words)

        if self._permissions:
            verdict = yes_no(words)
            if verdict is not None:
                reply = await self._answer_permission(verdict)
                self._remember("user", words, "")
                self._remember("assistant", reply, "")
                yield reply
                return

        busy = [r for r in self.runners if not r.task.done() and not r.cancelled]
        if busy and CANCEL.match(words):
            newest = max(busy, key=lambda r: r.turn.started)
            newest.cancelled = True
            with contextlib.suppress(Exception):
                await newest.lane.interrupt()
            self._remember("user", words, newest.lane.name)
            self._remember("assistant", "Okay, I stopped that.", newest.lane.name)
            self.calls.say("Claude", "Okay, I stopped that.")
            yield "Okay, I stopped that."
            return

        image = None
        looked = False
        if look and wants_look(words):
            looked = True
            yield "Let me look. "
            try:
                image = await look()
            except Exception:
                log.exception("could not get a picture from the camera")

        if not looked and self.config.talk is not None and WORK_HINTS.search(words):
            # Straight to the work model; "on it" doesn't need a model to say it.
            ack = self._pick(ACKS)
            entry = len(self.history)
            self._remember("user", words, "")
            self._remember("assistant", ack, "")
            self.calls.say("Claude", ack)
            log.info("straight to the work model")
            yield ack + " "
            begun = await self._begin_work(words, None, False, user_entry=entry)
            if isinstance(begun, str):
                yield begun
                return
            async for piece in self._follow(begun):
                yield piece
            return

        if not looked:
            talk = await self._free_talk()
            if talk is not None:
                async for piece in self._through_talk(talk, words):
                    yield piece
                return

        begun = await self._begin_work(words, image, looked)
        if isinstance(begun, str):
            yield begun
            return
        async for piece in self._follow(begun):
            yield piece

    async def _follow(self, runner: Runner) -> AsyncIterator[str]:
        try:
            async for piece in runner.live():
                yield piece
        finally:
            runner.detach()

    async def _begin_work(
        self, words: str, image: str | None, looked: bool, user_entry: int | None = None
    ) -> "Runner | str":
        """Send a request to a work line. Returns its Runner, or what to say if that failed.
        user_entry is where the user's words already are in the history (handed over by
        the quick voice), so they aren't repeated as background."""
        lane = await self._free_lane()
        if lane is None:
            reply = "I'm still busy with what you asked before. Give me a moment, then ask me again."
            if user_entry is None:
                self._remember("user", words, "")
            self.calls.say("Claude", reply)
            return reply
        turn = None
        for attempt in range(2):
            content = self._compose(lane, words, image, looked, until=user_entry, acked=user_entry is not None)
            try:
                turn = await lane.ask(content, words)
                break
            except Exception:
                log.exception("%s: could not send the request", lane.name)
                if lane in self.lanes:
                    self.lanes.remove(lane)
                await lane.close()
                lane = await self._free_lane() if attempt == 0 else None
                if lane is None:
                    break
        if turn is None or lane is None:
            if user_entry is None:
                self._remember("user", words, "")
            return "Sorry, I couldn't reach Claude Code. Try again in a second."
        if user_entry is None:
            self._remember("user", words, lane.name)
        self.seen[lane.name] = len(self.history)
        if lane is self._primary():
            self._turns_in_primary += 1
        runner = Runner(self, turn, acked=user_entry is not None)
        self.runners.add(runner)
        self._keep_a_spare()
        return runner

    async def _through_talk(self, talk: Lane, words: str) -> AsyncIterator[str]:
        """The quick voice answers, or says "on it" and hands the request to a work line."""
        entry = len(self.history)
        content = self._compose(talk, words, None, False)
        self._remember("user", words, talk.name)
        self.seen[talk.name] = len(self.history)
        try:
            turn = await talk.ask(content, words)
        except Exception:
            log.exception("%s: could not send the request", talk.name)
            begun = await self._begin_work(words, None, False, user_entry=entry)
            if isinstance(begun, str):
                yield begun
                return
            async for piece in self._follow(begun):
                yield piece
            return

        said: list[str] = []
        buffer = ""
        decided: str | None = None
        work: asyncio.Task[Runner | str] | None = None
        following = False
        events = turn.events().__aiter__()
        deadline = time.monotonic() + self.config.talk_decide_s

        def hand_over() -> None:
            nonlocal work
            log.info("%s: handing over to the work model", talk.name)
            work = asyncio.create_task(self._begin_work(words, None, False, user_entry=entry))

        def first(text: str) -> str:
            if not said:
                log.info("%s: first words after %.1fs", talk.name, time.monotonic() - turn.started)
            said.append(text)
            return text

        try:
            while True:
                try:
                    if decided is None:
                        event = await asyncio.wait_for(events.__anext__(), max(0.05, deadline - time.monotonic()))
                    else:
                        event = await events.__anext__()
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    log.warning("%s: no answer within %.0fs; the work model takes it", talk.name, self.config.talk_decide_s)
                    with contextlib.suppress(Exception):
                        await talk.interrupt()
                    decided = "work"
                    hand_over()
                    yield first(self._pick(ACKS) + " ")
                    break
                if isinstance(event, Text):
                    if decided is None:
                        buffer += event.text
                        head = buffer.lstrip()
                        if head.lower().startswith(HANDOFF):
                            decided = "work"
                            hand_over()
                            rest = head[len(HANDOFF):].lstrip()
                            if rest:
                                yield first(rest)
                        elif len(head) >= len(HANDOFF) or not HANDOFF.startswith(head.lower()):
                            decided = "talk"
                            yield first(buffer)
                    else:
                        text = event.text.replace(HANDOFF, "")
                        if text:
                            yield first(text)
                elif isinstance(event, ApiError):
                    decided = decided or "talk"
                    yield first(self._explain(event.kind, event.text))
                elif isinstance(event, Done):
                    if decided is None:
                        if buffer.strip() and event.ok:
                            decided = "talk"
                            yield first(buffer)
                        else:
                            decided = "work"
                            hand_over()
                    if decided == "work" and not said:
                        yield first(self._pick(ACKS) + " ")
                    if decided == "talk" and not said and not event.ok:
                        yield first(Runner._failure_text(self, event))
                    break
            if work is not None:
                begun = await asyncio.shield(work)
                if isinstance(begun, str):
                    yield begun
                    return
                following = True
                gap = bool(said) and not said[-1][-1:].isspace()
                async for piece in self._follow(begun):
                    if gap and piece and not piece[:1].isspace():
                        piece = " " + piece
                    gap = False
                    yield piece
        finally:
            text = "".join(said).strip()
            if text:
                self._remember("assistant", text, talk.name)
                self.calls.say("Claude", text)
            if work is not None and not following:
                def detach(task: asyncio.Task[Runner | str]) -> None:
                    with contextlib.suppress(Exception):
                        begun = task.result()
                        if isinstance(begun, Runner):
                            begun.detach()

                work.add_done_callback(detach)

    # ---- lines -----------------------------------------------------------

    def _primary(self) -> Lane | None:
        return self.lanes[0] if self.lanes else None

    async def _start_talk(self) -> None:
        if self.config.talk is None or self.closing:
            return
        lane = self.lane_factory(self.config.talk, "quick voice")
        try:
            await lane.start()
        except Exception:
            log.exception("could not start the quick voice")
            await lane.close()
            return
        self.talk_lane = lane
        self._warm(lane)

    async def _free_talk(self) -> Lane | None:
        if self.config.talk is None:
            return None
        lane = self.talk_lane
        if lane is None or not lane.alive:
            if self._talk_failures >= 2:
                return None
            self._talk_failures += 1
            if lane is not None:
                await lane.close()
                self.talk_lane = None
            if self._talk_failures >= 2:
                log.warning("the quick voice keeps stopping; the work model answers everything")
                return None
            await self._start_talk()
            lane = self.talk_lane
            if lane is None:
                return None
        if lane.busy:
            # Still warming up, or finishing its last answer.
            await self._idle(lane, 3.0)
            if lane.busy:
                return None
        return lane

    async def _idle(self, lane: Lane, timeout: float) -> None:
        end = time.monotonic() + timeout
        while lane.busy and time.monotonic() < end:
            await asyncio.sleep(0.05)

    def _warm(self, lane: Lane) -> None:
        if not self.config.warm_up:
            return

        async def warm() -> None:
            self._warming.add(lane)
            try:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(lane.ready.wait(), 30)
                if not lane.alive or lane.busy:
                    return
                started = time.monotonic()
                turn = await lane.ask(WARM_UP, "(warm-up)")
                async for event in turn.events():
                    if isinstance(event, Permission):
                        await lane.answer(event, False, "This is only a warm-up.")
                log.info("%s: warmed up in %.1fs", lane.name, time.monotonic() - started)
            except Exception:
                log.exception("%s: warm-up failed", lane.name)
            finally:
                self._warming.discard(lane)

        task = asyncio.create_task(warm())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _spawn(self, config: LaneConfig | None = None) -> "asyncio.Future[Lane | None]":
        """Start a line. One at a time; shielded, so a request that gets
        interrupted while waiting doesn't leave a half-started process."""
        if self._spawning is None or self._spawning.done():
            self._spawning = asyncio.create_task(self._start_lane(config or self.config.lane))
        return asyncio.shield(self._spawning)

    async def _start_lane(self, config: LaneConfig) -> Lane | None:
        self._lane_counter += 1
        lane = self.lane_factory(config, f"line {self._lane_counter}")
        try:
            await lane.start()
        except Exception:
            log.exception("could not start Claude Code")
            await lane.close()
            return None
        if self.closing:
            await lane.close()
            return None
        self.lanes.append(lane)
        return lane

    async def _free_lane(self) -> Lane | None:
        for lane in list(self.lanes):
            if not lane.alive and not lane.busy:
                self.lanes.remove(lane)
                await lane.close()
        for lane in self.lanes:
            if not lane.busy:
                return lane
        for lane in list(self._warming):
            if lane in self.lanes:
                await self._idle(lane, 10.0)
                if not lane.busy and lane.alive:
                    return lane
        # A line that has said its answer is usually a moment from done; keep
        # the conversation on it rather than starting over on another.
        for runner in sorted(self.runners, key=lambda r: self.lanes.index(r.lane) if r.lane in self.lanes else 99):
            if runner.live_ended and not runner.tools and not runner.task.done():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(runner.task), 1.5)
                if not runner.lane.busy and runner.lane.alive:
                    return runner.lane
        if self._spawning is not None and not self._spawning.done():
            lane = await asyncio.shield(self._spawning)
            if lane is not None and not lane.busy:
                return lane
        if len(self.lanes) < self.config.max_lanes:
            lane = await self._spawn()
            if lane is not None and not lane.busy:
                return lane
        return None

    def _keep_a_spare(self) -> None:
        """Start the next line ahead of time, so asking something else while
        Claude works doesn't wait for Claude Code to start."""
        if self.closing or any(not lane.busy for lane in self.lanes):
            return
        if len(self.lanes) >= self.config.max_lanes:
            return
        self._spawn()

    # ---- context ---------------------------------------------------------

    def _remember(self, role: str, text: str, lane: str) -> None:
        self.history.append(Said(role, text, lane))

    def _compose(
        self, lane: Lane, words: str, image: str | None, looked: bool,
        until: int | None = None, acked: bool = False,
    ) -> str | list[dict[str, Any]]:
        parts = []
        first_message = lane.name not in self.seen
        if first_message and self.earlier and not (self.resumed and lane is self._primary()):
            parts.append("Earlier calls on the glasses (most recent last):\n" + self.earlier)
        start = self.seen.get(lane.name, 0)
        missed = [s for s in self.history[start:until] if s.lane != lane.name]
        if missed:
            lines = "\n".join(f"{'User' if s.role == 'user' else 'You'}: {s.text}" for s in missed[-20:])
            label = "Earlier on this call" if first_message else "Meanwhile on this call, handled separately"
            parts.append(f"{label}:\n{lines}")
        text = words
        if parts:
            text = "[Context, not said just now]\n" + "\n\n".join(parts) + "\n\n[The user just said]\n" + words
        if acked:
            text += "\n\n(The user already heard a quick \"on it\", so don't acknowledge again: start with what you're doing or the answer.)"
        if looked and image is None:
            text += "\n\n(The user asked you to look, but the glasses camera didn't send a picture. Say so.)"
        if image is None:
            return text
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
            {"type": "text", "text": text + "\n\n(The picture is what the user's glasses camera sees right now.)"},
        ]

    # ---- speaking --------------------------------------------------------

    def _explain(self, kind: str, text: str) -> str:
        """What to say about an API error; shorter when it's the same one again."""
        full, short = API_ERRORS.get(kind, (f"Sorry, Claude Code ran into an error: {text[:150]}", "the same error"))
        last = self._last_api_error
        self._last_api_error = (kind, time.monotonic())
        if last and last[0] == kind and time.monotonic() - last[1] < 300:
            return f"Same problem as before: {short}."
        return full

    def _pick(self, options: list[str]) -> str:
        self._pick_counter += 1
        return options[self._pick_counter % len(options)]

    async def _say(self, text: str, lane: str) -> None:
        self._remember("assistant", text, lane)
        if self.closing or self.speak is None:
            self._park(text)
            return
        self.calls.say("Claude", text)
        try:
            result = self.speak(text)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("could not speak; keeping it for the next call")
            self._park(text)

    def _park(self, text: str) -> None:
        if not text or text in STILL:
            return
        self.calls.say("Claude (after the call)", text)
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.config.data_dir / "parked.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"text": text, "at": time.time()}) + "\n")
        except OSError:
            log.exception("could not park a result")

    # ---- approvals -------------------------------------------------------

    async def _ask_permission(self, permission: Permission, lane: Lane) -> None:
        if self.closing:
            await lane.answer(permission, False, "The user hung up, so nobody can approve this. Skip it.")
            return
        timer = asyncio.create_task(self._permission_timeout(permission, lane))
        self._permissions.append((permission, lane, timer))
        log.info("%s: asking to %s", lane.name, permission.spoken())
        if len(self._permissions) == 1:
            await self._say(f"Quick check before I go on: {permission.spoken()}. Is that okay?", lane.name)

    async def _answer_permission(self, allow: bool) -> str:
        permission, lane, timer = self._permissions.popleft()
        timer.cancel()
        log.info("%s: %s %s", lane.name, "allowed" if allow else "denied", permission.tool_name)
        await lane.answer(permission, allow)
        reply = "Okay, going ahead." if allow else "Okay, I won't do that."
        self.calls.say("Claude", reply)
        if self._permissions:
            nxt, nxt_lane, _ = self._permissions[0]
            reply += f" One more: {nxt.spoken()}. Okay?"
        return reply

    async def _permission_timeout(self, permission: Permission, lane: Lane) -> None:
        await asyncio.sleep(self.config.permission_timeout_s)
        for i, (p, _, _) in enumerate(self._permissions):
            if p is permission:
                del self._permissions[i]
                break
        await lane.answer(permission, False, "The user didn't answer, so this was not approved. Skip it.")

    # ---- bookkeeping -----------------------------------------------------

    def _turn_finished(self, runner: Runner) -> None:
        self.runners.discard(runner)
        if runner.lane is self._primary():
            self._save_state()

    def _state_path(self) -> Path:
        return self.config.data_dir / "voice-state.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        primary = self._primary()
        if primary is None or not primary.session_id:
            return
        state = {
            "session": primary.session_id,
            "cwd": self.config.lane.cwd,
            "updated": time.time(),
            "turns": self._turns_in_primary,
        }
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            self._state_path().write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            log.exception("could not save voice state")
