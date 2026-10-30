# iPhone + Mac setup, step by step

This guide connects your Meta glasses to Claude Code through VisionClaw. When it's finished, everything runs on your Mac except the phone app and two cloud services:

| Where | What |
| - | - |
| Your Mac | VisionClaw's gateway (hands your phone a call ticket), its voice worker (Gemini Live), and Claude Code with the bridge |
| Cloud | LiveKit (carries audio and video between your phone and your Mac) and Gemini (the voice) |
| iPhone | The VisionClaw app, talking to your glasses |

You'll end up with three Terminal tabs: **gateway**, **claude**, **worker**. Your iPhone and Mac need to be on the same Wi-Fi.

## 1. One-time installs and accounts

1. **Homebrew.** Run `brew --version`. If you get "command not found", install it with the command on https://brew.sh.
2. **Node and Python:**
   ```bash
   brew install node python@3.12
   ```
3. **Xcode.** Install it from the Mac App Store and open it once to finish setup. Then go to Xcode → Settings → Accounts and add your Apple ID.
4. **Gemini API key.** Create one at https://aistudio.google.com/apikey and keep it handy.
5. **LiveKit Cloud** (the free tier is enough):
   - Sign up at https://cloud.livekit.io and create a project.
   - In the project, go to **Settings → API Keys** and create a key.
   - Keep three values: the **WebSocket URL** (`wss://….livekit.cloud`), the **API Key**, and the **API Secret**.

## 2. Download VisionClaw and add Claude Code to it

```bash
cd ~
git clone https://github.com/sseanliu/VisionClaw
cd VisionClaw
git apply ~/claude-glasses-bridge/integrations/visionclaw/claude-code.patch
```

If `git apply` reports an error, VisionClaw has changed since the patch was written. Go back to the version the patch was made for and apply it again:

```bash
git checkout 3f39807 && git apply ~/claude-glasses-bridge/integrations/visionclaw/claude-code.patch
```

## 3. Gateway (tab 1: "gateway")

The commands below write the gateway's settings file. They also generate two secrets: your **access code** and a **service token**.

```bash
cd ~/VisionClaw/gateway
npm install
cat > .env <<EOF
ANTHROPIC_API_KEY=sk-ant-placeholder
PORT=8788
GATEWAY_TOKENS=$(openssl rand -hex 8):me
GATEWAY_SERVICE_TOKEN=$(openssl rand -hex 16)
STORE_PATH=./data/gateway-store.json
LIVEKIT_URL=paste-websocket-url-here
LIVEKIT_API_KEY=paste-api-key-here
LIVEKIT_API_SECRET=paste-api-secret-here
EOF
open -e .env
```

TextEdit opens. Replace the three `paste-…` values with your LiveKit values, save, and close it. Then start the gateway:

```bash
npm run dev
```

- **Expected:** `[gateway] listening on :8788`.
- **If macOS asks** whether `node` may accept incoming connections, click **Allow**.

**About the placeholder Anthropic key:** VisionClaw has its own action agent, the `execute` tool, for email, reminders and so on. It runs on Anthropic's API, so it won't work with a placeholder key, and Claude Code does the work instead. If you want it as well, put a real Console API key in `.env` and run `npm run provision` once.

## 4. Claude Code with the bridge (tab 2: "claude")

```bash
cd ~/claude-glasses-bridge
claude --dangerously-load-development-channels server:glasses
```

You can use any project folder instead of `~/claude-glasses-bridge`. Choose "I am using this for local development", then leave the tab alone.

## 5. Voice worker (tab 3: "worker")

```bash
cd ~/VisionClaw/agent
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py download-files
```

Those install steps only need to run once. Next, save a start script so you enter your Gemini key only once:

```bash
cat > ~/VisionClaw/agent/start.sh <<'EOF'
#!/bin/bash
cd ~/VisionClaw/agent
source .venv/bin/activate
export GOOGLE_API_KEY=paste-gemini-key-here
export $(grep -E '^(LIVEKIT_URL|LIVEKIT_API_KEY|LIVEKIT_API_SECRET|GATEWAY_SERVICE_TOKEN)=' ~/VisionClaw/gateway/.env)
export GATEWAY_URL=http://127.0.0.1:8788
export CLAUDE_CODE_URL=http://127.0.0.1:8790
export CLAUDE_CODE_TOKEN="$(cat ~/.claude-glasses-bridge/token)"
exec python main.py start
EOF
chmod +x ~/VisionClaw/agent/start.sh
open -e ~/VisionClaw/agent/start.sh
```

Replace `paste-gemini-key-here` with your Gemini key (no quotes), save, and close TextEdit. Then start the worker:

```bash
~/VisionClaw/agent/start.sh
```

Wait for a line containing `registered worker`. If you also deployed VisionClaw's worker to Fly at some point, stop that copy (`fly scale count 0` in `agent/`), or it may take your calls.

## 6. Build the iPhone app

1. **Point the app at your Mac** (in tab 1 or a new tab):
   ```bash
   cd ~/VisionClaw/samples/CameraAccess
   cp CameraAccess/Secrets.swift.example CameraAccess/Secrets.swift
   sed -i '' "s|https://api.visionagents.app|http://$(scutil --get LocalHostName).local:8788|" CameraAccess/Secrets.swift
   grep cloudGatewayURL CameraAccess/Secrets.swift
   open CameraAccess.xcodeproj
   ```
   The `grep` should print your Mac's name followed by `.local:8788`.

