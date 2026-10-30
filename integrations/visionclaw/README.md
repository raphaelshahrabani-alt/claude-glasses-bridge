# VisionClaw + Claude Code

[VisionClaw](https://github.com/sseanliu/VisionClaw) already handles the hard parts: the Meta glasses link, live voice, and camera frames. `claude-code.patch` adds one tool to its voice worker, `claude_code`, which hands tasks to your Claude Code session through this bridge. VisionClaw's own tools (`execute`, `browse`, notes and so on) keep working. The voice model chooses `claude_code` when you mention Claude, your computer, your code, or files on your machine.

The patch is written against VisionClaw commit `3f39807` (October 2026) and only touches `agent/main.py`.

## How VisionClaw is wired today

Older write-ups describe the phone calling OpenClaw directly. Current VisionClaw works differently:

1. The phone app publishes the glasses' mic and camera into a **LiveKit** room.
2. The **voice worker** (`agent/main.py`, Python, LiveKit Agents) joins the room and runs the voice model (Gemini Live or OpenAI Realtime).
3. Tool calls go from the worker to VisionClaw's **gateway** at `POST /v1/chat/completions`. The gateway also gives the phone its room ticket.

The patch adds a second destination for step 3. When `CLAUDE_CODE_URL` is set, the worker gets a `claude_code` tool that POSTs to this bridge, using the same request and response shape it already uses for the gateway. That includes the `[task started]` deferral for long tasks and draining `/pending-notifications` at the start of the next call.

## Setup

**On an iPhone and a Mac, follow [IPHONE.md](IPHONE.md)** instead: it covers every step, including running VisionClaw's gateway on your Mac. What follows is the short version.

1. **Get VisionClaw working first**, following its README: the gateway, a LiveKit Cloud project, the iOS or Android app, and Developer Mode in the Meta AI app. Voice calls should work before you add Claude Code.

2. **Apply the patch:**

   ```bash
   cd VisionClaw
   git apply /path/to/claude-glasses-bridge/integrations/visionclaw/claude-code.patch
   ```

3. **Start the bridge** on your computer. See the [main README](../../README.md): run `claude --dangerously-load-development-channels server:glasses` in the project you want Claude to work in. Check that `curl localhost:8790/health` shows `"claude_connected": true`.

4. **Run the voice worker on the same computer**, so the bridge can stay on `127.0.0.1`. The worker only makes outbound connections to LiveKit and your gateway, so it runs fine on a laptop:

   ```bash
   cd VisionClaw/agent
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   python main.py download-files

   export LIVEKIT_URL=wss://... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
   export GOOGLE_API_KEY=... GATEWAY_URL=https://your-gateway... GATEWAY_SERVICE_TOKEN=...
   export CLAUDE_CODE_URL=http://127.0.0.1:8790
   export CLAUDE_CODE_TOKEN="$(cat ~/.claude-glasses-bridge/token)"
   python main.py start
   ```

   If you also have the worker deployed on Fly, scale that one to zero while you use the local one (`fly scale count 0` in `agent/`). LiveKit hands each call to any registered worker, and the Fly worker doesn't have the `claude_code` tool.

   To keep the worker in the cloud instead, put both machines on Tailscale, start the bridge with `GLASSES_HOST=<the computer's Tailscale IP>`, and set `CLAUDE_CODE_URL` to `http://<that IP>:8790`.

5. **Call in from the glasses** and try things like:
   - "Ask Claude what changed in my repo today."
   - "Have Claude run the tests and tell me what failed."
   - Looking at your monitor: "Ask Claude what this error on my screen means." The voice model attaches the camera frame.

## Approvals

Start the bridge with `GLASSES_PERMISSION_RELAY=1` to approve tool calls by voice. The prompt comes back as the `claude_code` result, for example "...say yes k q r t x to allow it". The patched instructions tell the voice model to read it to you and pass your exact answer back. Once you approve, the same call waits for Claude to finish the original task, so you hear the result right away.

## What was tested

The patched `_claude_code_execute` and `_drain_claude_code_pending` were taken verbatim from the patched `agent/main.py` and run against a live bridge. The tests covered:

- a quick reply
- an attached camera frame
- a deferred task whose late result was drained afterwards
- a bad token

The patched file also compiles, and the patch applies cleanly to `3f39807`. It has not been run inside a full LiveKit call yet.
