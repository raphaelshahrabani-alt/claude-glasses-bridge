"""The glasses' voice worker, with Claude Code as the brain.

The VisionClaw app on the phone joins a LiveKit room with the glasses' mic and
(when asked) camera. This worker joins the same room: speech-to-text turns what
the user says into words, Claude Code answers (brain.py), and text-to-speech
speaks the answer as it is written. No second AI model sits in between.

Run it with bin/glasses-voice, which sets up its Python environment and loads
the LiveKit keys from VisionClaw's gateway/.env.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from livekit import rtc  # noqa: E402
from livekit.agents import (  # noqa: E402
    DEFAULT_API_CONNECT_OPTIONS,
    Agent,
    AgentServer,
    AgentSession,
    APIConnectOptions,
    JobContext,
    cli,
    llm,
)

from brain import Brain, BrainConfig  # noqa: E402
from claude_lane import LaneConfig  # noqa: E402

log = logging.getLogger("glasses.voice")

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GLASSES_DATA_DIR", Path.home() / ".claude-glasses-bridge")).expanduser()
FINISH_AFTER_HANGUP_S = float(os.environ.get("GLASSES_FINISH_AFTER_HANGUP_S", "600"))

# Camera on demand: the phone only streams the glasses camera while asked to
# (VisionClaw's "vc.camera" text-stream topic), so the capture light is on for
# a few seconds, not the whole call.
CAMERA_WAKE_S = 12
CAMERA_SETTLE_S = 0.8


def setup_logging() -> None:
    root = logging.getLogger("glasses")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(DATA_DIR / "voice.log", maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(process)d] %(message)s"))
    root.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root.addHandler(console)


def env_dirs(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [str(Path(p).expanduser()) for p in raw.split(os.pathsep) if p.strip()]


def persona_path() -> Path:
    return Path(os.environ.get("GLASSES_PERSONA", DATA_DIR / "persona.md")).expanduser()


def read_persona() -> str:
    try:
        return persona_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def first_name(persona: str) -> str:
    """What to call the user: a nickname in brackets, else the first name."""
    match = re.search(r"^\s*-\s*Name:\s*(.+)$", persona, re.M)
    if not match or "<" in match.group(1):
        return ""
    name = match.group(1)
    nickname = re.search(r"\(([^)\s]+)\)", name)
    return nickname.group(1) if nickname else name.split()[0].strip(",")


def keyterms(persona: str) -> list[str]:
    """Names speech-to-text should expect: Claude, the user's projects, and
    anything listed in ~/.claude-glasses-bridge/keyterms.txt."""
    terms = ["Claude", "Claude Code"]
    section = re.search(r"^## My projects\s*$(.*?)(?=^## |\Z)", persona, re.M | re.S)
    if section:
        for name in re.findall(r"^\s*-\s*\**([A-Z][\w .'-]{1,30}?)\**\s*:", section.group(1), re.M):
            terms.append(name.strip())
    try:
        extra = (DATA_DIR / "keyterms.txt").read_text(encoding="utf-8")
        terms += [line.strip() for line in extra.splitlines() if line.strip() and not line.startswith("#")]
    except OSError:
        pass
    seen: set[str] = set()
    return [t for t in terms if not (t.lower() in seen or seen.add(t.lower()))][:50]


def system_prompt_file(persona: str, rules: str = "prompt.md", name: str = "voice-prompt.md") -> str:
    """Voice rules plus the user's profile, in one file Claude Code reads at start."""
    text = (HERE / rules).read_text(encoding="utf-8")
    if persona.strip():
        text += "\n\n" + persona
    path = DATA_DIR / name
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def mcp_config() -> str:
    """MCP servers the work model gets on top of the user's own: every email account (glasses-email)."""
    path = DATA_DIR / "voice-mcp.json"
    config = {"mcpServers": {"email": {
        "command": sys.executable,
        "args": [str(HERE / "email_mcp.py")],
        "env": {"GLASSES_DATA_DIR": str(DATA_DIR)},
    }}}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return str(path)


