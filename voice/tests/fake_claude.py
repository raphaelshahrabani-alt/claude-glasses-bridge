#!/usr/bin/env python3
"""Stands in for `claude -p` in streaming JSON mode. What it does depends on
the words in each message; every message it gets is appended to $FAKE_CLAUDE_LOG."""
import json
import os
import sys
import threading
import time
import uuid

session = os.environ.get("FAKE_SESSION") or str(uuid.uuid4())
args = sys.argv[1:]
if "--resume" in args:
    resumed = args[args.index("--resume") + 1]
    if resumed == "gone":
        print("No conversation found with session ID: gone", file=sys.stderr, flush=True)
        sys.exit(1)
    session = resumed
log_path = os.environ.get("FAKE_CLAUDE_LOG")
lock = threading.Lock()
interrupted = threading.Event()
answers: dict[str, dict] = {}
answer_ready = threading.Condition()


def out(message):
    with lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def record(kind, value):
    if log_path:
        with open(log_path, "a") as f:
            f.write(json.dumps({"pid": os.getpid(), "kind": kind, "value": value}) + "\n")


def event(e):
    out({"type": "stream_event", "event": e, "parent_tool_use_id": None, "session_id": session})


def say(text, index=0):
    event({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
    for word in text.split(" "):
        event({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": word + " "}})
        time.sleep(0.01)
    event({"type": "content_block_stop", "index": index})


def tool(name, index):
    event({"type": "content_block_start", "index": index,
           "content_block": {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}})
    event({"type": "content_block_stop", "index": index})


def wait(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if interrupted.is_set():
            return False
        time.sleep(0.02)
    return True


def result(text, ok=True):
    out({"type": "result", "subtype": "success" if ok else "error_during_execution", "is_error": not ok,
         "result": text, "session_id": session, "duration_ms": 10})


def handle(content):
    interrupted.clear()
    text = content if isinstance(content, str) else " ".join(
        b.get("text", "[image]") if b.get("type") == "text" else "[image]" for b in content)
    words = text.split("[The user just said]")[-1].lower()
    if "email" in words:
        say("Checking your inbox.")
        tool("mcp__plugin_small-business_gmail__search_threads", 1)
        wait(0.3)
        say("Your inbox: two emails need you.", 2)
        result("Your inbox: two emails need you.")
    elif "long" in words:
        say("Starting the long task.")
        tool("Bash", 1)
        if not wait(float(os.environ.get("FAKE_LONG_S", "1.5"))):
            result("", ok=False)
            return
        say("Finished the long task.", 2)
        result("Finished the long task.")
    elif "slow" in words:
        if not wait(float(os.environ.get("FAKE_SLOW_S", "1.0"))):
            result("", ok=False)
            return
        say("Sorry for the wait, here it is.")
        result("Sorry for the wait, here it is.")
    elif "file" in words:
        say("One moment.")
        tool("Bash", 1)
        rid = str(uuid.uuid4())
        out({"type": "control_request", "request_id": rid, "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "touch notes.txt"},
            "description": "Create the file notes.txt"}})
        with answer_ready:
            answer_ready.wait_for(lambda: rid in answers, timeout=10)
        behavior = answers.get(rid, {}).get("behavior")
        record("permission", behavior)
        say("Done, I made the file." if behavior == "allow" else "Okay, I skipped it.", 2)
        result("done")
    elif "crash" in words:
        say("Oh no")
        os._exit(3)
    elif "look" in words:
        say("I see a picture." if not isinstance(content, str) else "I can't see anything.")
        result("seen")
    else:
        say("Hi there.")
        result("Hi there.")


def main():
    record("argv", args)
    out({"type": "system", "subtype": "init", "session_id": session,
         "mcp_servers": [{"name": "gmail", "status": "connected"}]})
    worker = None
    for line in sys.stdin:
        message = json.loads(line)
        kind = message.get("type")
        if kind == "control_request":
            subtype = message["request"]["subtype"]
            record("control", subtype)
            if subtype == "interrupt":
                interrupted.set()
            out({"type": "control_response",
                 "response": {"subtype": "success", "request_id": message["request_id"], "response": {}}})
        elif kind == "control_response":
            response = message["response"]
            with answer_ready:
                answers[response["request_id"]] = response.get("response") or {}
                answer_ready.notify_all()
        elif kind == "user":
            content = message["message"]["content"]
            record("user", content)
            if worker is not None:
                worker.join()
            worker = threading.Thread(target=handle, args=(content,))
            worker.start()
    if worker is not None:
        worker.join()


main()
