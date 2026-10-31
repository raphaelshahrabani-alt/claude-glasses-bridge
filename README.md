# claude-glasses-bridge

Talk to Claude Code through Meta smart glasses. Ask a question out loud, show it what you're looking at, and hear the answer from a Claude Code session running on your own computer.

The bridge is a Claude Code [channel](https://code.claude.com/docs/en/channels-reference): an MCP server that pushes messages into a running `claude` session and gives Claude a `reply` tool. It also serves a small HTTP API that the voice app on your phone calls. It's the Claude Code equivalent of the OpenClaw gateway that projects like VisionClaw use.

```
 Meta glasses ── camera + mic (Wearables Device Access Toolkit) ──▶ phone app
                                                                      │
                                     voice model (e.g. Gemini Live in VisionClaw)
                                                                      │  claude_code(task, camera frame)
                                                                      ▼
                         POST /v1/chat/completions  ── this bridge, on your computer
                                                                      │  <channel source="glasses" chat_id="…" image_path="…">
                                                                      ▼
                                              your interactive Claude Code session
                                                                      │  reply(chat_id, "…")
                                                                      ▼
                                       HTTP response ──▶ spoken back through the glasses
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

`bin/start-glasses [project-folder]` opens VisionClaw's gateway, its voice worker and a Claude Code session with the glasses channel, each in its own Terminal window, and skips any that are already running. Claude Code starts on Sonnet at low effort with auto permissions, because voice answers are only as fast as the model behind them; `GLASSES_MODEL`, `GLASSES_EFFORT` and `GLASSES_PERMISSION_MODE` override that. See [IPHONE.md](integrations/visionclaw/IPHONE.md#every-time-after-setup).

The worker starts with `CLAUDE_CODE_ONLY=1`, which turns VisionClaw's voice model into a thin voice for Claude Code. Its own instructions and tools are replaced by just `claude_code` and `quick_search`. Anything that needs knowledge or action goes to Claude Code. The voice model answers on its own only for small talk and simple questions about what's on camera. Live web facts like weather or news go to quick_search, which takes about two seconds. Without this mode the voice model answered most questions itself, knowing nothing about your projects.

The patch also keeps the glasses camera off during calls. When you ask about something in front of you, the worker turns it on for one picture and sends that to Claude Code, so the capture light doesn't stay on (iPhone app only).

## Checking the setup

`bin/glasses-doctor` checks each piece: the Claude Code session and bridge, VisionClaw's gateway, the voice worker and its mode, and the VisionClaw patch version. Anything wrong gets a ✗ and the fix. It then lists your recent questions with how long Claude Code took for each. Finally it sends two live test questions through the bridge and times them: a plain one, and one that runs `git status` in the project, to show Claude Code can work in your code. `--quick` skips the live questions.

## Faster answers

A spoken answer goes through three steps. First the voice model hears you, which takes a second or so. Then Claude Code works on it; this is the step `glasses-doctor` times. Finally the voice model speaks the answer, which takes another second or two. Nearly all the variation is in the Claude Code step:

- **Model and effort:** `start-glasses` uses Sonnet at low effort. `GLASSES_MODEL=haiku` is faster still. Opus is slower but better at hard coding work.
- **Context size:** a long-running session gets slower as its context grows. Type `/clear` in the Claude Code window when you switch topics.
- **Permission prompts:** a prompt in the Claude Code window stops the answer until someone approves it. Use auto mode (the `start-glasses` default), add allow rules, or turn on `GLASSES_PERMISSION_RELAY` to approve by voice.
- **Hooks and plugins:** anything that runs on every prompt or tool call, such as a memory plugin, adds its time to every answer. If the doctor's times are high, try with them turned off in `/plugin`.

## Configuration

Set these with `-e` when you register the server, for example `claude mcp add glasses --scope user -e GLASSES_PERMISSION_RELAY=1 -- bun "$PWD/src/server.ts"`.

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

- **Run `bun run talk` in a plain terminal, never in Claude Code's prompt.** If Claude runs it, Claude is stuck waiting on a command that is waiting for Claude.
- **`/mcp` shows glasses as failed:** usually Bun isn't on the `PATH` Claude Code sees. Register it with the full path: `claude mcp add glasses --scope user -- "$HOME/.bun/bin/bun" "$PWD/src/server.ts"`.
- **The server disconnects, or nothing arrives:** read `~/.claude-glasses-bridge/bridge.log`. It records every start, connection, message and exit, including the reason the bridge stopped. For Claude Code's side, start it with `--debug` and look in `~/.claude/debug/`.
- **"could not listen on 127.0.0.1:8790":** another Claude Code session with channels is already running the bridge. Each port can serve one session; quit the other, or set a different `GLASSES_PORT`.
- **"not listening: this Claude Code process was not started with channels enabled":** normal for other Claude Code processes. If it's the line for your channel session, tell us what the `started by` line before it says.

## Development

```bash
bun test            # spawns the server and plays Claude Code's side of the channel protocol
bun run typecheck
```

`test/bridge.test.ts` drives the real server over stdio with an MCP client. The client receives `<channel>` notifications, calls `reply`, sends permission requests and checks the verdicts.

## Status

This is v0.1, and some of it hasn't been run end to end:

- **Tested:** the channel protocol, against an MCP client that follows Claude Code's [channels reference](https://code.claude.com/docs/en/channels-reference). The VisionClaw patch was also run against a live bridge.
- **Not yet tested:** a live Claude Code session, and the full glasses → phone → LiveKit → bridge path.
- **Glasses:** Ray-Ban Meta Gen 3 streams through VisionClaw (MetaWearablesDAT 0.9.0), built with a free Apple ID. For other models, check [Meta's docs](https://wearables.developer.meta.com/docs/develop/dat/getting-started-toolkit/).
