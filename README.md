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
- **A glasses app.** [VisionClaw](integrations/visionclaw/README.md) with the patch in this repo, or anything that can make an HTTP POST (see [HTTP API](#http-api)).

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
- **Your glasses model:** the Wearables Device Access Toolkit's published device list has lagged behind new hardware. Check [Meta's docs](https://wearables.developer.meta.com/docs/develop/dat/getting-started-toolkit/) for your model.
