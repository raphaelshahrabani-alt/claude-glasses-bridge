"""The brain against a stand-in for Claude Code (fake_claude.py).

Run: python -m unittest discover -s voice/tests
"""
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

from brain import ACKS, API_ERRORS, WORK_HINTS, Brain, BrainConfig, CallLog, wants_look, yes_no  # noqa: E402
from claude_lane import LaneConfig, Permission  # noqa: E402

FAKE = str(HERE / "fake_claude.py")


async def until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


class BrainBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "fake.log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        os.environ.pop("FAKE_SESSION", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.spoken: list[str] = []
        self.brains: list[Brain] = []

    async def asyncTearDown(self):
        for brain in self.brains:
            brain.config.finish_after_hangup_s = 0.1
            await brain.close()
        self.tmp.cleanup()

    def config(self, **overrides):
        lane = LaneConfig(cwd=str(self.dir), claude=FAKE, model="sonnet", effort="low")
        values = dict(lane=lane, data_dir=self.dir / "data", progress_s=60.0, first_words_s=5.0)
        values.update(overrides)
        return BrainConfig(**values)

    async def brain(self, **overrides):
        brain = Brain(self.config(**overrides), speak=self.spoken.append)
        self.brains.append(brain)
        await brain.start()
        return brain

    async def ask(self, brain, words, look=None):
        return "".join([piece async for piece in brain.handle(words, look)]).strip()

    def fake_log(self, kind=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r for r in rows if kind is None or r["kind"] == kind]