def setting(*names: str, default: str) -> str | None:
    """The first of these environment variables that is set; empty means Claude Code's default."""
    for name in names:
        if name in os.environ:
            return os.environ[name] or None
    return default


def brain_config(persona: str) -> BrainConfig:
    home = Path.home()
    default_project = home / "Desktop" if (home / "Desktop").is_dir() else home
    project = str(Path(os.environ.get("GLASSES_PROJECT", default_project)).expanduser())
    claude = os.environ.get("CLAUDE_BIN", "claude")
    use_api_key = os.environ.get("GLASSES_USE_API_KEY") == "1"
    # Real work (code, email, calendar, business) runs on the best model.
    work = LaneConfig(
        cwd=project,
        claude=claude,
        model=setting("GLASSES_WORK_MODEL", "GLASSES_MODEL", default="opus"),
        effort=setting("GLASSES_WORK_EFFORT", "GLASSES_EFFORT", default="high"),
        permission_mode=setting("GLASSES_PERMISSION_MODE", default="auto"),
        add_dirs=env_dirs("GLASSES_ADD_DIRS"),
        system_prompt_file=system_prompt_file(persona),
        use_api_key=use_api_key,
        chrome=os.environ.get("GLASSES_CHROME", "1") != "0",
        mcp_config=mcp_config(),
        settings_path=str(DATA_DIR / "voice-settings.json"),
    )
    # A fast model with no tools answers first: small talk and quick facts
    # itself, and "on it" for everything else while the work model starts.
    talk = None
    if os.environ.get("GLASSES_TALK", "1") != "0":
        talk = LaneConfig(
            cwd=project,
            claude=claude,
            model=setting("GLASSES_TALK_MODEL", default="haiku"),
            effort=setting("GLASSES_TALK_EFFORT", default="low"),
            talk_only=True,
            system_prompt_file=system_prompt_file(persona, "talk.md", "talk-prompt.md"),
            use_api_key=use_api_key,
        )
    return BrainConfig(
        lane=work,
        talk=talk,
        data_dir=DATA_DIR,
        max_lanes=int(os.environ.get("GLASSES_LINES", "3")),
        finish_after_hangup_s=FINISH_AFTER_HANGUP_S,
        resume_minutes=float(os.environ.get("GLASSES_RESUME_MINUTES", "30")),
        warm_up=os.environ.get("GLASSES_WARM_UP", "1") != "0",
    )


def make_stt(terms: list[str]):
    language = os.environ.get("GLASSES_LANGUAGE", "en")
    if os.environ.get("DEEPGRAM_API_KEY"):
        from livekit.plugins import deepgram

        return deepgram.STT(model="nova-3", language=language, keyterm=terms or None)
    return os.environ.get("GLASSES_STT", f"deepgram/nova-3:{language}")


# "Eric - smooth, trustworthy", an ElevenLabs voice made for assistants.
ELEVENLABS_VOICE = "cjVigY5qzO86Huf0OWal"
DEFAULT_TTS = "cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"


def backup_tts():
    if os.environ.get("DEEPGRAM_API_KEY"):
        from livekit.plugins import deepgram

        return deepgram.TTS(model=os.environ.get("GLASSES_DEEPGRAM_VOICE", "aura-2-thalia-en"))
    from livekit.agents import inference

    return inference.TTS.from_model_string(os.environ.get("GLASSES_TTS", DEFAULT_TTS))