2. **Wait for Xcode to finish loading packages.** It downloads the Meta glasses SDK and LiveKit; you can see the progress in the top bar.

3. **Set up signing:**
   - Click the blue **CameraAccess** project at the top left, then the **CameraAccess** target, then **Signing & Capabilities**.
   - **Team:** your Apple ID ("Personal Team").
   - **Bundle Identifier:** change it to something only you use, such as `com.yourname.visionclaw`.
   - **If Xcode reports that your team doesn't support "Hotspot Configuration" or "Access WiFi Information":** a free Apple ID may not be allowed those capabilities. You have two options:
     - Delete both capabilities with the **×** next to each. The iPhone-camera mode will still work, but streaming from the glasses may not, because the glasses SDK uses Wi-Fi for video.
     - Join the Apple Developer Program ($99 a year) and keep them.

4. **Prepare the iPhone:**
   - Plug it into the Mac with a cable.
   - Turn on **Settings → Privacy & Security → Developer Mode**, and restart when asked. If the toggle isn't there, it appears after Xcode has seen the phone once.

5. **Pin LiveKit to 2.18.0.** VisionClaw (last updated Oct 21, 2026) was built against LiveKit's Swift SDK 2.18.0. LiveKit 2.19.0 (Oct 27) changed APIs it uses, so Xcode's default "latest 2.x" fails to build with "Expression is 'async' but is not marked with 'await'" in `LiveKitSession.swift`. You can set the rule in Xcode's Package Dependencies tab, but a leftover `Package.resolved` can keep the old pick: in testing it went on resolving LiveKit 2.0.0, whose WebRTC download now returns 404. The commands below set the pin in the project file and clear that lock file and the build cache, then resolve from the command line so you can see the result. Quit Xcode (⌘Q) first, then:
   ```bash
   cd ~/VisionClaw/samples/CameraAccess
   python3.12 - <<'PY'
   import re, pathlib
   p = pathlib.Path("CameraAccess.xcodeproj/project.pbxproj")
   s = p.read_text()
   pat = re.compile(r'(repositoryURL = "https://github.com/livekit/client-sdk-swift";\s*requirement = \{)(.*?)(\};)', re.S)
   m = pat.search(s)
   print("was:", " ".join(m.group(2).split()))
   s = s[:m.start(2)] + "\n\t\t\t\tkind = exactVersion;\n\t\t\t\tversion = 2.18.0;\n\t\t\t" + s[m.end(2):]
   p.write_text(s)
   print("now: kind = exactVersion; version = 2.18.0;")
   PY
   rm -f CameraAccess.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved
   rm -rf ~/Library/Developer/Xcode/DerivedData/CameraAccess-*
   xcodebuild -resolvePackageDependencies -project CameraAccess.xcodeproj
   open CameraAccess.xcodeproj
   ```
   The `xcodebuild` output should list `client-sdk-swift` at **2.18.0** and `webrtc-xcframework` at **150.x**.

6. **Run the app.** Pick your iPhone as the run destination at the top of Xcode and press **⌘R**.
   - The first time, iOS blocks the app until you trust it: **Settings → General → VPN & Device Management** → your Apple ID → **Trust**. Then press ⌘R again.
   - With a free Apple ID, the app stops opening after 7 days; press ⌘R to reinstall it.

7. **Unlock the app.** On the first screen, skip Google sign-in.
   - Open **"Have an access code or your own gateway?"**. The access code is the part of `GATEWAY_TOKENS` before `:me`; print it with `grep GATEWAY_TOKENS ~/VisionClaw/gateway/.env`.
   - Under **"Using your own gateway?"**, the URL should already be your Mac's. Submit.
   - When iOS asks to find devices on your local network, tap **Allow**.

## 7. First call, with the iPhone camera

Test the pipeline with the phone's own camera before adding the glasses:

1. Tap **Start on iPhone**, then the AI/call button.
2. Say: *"Ask Claude what the latest commit in this repo is."*

The **claude** tab should show `← glasses: …`, and you should hear the answer. If something doesn't work, check in this order:

1. **Gateway tab:** any errors?
2. **Worker tab:** did it join a room, and does it log `claude_code start` when you ask?
3. **Claude Code side:** run `tail ~/.claude-glasses-bridge/bridge.log`.

## 8. With the glasses

1. **Enable Developer Mode for the glasses.** In the **Meta AI** app: Settings → App Info → tap the version number 5 times, go back, and turn on **Developer Mode**.
2. **Connect and call.** In VisionClaw, tap **Start Streaming** to connect the glasses, then the AI button.
3. **Try:**
   - *"Ask Claude what changed in my repo today."*
   - While looking at your screen: *"Ask Claude what this error means."*

If the glasses never connect, check that your model is listed as supported in [Meta's toolkit docs](https://wearables.developer.meta.com/docs/develop/dat/getting-started-toolkit/). Newer models have appeared there later than in stores.

## Every time after setup

1. **gateway tab:** `cd ~/VisionClaw/gateway && npm run dev`
2. **claude tab:** `claude --dangerously-load-development-channels server:glasses` in your project.
3. **worker tab:** `~/VisionClaw/agent/start.sh`
4. Keep the Mac awake while you're out with the glasses. Running `caffeinate -dims` in a spare tab does it.