class BrainTest(BrainBase):
    async def test_answer_is_spoken_as_it_streams(self):
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "hello"), "Hi there.")
        self.assertEqual(self.spoken, [])

    async def test_quick_follow_up_stays_on_the_same_line(self):
        brain = await self.brain()
        await self.ask(brain, "hello")
        await self.ask(brain, "hello again")
        self.assertEqual(len({u["pid"] for u in self.fake_log("user")}), 1)

    async def test_tool_result_is_spoken_when_ready(self):
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "check my email"), "Checking your inbox.")
        self.assertTrue(await until(lambda: self.spoken))
        self.assertEqual(self.spoken, ["Your inbox: two emails need you."])

    async def test_talk_while_it_works(self):
        os.environ["FAKE_LONG_S"] = "1.5"
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "do the long thing"), "Starting the long task.")
        started = time.monotonic()
        self.assertEqual(await self.ask(brain, "hello"), "Hi there.")
        self.assertLess(time.monotonic() - started, 1.4, "the second question waited for the first")
        self.assertTrue(await until(lambda: "Finished the long task." in self.spoken))
        users = self.fake_log("user")
        self.assertEqual(len({u["pid"] for u in users}), 2, "both requests went to the same line")
        # the second line was told what the first one is doing
        self.assertIn("Starting the long task.", users[1]["value"])
        self.assertIn("[The user just said]\nhello", users[1]["value"])

    async def test_approve_by_voice(self):
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "make a file"), "One moment.")
        self.assertTrue(await until(lambda: self.spoken))
        self.assertEqual(self.spoken[0], "Quick check before I go on: Create the file notes.txt. Is that okay?")
        self.assertEqual(await self.ask(brain, "yes go ahead"), "Okay, going ahead.")
        self.assertTrue(await until(lambda: "Done, I made the file." in self.spoken))
        self.assertEqual([r["value"] for r in self.fake_log("permission")], ["allow"])

    async def test_deny_by_voice(self):
        brain = await self.brain()
        await self.ask(brain, "make a file")
        self.assertTrue(await until(lambda: self.spoken))
        self.assertEqual(await self.ask(brain, "no don't"), "Okay, I won't do that.")
        self.assertTrue(await until(lambda: "Okay, I skipped it." in self.spoken))
        self.assertEqual([r["value"] for r in self.fake_log("permission")], ["deny"])

    async def test_stop_cancels_the_work(self):
        os.environ["FAKE_LONG_S"] = "3"
        brain = await self.brain()
        await self.ask(brain, "do the long thing")
        self.assertEqual(await self.ask(brain, "stop"), "Okay, I stopped that.")
        self.assertTrue(await until(lambda: "interrupt" in [r["value"] for r in self.fake_log("control")]))
        await asyncio.sleep(0.5)
        self.assertNotIn("Finished the long task.", self.spoken)
        self.assertEqual(await self.ask(brain, "hello"), "Hi there.")

    async def test_says_something_when_claude_is_slow_to_start(self):
        os.environ["FAKE_SLOW_S"] = "1.0"
        brain = await self.brain(first_words_s=0.3)
        reply = await self.ask(brain, "a slow one")
        self.assertIn(reply, ["On it.", "Let me check.", "One sec.", "Okay, looking into it."])
        self.assertTrue(await until(lambda: "Sorry for the wait, here it is." in self.spoken))

    async def test_still_working_notices(self):
        os.environ["FAKE_LONG_S"] = "1.2"
        brain = await self.brain(progress_s=0.4)
        await self.ask(brain, "do the long thing")
        self.assertTrue(await until(lambda: "Finished the long task." in self.spoken))
        self.assertTrue(any(s.startswith("Still") for s in self.spoken[:-1]), self.spoken)

    async def test_recovers_when_claude_code_dies(self):
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "crash please"), "Oh no")
        self.assertTrue(await until(lambda: self.spoken))
        self.assertIn("lost my connection", self.spoken[0])
        self.assertEqual(await self.ask(brain, "hello"), "Hi there.")

    async def test_uses_the_claude_login_not_an_api_key(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        brain = await self.brain()
        await self.ask(brain, "hello")
        self.assertEqual({r["value"] for r in self.fake_log("api_key")}, {False})
        self.assertEqual(brain.lanes[0].api_key_source, "none")

    async def test_api_key_when_asked_for(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        config = self.config()
        config.lane.use_api_key = True
        brain = Brain(config, speak=self.spoken.append)
        self.brains.append(brain)
        await brain.start()
        await self.ask(brain, "hello")
        self.assertEqual({r["value"] for r in self.fake_log("api_key")}, {True})
        self.assertEqual(brain.lanes[0].api_key_source, "ANTHROPIC_API_KEY")

    async def test_out_of_credit_is_explained_once(self):
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "it broke"), API_ERRORS["billing_error"][0])
        self.assertEqual(
            await self.ask(brain, "it broke again"), "Same problem as before: the Anthropic account is out of credit."
        )
        await asyncio.sleep(0.3)
        self.assertEqual(self.spoken, [])

    async def test_older_claude_code_errors_are_explained_too(self):
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "oldbroke"), "Credit balance is too low")
        self.assertTrue(await until(lambda: self.spoken))
        self.assertEqual(self.spoken, [API_ERRORS["billing_error"][0]])

    async def test_ignores_filler_words(self):
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "umm"), "")
        self.assertEqual(self.fake_log("user"), [])

    async def test_looks_through_the_camera_when_asked(self):
        brain = await self.brain()

        async def look():
            return "aGVsbG8="

        self.assertEqual(await self.ask(brain, "look at this", look), "Let me look. I see a picture.")
        content = self.fake_log("user")[-1]["value"]
        self.assertEqual(content[0]["type"], "image")

        async def no_picture():
            return None

        reply = await self.ask(brain, "what am I looking at", no_picture)
        self.assertEqual(reply, "Let me look. I can't see anything.")
        self.assertIn("camera didn't send a picture", self.fake_log("user")[-1]["value"])

    async def test_writes_the_call_down(self):
        brain = await self.brain()
        await self.ask(brain, "hello")
        await self.ask(brain, "check my email")
        self.assertTrue(await until(lambda: self.spoken))
        text = brain.calls.path().read_text()
        self.assertIn("You: hello", text)
        self.assertIn("Claude: Hi there.", text)
        self.assertIn("Claude: Your inbox: two emails need you.", text)

    async def test_next_call_picks_up_the_conversation(self):
        os.environ["FAKE_SESSION"] = "11111111-2222-3333-4444-555555555555"
        brain = await self.brain()
        await self.ask(brain, "hello")
        await brain.close()
        self.brains.remove(brain)
        again = await self.brain()
        self.assertTrue(again.resumed)
        argv = self.fake_log("argv")[-1]["value"]
        self.assertEqual(argv[argv.index("--resume") + 1], "11111111-2222-3333-4444-555555555555")

    async def test_starts_fresh_when_the_old_session_is_gone(self):
        data = self.dir / "data"
        data.mkdir()
        (data / "voice-state.json").write_text(json.dumps(
            {"session": "gone", "cwd": str(self.dir), "updated": time.time(), "turns": 1}))
        brain = await self.brain()
        self.assertFalse(brain.resumed)
        self.assertEqual(await self.ask(brain, "hello"), "Hi there.")

    async def test_earlier_calls_are_passed_on(self):
        CallLog(self.dir / "data" / "calls").say("You", "remind me about the venue meeting")
        brain = await self.brain()
        await self.ask(brain, "hello")
        first = self.fake_log("user")[0]["value"]
        self.assertIn("Earlier calls on the glasses", first)
        self.assertIn("venue meeting", first)

    async def test_work_finishes_after_hanging_up_and_is_told_next_call(self):
        os.environ["FAKE_LONG_S"] = "0.8"
        brain = await self.brain()
        await self.ask(brain, "do the long thing")
        brain.config.finish_after_hangup_s = 5
        await brain.close()
        self.brains.remove(brain)
        self.assertEqual(self.spoken, [])
        again = await self.brain()
        self.assertEqual(again.parked(), ["Finished the long task."])
        self.assertEqual(again.parked(), [])