def make_tts():
    key = os.environ.get("ELEVEN_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return backup_tts()
    from livekit.agents import tts
    from livekit.plugins import elevenlabs

    options = {"voice_id": os.environ.get("GLASSES_ELEVENLABS_VOICE", ELEVENLABS_VOICE), "api_key": key}
    if os.environ.get("GLASSES_ELEVENLABS_MODEL"):
        options["model"] = os.environ["GLASSES_ELEVENLABS_MODEL"]
    primary = elevenlabs.TTS(**options)
    try:
        backup = backup_tts()
    except Exception:
        log.exception("no backup voice")
        return primary
    # When ElevenLabs refuses (a bad key, no credits left), the backup voice
    # speaks instead of silence; glasses-doctor says why ElevenLabs refused.
    return tts.FallbackAdapter([primary, backup], max_retry_per_tts=0)


class Camera:
    """Keeps the latest frame of the glasses camera, waking it when asked to look."""

    def __init__(self, room: rtc.Room) -> None:
        self.room = room
        self.frame: rtc.VideoFrame | None = None
        self.rotation = 0
        self.arrived_at = 0.0
        self._readers: dict[str, asyncio.Task[None]] = {}

        @room.on("track_subscribed")
        def _on_track(track: rtc.Track, *_: object) -> None:
            self._watch(track)

        for participant in room.remote_participants.values():
            for publication in participant.track_publications.values():
                if publication.track is not None:
                    self._watch(publication.track)

    def _watch(self, track: rtc.Track) -> None:
        if track.kind != rtc.TrackKind.KIND_VIDEO or track.sid in self._readers:
            return

        async def read() -> None:
            stream = rtc.VideoStream(track)
            try:
                async for event in stream:
                    self.frame = event.frame
                    self.rotation = getattr(event, "rotation", 0) or 0
                    self.arrived_at = time.time()
            finally:
                self._readers.pop(track.sid, None)
                await stream.aclose()

        self._readers[track.sid] = asyncio.create_task(read())

    def _encode(self) -> str | None:
        from PIL import Image

        frame = self.frame
        if frame is None:
            return None
        rgba = frame.convert(rtc.VideoBufferType.RGBA)
        img = Image.frombuffer("RGBA", (rgba.width, rgba.height), bytes(rgba.data)).convert("RGB")
        rotation = int(self.rotation) % 360 if isinstance(self.rotation, int) else 0
        if rotation:
            img = img.rotate(-rotation, expand=True)
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()

    async def _command(self, on: bool) -> None:
        try:
            await self.room.local_participant.send_text("on" if on else "off", topic="vc.camera")
        except Exception:
            log.exception("camera %s request failed", "on" if on else "off")

    async def look(self) -> str | None:
        if self.frame is not None and time.time() - self.arrived_at < 2:
            return self._encode()
        asked_at = time.time()
        await self._command(True)
        try:
            deadline = time.monotonic() + CAMERA_WAKE_S
            while time.monotonic() < deadline:
                if self.frame is not None and self.arrived_at > asked_at:
                    # The first frames after waking are often dark while exposure settles.
                    await asyncio.sleep(CAMERA_SETTLE_S)
                    return self._encode()
                await asyncio.sleep(0.2)
            log.warning("the camera did not wake within %ss", CAMERA_WAKE_S)
            return None
        finally:
            await self._command(False)


def hearing_notice(event) -> str | None:
    """What to say out loud when the speech-to-text can't hear the user (otherwise they hear silence)."""
    if getattr(event, "type", "") != "stt_error":
        return None
    error = str(getattr(event, "error", event))
    if "429" in error or "quota" in error.lower() or "credit" in error.lower():
        return ("I can't hear you: the speech service is out of free credits. "
                "On your Mac, add a Deepgram key with glasses key, then restart me.")
    return "I'm having trouble hearing you. Check the Mac, in the voice window."


def last_user_text(chat_ctx: llm.ChatContext) -> str:
    for item in reversed(chat_ctx.items):
        if getattr(item, "type", None) == "message" and getattr(item, "role", None) == "user":
            return (item.text_content or "").strip()
    return ""


class ClaudeCodeLLM(llm.LLM):
    """Plugs the brain into LiveKit where a language model would go."""

    def __init__(self, brain: Brain, look=None) -> None:
        super().__init__()
        self.brain = brain
        self.look = look

    @property
    def model(self) -> str:
        return "claude-code"

    @property
    def provider(self) -> str:
        return "claude-code"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        **_: object,
    ) -> "ClaudeCodeStream":
        return ClaudeCodeStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class ClaudeCodeStream(llm.LLMStream):
    async def _run(self) -> None:
        brain_llm: ClaudeCodeLLM = self._llm  # type: ignore[assignment]
        words = last_user_text(self._chat_ctx)
        chunk_id = f"claude-{uuid.uuid4().hex[:12]}"
        try:
            async for piece in brain_llm.brain.handle(words, look=brain_llm.look):
                self._event_ch.send_nowait(
                    llm.ChatChunk(id=chunk_id, delta=llm.ChoiceDelta(role="assistant", content=piece))
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("could not answer %r", words[:120])
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id=chunk_id,
                    delta=llm.ChoiceDelta(role="assistant", content="Sorry, something went wrong on my end."),
                )
            )


