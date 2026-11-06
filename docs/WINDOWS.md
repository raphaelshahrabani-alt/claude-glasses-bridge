# Running the glasses on a Windows PC

This moves the glasses assistant from your MacBook to a PC that stays on, so it works whenever you
call, and (with Tailscale) from anywhere, not just on your home Wi-Fi.

> **Honest note:** this was written and tested on Linux and macOS, not on a real Windows PC. The
> Windows-specific parts are small and covered by tests with stand-ins, but the first run on your PC
> is the real test. If a step fails, send a screenshot of the PowerShell window and it'll be fixed.

## What moves, and what stays on the Mac

| Moves to the PC | Stays on the Mac |
| - | - |
| Claude Code (logged in to your Claude account) | Xcode, for installing the phone app |
| The voice program and VisionClaw's login service | |
| Your profile, email accounts and keys (you enter them again) | |
| Chrome with the Claude extension | |
| Your project folders | |

The phone app was installed with a free Apple account, which expires every 7 days. About once a
week, plug in the iPhone and press ▶ in Xcode on the MacBook. That's the only time the Mac is needed.

## Part 1: Install on the PC

Open **PowerShell**: press the Windows key, type `PowerShell`, press Enter. Run each command by
pasting it and pressing Enter.

**1. Python and Git**

Use the websites; they're the most reliable. (`winget`, Windows' built-in installer, hangs on some PCs.)

- **Git:** go to **git-scm.com/download/win**, click **Click here to download**, open the file, click
  **Next** on every screen, then **Install** and **Finish**.