class QuickVoiceTest(BrainBase):
    """The quick voice up front, handing real work to the work lines."""

    def config(self, **overrides):
        config = super().config(**overrides)
        if "talk" not in overrides:
            config.talk = LaneConfig(cwd=str(self.dir), claude=FAKE, model="haiku", talk_only=True)
        return config

    def by_lane(self):
        """User messages, split into the quick voice's and the work lines'."""
        talk_pids = {r["pid"] for r in self.fake_log("argv") if "--strict-mcp-config" in r["value"]}
        users = self.fake_log("user")
        return [u for u in users if u["pid"] in talk_pids], [u for u in users if u["pid"] not in talk_pids]

    async def test_quick_answer_comes_from_the_quick_voice(self):
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "hello"), "Hi there, quick answer.")
        talk, work = self.by_lane()
        self.assertEqual(len(talk), 1)
        self.assertEqual(work, [])

    def assertAcked(self, reply, rest):
        ack = reply[: len(reply) - len(rest)].strip()
        self.assertTrue(reply.endswith(rest), reply)
        self.assertIn(ack, ACKS)

    async def test_obvious_work_skips_the_quick_voice(self):
        brain = await self.brain()
        self.assertAcked(await self.ask(brain, "check my email"), " Checking your inbox.")
        self.assertTrue(await until(lambda: self.spoken))
        self.assertEqual(self.spoken, ["Your inbox: two emails need you."])
        talk, work = self.by_lane()
        self.assertEqual(talk, [])
        self.assertEqual(len(work), 1)
        self.assertIn("already heard a quick", work[0]["value"])
        # the user's words aren't repeated as background
        self.assertNotIn("User: check my email", work[0]["value"])
        argv = [r["value"] for r in self.fake_log("argv") if "--strict-mcp-config" not in r["value"]][0]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        # the quick voice hears what happened
        await self.ask(brain, "thanks")
        talk, _ = self.by_lane()
        self.assertIn("User: check my email", talk[-1]["value"])
        self.assertIn("Your inbox: two emails need you.", talk[-1]["value"])

    async def test_the_quick_voice_hands_over_what_it_cannot_do(self):
        os.environ["FAKE_LONG_S"] = "0.5"
        brain = await self.brain()
        self.assertEqual(await self.ask(brain, "do the long thing"), "On it. Starting the long task.")
        talk, work = self.by_lane()
        self.assertEqual((len(talk), len(work)), (1, 1))
        self.assertTrue(await until(lambda: "Finished the long task." in self.spoken))

    async def test_advice_without_tools_streams_straight_through(self):
        brain = await self.brain()
        reply = await self.ask(brain, "a business question about my prices")
        self.assertAcked(reply, " Raise it to six dollars, your margins can take it.")
        self.assertEqual(self.spoken, [])

    async def test_a_stuck_quick_voice_hands_over(self):
        os.environ["FAKE_STALL_S"] = "3"
        brain = await self.brain(talk_decide_s=0.5)
        started = time.monotonic()
        reply = await self.ask(brain, "stall then answer")
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertIn(reply.split(" ")[0], ["On", "Let", "One", "Okay,"])
        _, work = self.by_lane()
        self.assertEqual(len(work), 1)

    async def test_works_without_the_quick_voice_when_it_cannot_start(self):
        talk = LaneConfig(cwd=str(self.dir), claude=str(self.dir / "no-such-claude"), talk_only=True)
        brain = await self.brain(talk=talk)
        self.assertEqual(await self.ask(brain, "hello"), "Hi there.")

    async def test_warm_up_before_the_first_question(self):
        brain = await self.brain(warm_up=True)
        self.assertTrue(await until(lambda: len(self.fake_log("user")) >= 2))
        self.assertEqual(await self.ask(brain, "hello"), "Hi there, quick answer.")
        talk, work = self.by_lane()
        self.assertIn("warm-up", talk[0]["value"])
        self.assertIn("warm-up", work[0]["value"])
        # warm-ups aren't part of the conversation
        self.assertNotIn("warm-up", brain.calls.path().read_text())