server = AgentServer(
    # One call at a time; each idle process waiting for a call costs memory.
    num_idle_processes=int(os.environ.get("GLASSES_IDLE_PROCESSES", "1")),
    # After a hang-up, unfinished work keeps going and is told on the next call.
    shutdown_process_timeout=FINISH_AFTER_HANGUP_S + 60,
)


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    setup_logging()
    await ctx.connect()
    persona = read_persona()
    name = first_name(persona)
    terms = keyterms(persona)
    brain = Brain(brain_config(persona))
    starting = asyncio.create_task(brain.start())
    camera = Camera(ctx.room)

    session = AgentSession(
        stt=make_stt(terms),
        tts=make_tts(),
        llm=ClaudeCodeLLM(brain, look=camera.look),
        # Guessing ahead would send half-finished sentences to Claude Code.
        turn_handling={"preemptive_generation": {"enabled": False}},
        stt_context_options={"keyterms": terms},
        user_away_timeout=None,
    )
    brain.speak = lambda text: session.say(text)

    told_deaf = False

    @session.on("error")
    def _on_error(event) -> None:
        nonlocal told_deaf
        error = getattr(event, "error", event)
        log.error("voice error (%s): %s", getattr(event, "source", "?"), error)
        if "elevenlabs" in str(error).lower():
            log.error("ElevenLabs refused to speak, so the backup voice is speaking. To see why, run: "
                      "~/claude-glasses-bridge/bin/glasses-doctor")
        elif "429" in str(error) or "quota" in str(error).lower() or "credit" in str(error).lower():
            log.error(
                "LiveKit Inference may be out of free credits. Add a Deepgram key: "
                "~/claude-glasses-bridge/bin/glasses-key DEEPGRAM_API_KEY"
            )
        notice = hearing_notice(event)
        if notice and not told_deaf:
            # Without this the user just hears silence and has no idea why.
            told_deaf = True
            try:
                session.say(notice)
            except Exception:
                log.exception("could not say that I can't hear")

    @session.on("close")
    def _on_close(event) -> None:
        log.info("call closed: %s", getattr(event, "reason", ""))

    ctx.add_shutdown_callback(brain.close)
    log.info("call started in room %s", ctx.room.name)
    await session.start(agent=Agent(instructions="Answers come from Claude Code."), room=ctx.room)
    session.say(f"Hey {name}, I'm here." if name else "Hey, I'm here.")

    try:
        await starting
    except Exception:
        # The first request tries again; this call stays up either way.
        log.exception("could not start Claude Code")
    for text in brain.parked():
        await brain._say(f"While you were away: {text}", "")


def keep_awake() -> None:
    """On Windows, stop the PC sleeping while the voice is running (what caffeinate does on a Mac)."""
    if os.name != "nt":
        return
    import ctypes
    import threading

    def hold() -> None:
        # The setting lasts as long as this thread does.
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED
        threading.Event().wait()

    threading.Thread(target=hold, daemon=True, name="keep-awake").start()


if __name__ == "__main__":
    keep_awake()
    cli.run_app(server)