- **Python:** go to **python.org/downloads/release/python-31210/** (Python 3.12.10), scroll down to
  *Files*, and click **Windows installer (64-bit)**. Open it, and **tick "Add python.exe to PATH"** at
  the bottom of the first screen before you click **Install Now**.

  Don't use the big yellow button on python.org's front page: it now gives a newer "Python Install
  Manager", which can get stuck on "Getting the system ready for install...". If Python 3.12.10 has no
  Windows installer, use Python 3.13 the same way (python.org/downloads, find *Python 3.13.x*, take its
  *Windows installer (64-bit)*).

Then open a **new** PowerShell and check both: `py --version` and `git --version`.

**2. Claude Code**

```powershell
irm https://claude.ai/install.ps1 | iex
```

Close PowerShell, open a new one, type `claude`, press Enter, and log in with the same Claude
account you use on the Mac. When you see the prompt, type `/exit`.

**3. Download this software**

```powershell
cd $HOME
git clone https://github.com/raphaelshahrabani-alt/claude-glasses-bridge
cd claude-glasses-bridge
```

**4. Run the setup**

```powershell
py glasses.py setup
```

It installs Node.js and Tailscale if they're missing (it tries `winget`, gives up after 10 minutes if
that hangs, and then names the website to use instead; Windows asks for permission; click Yes),
downloads VisionClaw, installs everything, and asks for your LiveKit details:

- On **cloud.livekit.io**, open your project, then **Settings → API Keys → Create key** (name it
  `pc`). Copy the **secret** right away; it's shown only once.
- The address is on the same Settings page and looks like `wss://your-project.livekit.cloud`.
- For the **access code**, press Enter to make a new one, or type one you choose. It's what you type
  into the phone app.

If it prints **Still to do**, do those things, then run `py glasses.py setup` again. It picks up where
it left off and skips what's done.

If you need a new access code (for example, because it showed up in a screenshot), run
`glasses newcode`, write down the new one, and restart with `glasses stop` then `glasses start`.

**5. Make `glasses` work from anywhere** (paste once, then open a new PowerShell window)

```powershell
$p = [Environment]::GetEnvironmentVariable('Path','User'); [Environment]::SetEnvironmentVariable('Path', "$p;$HOME\claude-glasses-bridge", 'User')
```

Then `glasses help` shows every command.

## Part 2: Your keys, email and profile

Create new ones here (the old ones on the Mac still work; each is separate):

```powershell
glasses key ELEVEN_API_KEY
glasses key DEEPGRAM_API_KEY
glasses email add you@gmail.com --name "Your Name"
glasses email add your-other-address@gmail.com --name "Your Name"
glasses email list
```

Each key is checked as you save it. For email, make a new Google **app password** for each account
(myaccount.google.com/apppasswords); the old ones can't be shown again. Paste each into the prompt;
nothing shows while you paste.

**Your profile.** This is how the glasses know who you are and which businesses to follow in the news.
Run:

```powershell
glasses profile
```

It asks short questions: your name, what you do, your time zone, then for each business its name, what
it does, its website and competitors or names to watch. Then news topics you care about, and any
software projects. Press Enter to skip any question. When you ask "what's new" or "catch me up", it
searches the web for each business, competitor and topic you listed. Run `glasses profile` again
whenever something changes: it offers to keep the profile, edit it in Notepad (E), or redo the
questions (R; the old one is saved as `persona.md.bak`). Restart afterwards with `glasses stop`, then
`glasses start`.

**Chrome control.** On the PC, open Chrome, install the **Claude** extension from the Chrome Web
Store, and sign in. Then in PowerShell: `claude`, type `/chrome`, choose **Enabled by default**, then
`/exit`. Chrome has to be open for the glasses to use it.

**Your projects.** Put them where Claude Code can see them, for example on the PC's Desktop. If a
project is on GitHub: `cd $HOME\Desktop` then `git clone <its address>`. Others you can copy over with
a USB stick or a cloud drive.

## Part 3: Let your phone reach the PC

Right now your phone finds the software through your home Wi-Fi. To reach it from anywhere, use
Tailscale: a free private network for your own devices. Nothing is opened to the public internet.

1. Open **Tailscale** from the Start menu and sign in (Google, Apple or Microsoft account).
2. In a browser, go to **login.tailscale.com/admin/dns** and check that **MagicDNS** is on, and click
   **Enable HTTPS** under HTTPS Certificates. (The phone app only talks to a secure address.)
3. In PowerShell:

   ```powershell
   glasses start
   glasses phone
   ```

   `glasses phone` shares the login service with your own devices and prints an address like
   `https://my-pc.tail1234.ts.net`. If it prints a link, open it and approve, then run it again.
4. On the **iPhone**: install **Tailscale** from the App Store, sign in with the same account, and
   leave it switched on.
5. In the **VisionClaw app**: set **Gateway URL** to the address from step 3 (it's on the login
   screen, or in Settings if you're already logged in), and log in with your access code.

## Part 4: Keep it running

```powershell
glasses autostart on
```

- That starts everything when you sign in to Windows. To have it come back after a restart on its
  own, turn on automatic sign-in: press **Windows + R**, type `netplwiz`, untick *Users must enter a
  user name and password*, click OK.
- Stop the PC sleeping: **Settings → System → Power → Screen and sleep**, and set *When plugged in,
  put my device to sleep after* to **Never**. (The voice program also asks Windows to stay awake while
  it runs.)
- On the **Mac**, quit Terminal (⌘Q → Terminate). If both are running, each call goes to one of them at random, which is confusing.

## Part 5: Check it

```powershell
glasses doctor
```

It checks the login service, the voice program, your keys, your email accounts, whether the phone can
reach the PC, and runs a short live test. Anything wrong gets a ✗ and the fix.

## Everyday commands

| | |
| - | - |
| `glasses start` | start everything (add folders to change where Claude works: `glasses start $HOME\Documents $HOME\Downloads`; the first one is where it works, the rest it may also read and change) |
| `glasses stop` | stop everything |
| `glasses status` | what's running |
| `glasses logs` | the end of the voice log (`glasses logs gateway` for the login service) |
| `glasses pick-voice` | hear the ElevenLabs voices and pick one |
| `glasses livekit` | enter your LiveKit address, key and secret again (see below) |
| `glasses doctor` | check everything |

## If something goes wrong

- **`py` isn't recognized:** close and reopen PowerShell. If it still isn't, use `python` instead of
  `py`.
- **`winget` hangs or isn't recognized:** skip it and use the websites. `glasses setup` tells you which
  ones (Node.js from nodejs.org, Tailscale from tailscale.com/download/windows).
- **Claude Code can't find Git Bash:** install Git (`winget install --id Git.Git -e`), then open a
  new PowerShell window.
- **`glasses doctor` says Tailscale is sharing an address "but it doesn't answer":** it now says why.
  The first request makes Tailscale fetch a security certificate, which can take up to a minute, so
  run `glasses doctor --quick` again. If it says the certificate isn't ready, open
  login.tailscale.com/admin/dns and click **Enable HTTPS**. If it says the PC can't look up the name,
  check **MagicDNS** is on there and that **Use Tailscale DNS settings** is ticked in the Tailscale
  menu (the icon near the clock).
- **The phone says it can't connect:** `glasses doctor`. The "phone can reach this PC" line says
  whether Tailscale is signed in on the PC, whether the login service is shared, and whether it
  answers. Also check Tailscale is on, on the iPhone.
- **"The voice program didn't finish starting" and the log says `401`:** LiveKit refused the keys,
  usually because the secret didn't paste right. On cloud.livekit.io make a new key (Settings → API
  Keys → Create key; the secret is shown once, so use its Copy button), then run `glasses stop`,
  `glasses livekit`, `glasses start`. If pasting into the prompt does nothing, type the secret by hand;
  nothing shows while you type, that's normal. A real secret is about 44 characters, and the prompt
  tells you if what arrived is far shorter.
- **"Agent left the call" the moment a call starts:** `glasses logs` says why. If it says
  `The directory name is invalid` (or the folder doesn't exist), the folder Claude Code was told to
  work in isn't there. Windows often keeps the Desktop inside OneDrive. `glasses start` now looks for
  it there and falls back to your home folder; use a folder that exists, for example
  `glasses start $HOME\Documents`.
- **Calls connect but it never answers:** `glasses logs`, and `glasses doctor`.
- **Browser control doesn't work:** Chrome must be open, with the Claude extension signed in.
  Claude Code's Chrome control works on regular Windows, but **not** inside WSL, so don't run this
  there.