class WordsTest(unittest.TestCase):
    def test_yes_and_no(self):
        for words in ["yes", "Yeah, go ahead.", "okay", "sure thing", "do it"]:
            self.assertIs(yes_no(words), True, words)
        for words in ["no", "Nope.", "don't do that", "wait"]:
            self.assertIs(yes_no(words), False, words)
        self.assertIsNone(yes_no("what's on my calendar"))

    def test_work_hints(self):
        for words in ["what's the latest business news", "check my email", "what's on my calendar tomorrow",
                      "search for kiosk suppliers", "how much is a Tesla share right now", "fix the login bug"]:
            self.assertTrue(WORK_HINTS.search(words), words)
        for words in ["hey how's it going", "thanks man", "what's the capital of Portugal", "tell me a joke"]:
            self.assertFalse(WORK_HINTS.search(words), words)

    def test_look(self):
        for words in ["look at this", "What am I looking at?", "can you read this", "what's this"]:
            self.assertTrue(wants_look(words), words)
        for words in ["I see", "let's see what's next", "check my email"]:
            self.assertFalse(wants_look(words), words)

    def test_work_model_flags(self):
        cmd = LaneConfig(cwd="/x", chrome=True, mcp_config="/data/voice-mcp.json").command()
        self.assertIn("--chrome", cmd)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], "/data/voice-mcp.json")
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        self.assertIn("WebSearch", allowed)
        self.assertIn("mcp__email__search_email", allowed)
        self.assertNotIn("mcp__email__send_email", allowed)
        ask = json.loads(cmd[cmd.index("--settings") + 1])["permissions"]["ask"]
        self.assertIn("mcp__email__send_email", ask)
        self.assertIn("mcp__claude_ai_Gmail__send_message", ask)
        quick = LaneConfig(cwd="/x", chrome=True, talk_only=True).command()
        self.assertNotIn("--chrome", quick)

    def test_send_wording(self):
        p = Permission("r", "mcp__email__send_email",
                       {"from_account": "me@icloud.com", "to": "sam@example.com", "subject": "Kiosk delivery"}, "")
        self.assertEqual(p.spoken(), "send an email from me@icloud.com to sam@example.com, subject: Kiosk delivery")

    def test_permission_wording(self):
        p = Permission("r", "Bash", {"command": "git push"}, "")
        self.assertEqual(p.spoken(), "use Bash: git push")
        p = Permission("r", "mcp__gmail__create_draft", {}, "Draft a reply to Sam.")
        self.assertEqual(p.spoken(), "Draft a reply to Sam")


if __name__ == "__main__":
    unittest.main()
