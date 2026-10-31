"""The worker's LiveKit side, in LiveKit's text-only mode, with fake_claude.py
as Claude Code. Needs livekit-agents (run it with the voice worker's Python)."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    from livekit.agents import Agent, AgentSession
    import worker
except ImportError:  # pragma: no cover
    worker = None

from brain import Brain, BrainConfig  # noqa: E402
from claude_lane import LaneConfig  # noqa: E402


@unittest.skipIf(worker is None, "livekit-agents is not installed")
class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["FAKE_CLAUDE_LOG"] = str(self.dir / "fake.log")
        lane = LaneConfig(cwd=str(self.dir), claude=str(HERE / "fake_claude.py"))
        self.brain = Brain(BrainConfig(lane=lane, data_dir=self.dir / "data"))
        await self.brain.start()
        self.session = AgentSession(
            llm=worker.ClaudeCodeLLM(self.brain),
            turn_handling={"preemptive_generation": {"enabled": False}},
        )
        self.spoken = []
        self.brain.speak = self.spoken.append
        await self.session.start(Agent(instructions="unused"))

    async def asyncTearDown(self):
        await self.session.aclose()
        self.brain.config.finish_after_hangup_s = 0.1
        await self.brain.close()
        self.tmp.cleanup()

    def replies(self, result):
        return [
            e.item.text_content for e in result.events
            if getattr(e, "type", "") == "message" and e.item.role == "assistant"
        ]

    async def test_claude_code_answers_through_livekit(self):
        result = await self.session.run(user_input="hello")
        self.assertEqual([r.strip() for r in self.replies(result)], ["Hi there."])

    async def test_tool_result_follows(self):
        result = await self.session.run(user_input="check my email")
        self.assertEqual([r.strip() for r in self.replies(result)], ["Checking your inbox."])
        end = time.monotonic() + 5
        while not self.spoken and time.monotonic() < end:
            await asyncio.sleep(0.05)
        self.assertEqual(self.spoken, ["Your inbox: two emails need you."])

    async def test_quick_voice_hands_over_through_livekit(self):
        await self.session.aclose()
        self.brain.config.finish_after_hangup_s = 0.1
        await self.brain.close()
        lane = LaneConfig(cwd=str(self.dir), claude=str(HERE / "fake_claude.py"))
        talk = LaneConfig(cwd=str(self.dir), claude=str(HERE / "fake_claude.py"), talk_only=True)
        self.brain = Brain(BrainConfig(lane=lane, talk=talk, data_dir=self.dir / "data2"))
        await self.brain.start()
        self.spoken = []
        self.brain.speak = self.spoken.append
        self.session = AgentSession(
            llm=worker.ClaudeCodeLLM(self.brain),
            turn_handling={"preemptive_generation": {"enabled": False}},
        )
        await self.session.start(Agent(instructions="unused"))
        result = await self.session.run(user_input="hello")
        self.assertEqual([r.strip() for r in self.replies(result)], ["Hi there, quick answer."])
        result = await self.session.run(user_input="check my email")
        replies = [r.strip() for r in self.replies(result)]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0].endswith(" Checking your inbox."), replies)
        end = time.monotonic() + 5
        while not self.spoken and time.monotonic() < end:
            await asyncio.sleep(0.05)
        self.assertEqual(self.spoken, ["Your inbox: two emails need you."])

    def test_models(self):
        keys = ["GLASSES_MODEL", "GLASSES_WORK_MODEL", "GLASSES_TALK", "GLASSES_TALK_MODEL", "GLASSES_WORK_EFFORT"]
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            config = worker.brain_config("")
            self.assertEqual((config.lane.model, config.lane.effort), ("opus", "high"))
            self.assertTrue(config.lane.chrome)
            self.assertIn("email_mcp.py", Path(config.lane.mcp_config).read_text())
            self.assertEqual((config.talk.model, config.talk.effort, config.talk.talk_only), ("haiku", "low", True))
            self.assertTrue(config.warm_up)
            os.environ["GLASSES_MODEL"] = "sonnet"
            self.assertEqual(worker.brain_config("").lane.model, "sonnet")
            os.environ["GLASSES_WORK_MODEL"] = "opus"
            self.assertEqual(worker.brain_config("").lane.model, "opus")
            os.environ["GLASSES_TALK"] = "0"
            self.assertIsNone(worker.brain_config("").talk)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_profile_bits(self):
        persona = (
            "## About me\n\n- Name: Alexandra Martin (Alex)\n\n## My projects\n\n"
            "- Harbor: `~/Desktop/harbor`. Community app.\n- **QuickQuote**: quotes.\n\n## Other\n- Nope: x\n"
        )
        self.assertEqual(worker.first_name(persona), "Alex")
        self.assertEqual(worker.first_name("- Name: Dana Smith\n"), "Dana")
        self.assertEqual(worker.first_name("- Name: <your name>\n"), "")
        terms = worker.keyterms(persona)
        self.assertIn("Harbor", terms)
        self.assertIn("QuickQuote", terms)
        self.assertNotIn("Nope", terms)


if __name__ == "__main__":
    unittest.main()
