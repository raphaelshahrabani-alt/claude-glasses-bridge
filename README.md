# claude-glasses-bridge

Talk to Claude Code through Meta smart glasses. Ask a question out loud, show it what you're looking at, and hear the answer from Claude Code running on your own computer, with your projects, email and calendar.

There are two ways to wire it, both through [VisionClaw](https://github.com/Intent-Lab/VisionClaw)'s phone app:

**Direct voice (the default).** What you say goes through speech-to-text straight to Claude Code, and its answer is spoken as it is written. No second AI model sits in between. See [Direct voice](#direct-voice).

```
 Meta glasses ── mic (and camera when asked) ──▶ VisionClaw app ──▶ LiveKit
                                                                      │
                     voice/worker.py on your Mac: speech-to-text ─▶ Claude Code (claude -p, streaming)
                                                                      │  first words as they're written
                                                                      ▼
                                                 text-to-speech ──▶ the glasses' speakers
```

**Gemini voice with the glasses channel (`GLASSES_VOICE=gemini`).** VisionClaw's Gemini Live model holds the conversation and hands requests to an interactive Claude Code session. The bridge in `src/` is a Claude Code [channel](https://code.claude.com/docs/en/channels-reference): an MCP server that pushes messages into a running `claude` session and gives Claude a `reply` tool, plus a small HTTP API the voice app calls. It's the Claude Code equivalent of the OpenClaw gateway.

```
 VisionClaw app ── Gemini Live ── claude_code(task, camera frame)
                                                                      │
                         POST /v1/chat/completions  ── this bridge, on your computer
                                                                      │  <channel source="glasses" chat_id="…" image_path="…">
                                                                      ▼
                                              your interactive Claude Code session
                                                                      │  reply(chat_id, "…")
                                                                      ▼
                                       HTTP response ──▶ spoken back by Gemini
```

## Requirements

- **Claude Code** with channels (a research preview), signed in with a claude.ai account or a Console API key. On Team and Enterprise plans an admin has to [enable channels](https://code.claude.com/docs/en/channels#enterprise-controls) first.
- **Bun** 1.1 or later.
- **A glasses app.** [VisionClaw](integrations/visionclaw/README.md) with the patch in this repo (iPhone and Mac: [step-by-step guide](integrations/visionclaw/IPHONE.md)), or anything that can make an HTTP POST (see [HTTP API](#http-api)).

## Quick start

```bash
git clone https://github.com/raphaelshahrabani-alt/claude-glasses-bridge && cd claude-glasses-bridge
bun install
bun run token                      # creates ~/.claude-glasses-bridge/token and prints it

# Register the channel once, for every project:
claude mcp add glasses --scope user -- bun "$PWD/src/server.ts"

# Then start Claude Code wherever you want it to work:
cd ~/code/some-project
claude --dangerously-load-development-channels server:glasses
```

Custom channels aren't on the research-preview allowlist, so they need `--dangerously-load-development-channels`. Claude Code shows a warning first; choose **I am using this for local development**. The flag only works in an interactive session. It is ignored with `-p` and in the Agent SDK. Run the session inside `tmux` or `screen` if you want it to stay up after you close the terminal.

Try it from a second terminal before you involve the glasses:

```bash
bun run talk "what files are in this project?"
bun run talk --image ~/Desktop/screenshot.png "what does this error mean?"
bun run talk --events              # watch everything crossing the bridge
```

**Allow the reply tool**, or every answer will wait at a permission dialog in the terminal. Choose "don't ask again" the first time it asks, or add this to `~/.claude/settings.json`:

```json
{ "permissions": { "allow": ["mcp__glasses__reply"] } }
```

## Starting everything (VisionClaw)

`bin/start-glasses [project-folder] [more-folders...]` opens each piece in its own Terminal window and skips any that are already running. Claude Code works in the first folder and can also read and change the others. By default that is VisionClaw's gateway (the phone logs in through it) and the direct voice worker, `bin/glasses-voice`. Claude Code runs on Sonnet at low effort with auto permissions; `GLASSES_MODEL`, `GLASSES_EFFORT` and `GLASSES_PERMISSION_MODE` override that. See [IPHONE.md](integrations/visionclaw/IPHONE.md#every-time-after-setup).

`GLASSES_VOICE=gemini start-glasses ...` starts the older setup instead: the gateway, VisionClaw's Gemini worker with `CLAUDE_CODE_ONLY=1`, and a Claude Code session with the glasses channel. There, the voice model answers chat and general knowledge itself and sends anything about your code, files or computer to Claude Code. Long jobs reply at once and send a follow-up when done. Folders are passed with `--add-dir`, and permission prompts are read out on the glasses (`GLASSES_PERMISSION_RELAY=0` turns that off).

Either way the patched app keeps the glasses camera off during calls. When you ask about something in front of you, the worker turns it on for one picture, so the capture light doesn't stay on (iPhone app only).

## Direct voice

`bin/glasses-voice` runs `voice/worker.py`, a LiveKit agent that joins your calls in place of VisionClaw's Gemini worker (it stops that one if it's running). The first run sets up its own Python environment in `~/.claude-glasses-bridge/voice-venv`; it reads the LiveKit keys from `~/VisionClaw/gateway/.env`.

- **Straight to Claude Code.** Each call starts Claude Code headless (`claude -p` with streaming input and output) in your project folder, with your profile and voice rules (`voice/prompt.md`) as extra instructions. Your words go in as you finish a sentence; Claude's first sentence is spoken while it is still writing the rest. In a test on Sonnet, a simple answer started after about 2.5 seconds and a Gmail check after 2.7.
- **Keeps talking while it works.** When Claude goes off to use tools, the call is free again. Ask something else and a second Claude Code process ("line") answers, told what the first one is doing; the first one's result is spoken when it's ready. If a line goes quiet for 25 seconds you hear "Still on it." Say "stop" or "never mind" to cancel the newest request.
- **Approvals by voice.** When Claude Code needs permission, you hear "Quick check before I go on: ... Is that okay?" Say yes or no. Read-only Gmail, Calendar and Drive tools are pre-approved. With no answer in two minutes, the request is turned down.
- **Remembers.** A call within 30 minutes of the last one continues the same Claude Code session (`GLASSES_RESUME_MINUTES`). Earlier, every new line is also told the end of today's and yesterday's calls.
- **Where the chats go.** Every call is written down in `~/.claude-glasses-bridge/calls/<date>.md`. The Claude Code sessions are saved like any other: `cd ~/Desktop && claude --resume` lists them, so you can carry on at the keyboard.
- **Hanging up doesn't lose work.** Work still running when the call ends gets up to 10 more minutes (`GLASSES_FINISH_AFTER_HANGUP_S`). Its result is told at the start of your next call ("While you were away: ...").
- **Looking.** "Look at this", "what am I looking at", "read this" and similar turn the glasses camera on for one picture, which goes to Claude Code with your question.

**Speech-to-text and the voice** use [LiveKit Inference](https://docs.livekit.io/agents/models/) by default (Deepgram Nova-3 and a Cartesia voice), billed to your LiveKit Cloud project. No other account is needed, but the free Build plan includes only $2.50 of credits a month, about 50 minutes of calls, and then speech stops working (`glasses-doctor` says so). For more, either upgrade the LiveKit plan, or create a [Deepgram](https://deepgram.com) account (it starts with free credit) and put the key in `~/.claude-glasses-bridge/voice.env`. Speech-to-text and the voice then come from Deepgram directly:

```
DEEPGRAM_API_KEY=your-key
```

Other settings that can go in `voice.env` (restart the voice window after changing it):

| Variable | Default | |
| - | - | - |
| `GLASSES_MODEL` | `sonnet` | `haiku` answers about a second sooner; `opus` is slower but better at hard work. |
| `GLASSES_EFFORT` | `low` | |
| `GLASSES_PERMISSION_MODE` | `auto` | |
| `GLASSES_LANGUAGE` | `en` | Speech-to-text language, such as `fr`, or `multi` for several. |
| `GLASSES_STT`, `GLASSES_TTS` | Deepgram Nova-3, Cartesia Sonic 3 | LiveKit Inference models, as `provider/model:language` and `provider/model:voice`. |
| `GLASSES_DEEPGRAM_VOICE` | `aura-2-thalia-en` | The voice, when `DEEPGRAM_API_KEY` is set. |
| `GLASSES_LINES` | `3` | How many requests can run at once. |
| `GLASSES_RESUME_MINUTES` | `30` | |
| `GLASSES_FINISH_AFTER_HANGUP_S` | `600` | |
| `GLASSES_USE_API_KEY` | off | Claude Code in the voice worker uses your Claude subscription login, and leaves out any `ANTHROPIC_API_KEY` in your environment, which `claude -p` would otherwise always bill instead. Set to `1` to pay with the key. |

Speech-to-text listens for your project names: it takes them from the "My projects" list in your profile, plus any names you put one per line in `~/.claude-glasses-bridge/keyterms.txt`.

## A personal agent and business partner

Three things turn the glasses session into an agent across your work:

- **One folder holding all your projects.** Start Claude Code there, for example `start-glasses ~/Desktop`. It can then work in any project and create new ones next to them. Other folders can be added as extra arguments.
- **A profile.** Copy `templates/business-partner.md` to `~/.claude-glasses-bridge/persona.md` and fill it in: who you are, your projects and where they live, where new projects go, and what Claude must check with you first. Every call loads it.
- **Email and calendar.** Run `claude` in Terminal, type `/mcp`, choose Gmail (and Google Calendar, Drive, or anything else you use), and sign in once in the browser. The voice worker's Claude Code uses the same connections.

Whatever your profile says, Claude is told never to act in your name without a clear yes. That covers sending email or messages, posting or publishing, spending money, pushing to a main branch or deploying, and deleting your files or data. Permission prompts are read out on the glasses for you to approve by voice.

## Checking the setup

`bin/glasses-doctor` checks each piece and marks it ✓ or ✗ with the fix. With the direct voice it checks:

- VisionClaw's gateway.
- The voice worker, and whether it's running the current code.
- The LiveKit keys, and your profile.
- How quickly recent answers started, from `voice.log`.
- Speech errors and running out of LiveKit credits.
- Connectors that weren't connected.

It then starts Claude Code exactly as the voice worker does and times a plain question and a Gmail check, which also shows whether Gmail and Calendar are connected. With the Gemini voice it checks the bridge, the Claude Code session, the worker and the VisionClaw patch, and times two questions through the bridge. `--quick` skips the live tests.

## Faster answers

With the direct voice, an answer starts when speech-to-text notices you stopped talking (about half a second) and Claude Code writes its first words; `glasses-doctor` shows both. What changes the Claude Code part:

- **Model and effort:** Sonnet at low effort is the default. `GLASSES_MODEL=haiku` in `voice.env` starts about a second sooner. Opus is slower but better at hard coding work.
- **First questions about a project:** Claude Code has to read around a project it doesn't know yet, which is slow. Run `/init` once in each project to give it a `CLAUDE.md` it reads up front.
- **Long sessions:** a session that runs all day gets slower as its context grows. Calls more than 30 minutes apart start fresh; lower `GLASSES_RESUME_MINUTES` to start fresh more often.
- **Hooks and plugins:** anything that runs on every prompt or tool call, such as a memory plugin, adds its time to every answer. If the doctor's times are high, try with them turned off in `/plugin`.

With the Gemini voice, add a second or two on each side for the voice model to hear you and to speak, and the same points apply to the Claude Code session in its window. Permission prompts there stop the answer until someone approves them, so keep auto mode or `GLASSES_PERMISSION_RELAY` on.

## Configuration (the glasses channel)

These configure the bridge in `src/`, which the Gemini voice uses. Set them with `-e` when you register the server, for example `claude mcp add glasses --scope user -e GLASSES_PERMISSION_RELAY=1 -- bun "$PWD/src/server.ts"`.

| Variable | Default | |
| - | - | - |
| `GLASSES_HOST` | `127.0.0.1` | Interface to listen on. Keep the default when the voice worker runs on the same machine. Otherwise use your Tailscale IP. |
| `GLASSES_PORT` | `8790` | |
| `GLASSES_TOKEN` | contents of `~/.claude-glasses-bridge/token` | Bearer token for every endpoint except the two health probes. Must be at least 16 characters. |
| `GLASSES_DATA_DIR` | `~/.claude-glasses-bridge` | Token file, plus `frames/`, which keeps the latest 100 camera frames. |
| `GLASSES_WAIT_S` | `110` | How long a request waits for Claude's reply before answering "still working". A later reply goes to `/pending-notifications`. |
| `GLASSES_PERMISSION_RELAY` | off | Set to `1` to approve or deny tool calls by voice. See below. |
| `GLASSES_LISTEN` | `auto` | `auto` opens the port only in a Claude Code session started with `--channels` or `--dangerously-load-development-channels`. `always` opens it in every session. |

Registering at user scope means every Claude Code process starts a copy of the bridge. That includes other sessions, Claude Code's background spare processes (`claude bg-spare`), and plugins that run Claude in the background. In `auto` mode those copies stay quiet, so the port belongs to the session that can actually deliver your messages.

## Approving tool calls by voice

With `GLASSES_PERMISSION_RELAY=1`, an approval prompt in Claude Code is also sent to the glasses. Suppose you're waiting on a request and Claude needs to run something that requires approval. Your request is then answered with the prompt, written to be read aloud:

> Claude Code is asking for approval to use Bash: Remove the build directory. Details: {"command": "rm -rf build"}. To allow it, say "yes k q r t x". To deny it, say "no k q r t x".

Saying "yes k q r t x" (spelled-out letters and speech-to-text punctuation are fine) sends the verdict. That request then stays open for Claude's answer to the task you originally asked for. A verdict only counts if its ID matches an open prompt; otherwise the text goes to Claude as an ordinary message. The terminal dialog stays live too, and whichever answer comes first wins.

Leave this off unless you trust everyone who can reach the bridge with the token, because the token alone is enough to approve tool use.

## HTTP API

Every endpoint takes `Authorization: Bearer <token>`. The exceptions are `GET /health` and `GET /v1/chat/completions`, which are reachability probes.

| Endpoint | |
| - | - |
| `POST /v1/chat/completions` | OpenAI-style. Takes the last `user` message. An image can come as a top-level base64 `image` field (VisionClaw's format) or an inline `data:image/...` `image_url` part; images at http(s) URLs are ignored and never fetched. Returns `choices[0].message.content`. If Claude takes longer than `GLASSES_WAIT_S`, the content starts with `[task started]`. Supports `"stream": true`. |
| `POST /message` | `{ "text": "...", "image": "<base64>", "wait": true }` → `{ "status": "replied", "chat_id", "reply" }`, `{ "status": "pending", "chat_id" }`, or `{ "status": "verdict", "request_id", "behavior", "reply" }`. |
| `GET /pending-notifications` | `{ "notifications": [...] }`: replies that arrived after their request stopped waiting. Reading them clears the list. |
| `POST /pending-notifications` | `{ "text": "..." }` queues a notification (VisionClaw compatibility). |
| `POST /permission` | `{ "request_id": "kqrtx", "behavior": "allow" \| "deny" }` for apps with a button. Needs the relay to be on. |
| `GET /events` | Server-sent events stream of every message, reply, permission prompt and verdict as JSON. Also accepts `?token=` because `EventSource` can't set headers. |
| `GET /health` | `{ "ok": true, "claude_connected": true, "permission_relay": false }` |

Images must be JPEG, PNG or WebP (checked by their magic bytes) and at most 10 MB. They're saved to `frames/`, and their path is passed to Claude as `image_path` so it can Read them.

## Reaching the bridge

The bridge listens on `127.0.0.1` by default, so only processes on the same computer can reach it.

- **Recommended:** run the voice worker on the same computer. VisionClaw's LiveKit worker only makes outbound connections, so it runs fine on a laptop. See [integrations/visionclaw](integrations/visionclaw/README.md).
- **Otherwise:** use [Tailscale](https://tailscale.com) and set `GLASSES_HOST` to the machine's Tailscale IP. Don't port-forward it to the internet. Anyone with the token can run Claude Code on your computer, and the bridge has no rate limiting or TLS of its own.

## Security notes

- **It's a remote control for your shell.** Claude's normal permission rules still apply, so keep them on. Don't run this with `--dangerously-skip-permissions`.
- **The voice model is in the loop.** It paraphrases what you say before Claude sees it. With the relay on, it is also what repeats your yes or no. VisionClaw's patched instructions tell it never to approve on its own, but that's an instruction, not a guarantee.
- **The token is the only gate.** It is checked in constant time. Rotate it by deleting `~/.claude-glasses-bridge/token` and restarting the session.
- **Camera frames stay on disk** in `~/.claude-glasses-bridge/frames` (latest 100). Delete them whenever you like.

## Troubleshooting

- **It says "credit balance is too low":** Claude Code was paying with an Anthropic API key instead of your Claude subscription. The voice worker now leaves `ANTHROPIC_API_KEY` out; update and restart start-glasses. If it still happens, `glasses-doctor`'s live test says which account Claude Code uses (an Anthropic Console login also bills prepaid credit: `/logout`, then `/login` with your Claude account).
- **"Agent left the call" or no answer with the direct voice:** look at the "glasses: voice" window and `~/.claude-glasses-bridge/voice.log`, then run `glasses-doctor`. If LiveKit Inference ran out of credits, add a Deepgram key (see [Direct voice](#direct-voice)).
- **Run `bun run talk` in a plain terminal, never in Claude Code's prompt.** If Claude runs it, Claude is stuck waiting on a command that is waiting for Claude.
- **`/mcp` shows glasses as failed:** usually Bun isn't on the `PATH` Claude Code sees. Register it with the full path: `claude mcp add glasses --scope user -- "$HOME/.bun/bin/bun" "$PWD/src/server.ts"`.
- **The server disconnects, or nothing arrives:** read `~/.claude-glasses-bridge/bridge.log`. It records every start, connection, message and exit, including the reason the bridge stopped. For Claude Code's side, start it with `--debug` and look in `~/.claude/debug/`.
- **"could not listen on 127.0.0.1:8790":** another Claude Code session with channels is already running the bridge. Each port can serve one session; quit the other, or set a different `GLASSES_PORT`.
- **"not listening: this Claude Code process was not started with channels enabled":** normal for other Claude Code processes. If it's the line for your channel session, tell us what the `started by` line before it says.

## Development

```bash
bun test            # spawns the server and plays Claude Code's side of the channel protocol
bun run typecheck
python3 -m unittest discover -s voice/tests   # the voice brain against a stand-in for claude -p
~/.claude-glasses-bridge/voice-venv/bin/python -m unittest discover -s voice/tests   # plus the LiveKit side
```

`voice/tests/fake_claude.py` speaks Claude Code's streaming JSON protocol (stream events, tool calls, permission requests, interrupts), so the brain's tests cover streaming, multitasking, approvals, cancelling, crashes and resuming without using Claude.

`test/bridge.test.ts` drives the real server over stdio with an MCP client. The client receives `<channel>` notifications, calls `reply`, sends permission requests and checks the verdicts.

## Status

This is v0.2, and some of it hasn't been run end to end:

- **Tested:** the channel protocol, against an MCP client that follows Claude Code's [channels reference](https://code.claude.com/docs/en/channels-reference), and the VisionClaw patch against a live bridge. The direct voice's brain was tested against the real `claude -p`: answers, a voice approval, a tool call. Its LiveKit side was tested in LiveKit's text mode.
- **Not yet tested here:** a full direct-voice call from the glasses, with LiveKit Inference speech.
- **Glasses:** Ray-Ban Meta Gen 3 streams through VisionClaw (MetaWearablesDAT 0.9.0), built with a free Apple ID. For other models, check [Meta's docs](https://wearables.developer.meta.com/docs/develop/dat/getting-started-toolkit/).
