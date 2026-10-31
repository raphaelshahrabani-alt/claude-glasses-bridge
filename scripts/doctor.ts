// Checks every piece between the glasses and Claude Code, then times real
// round trips through the bridge, including one that runs a command in the
// project. Run it while start-glasses is running:
//   bin/glasses-doctor            (or: bun run doctor)
//   bin/glasses-doctor --quick    connection checks only; nothing is sent to Claude
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { loadConfig } from '../src/config.ts'
import { median, parseBridgeLog, patchState, seconds, verdict } from '../src/doctor.ts'
import type { AskResult } from '../src/http.ts'

const config = loadConfig()
const base = process.env.GLASSES_URL ?? `http://${config.host}:${config.port}`
const visionclaw = process.env.VISIONCLAW_DIR ?? join(homedir(), 'VisionClaw')
const gatewayUrl = process.env.GATEWAY_URL ?? 'http://127.0.0.1:8788'
const quick = process.argv.includes('--quick')

let problems = 0
const ok = (msg: string) => console.log(`  ✓ ${msg}`)
const info = (msg: string) => console.log(`    ${msg}`)
function bad(msg: string, fix?: string) {
  problems++
  console.log(`  ✗ ${msg}`)
  if (fix) console.log(`      → ${fix}`)
}

async function getJson(url: string, ms = 2_000): Promise<any | null> {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(ms) })
    return res.ok ? await res.json() : null
  } catch {
    return null
  }
}

function sh(cmd: string[]): string {
  try {
    return Bun.spawnSync(cmd).stdout.toString().trim()
  } catch {
    return ''
  }
}

console.log('Connections')

const health = await getJson(`${base}/health`)
const bridgeUp = health !== null && health.claude_connected === true
if (health === null) {
  bad(
    `Claude Code with the glasses channel isn't running (nothing answers at ${base})`,
    'run start-glasses, and choose "I am using this for local development" in the Claude window',
  )
} else if (!health.claude_connected) {
  bad('the bridge is up but Claude Code has not finished connecting to it', 'check the Claude Code window, or restart it with start-glasses')
} else {
  ok(`Claude Code is connected to the bridge at ${base}`)
}

if ((await getJson(`${gatewayUrl}/health`)) === null) {
  bad(`VisionClaw's gateway isn't answering at ${gatewayUrl}`, 'start-glasses starts it')
} else {
  ok("VisionClaw's gateway is running")
}

const workerPid = sh(['pgrep', '-f', 'main.py start']).split('\n')[0]
if (!workerPid) {
  bad("VisionClaw's voice worker isn't running", 'start-glasses starts it')
} else {
  ok("VisionClaw's voice worker is running")
  // macOS ps -E shows the environment a process started with.
  const env = sh(['ps', '-E', '-ww', '-o', 'command=', '-p', workerPid])
  if (env.includes('PATH=')) {
    if (!env.includes('CLAUDE_CODE_URL=')) {
      bad('the worker was started without CLAUDE_CODE_URL, so it cannot reach Claude Code', 'restart it with start-glasses')
    } else if (!env.includes('CLAUDE_CODE_ONLY=1')) {
      bad(
        'the worker is not in Claude-Code-only mode, so the voice model answers most questions itself',
        'restart it with start-glasses, which sets CLAUDE_CODE_ONLY=1',
      )
    } else {
      ok('the worker sends questions to Claude Code (CLAUDE_CODE_ONLY=1)')
    }
  }
}

const mainPy = join(visionclaw, 'agent', 'main.py')
const patch = patchState(existsSync(mainPy) ? readFileSync(mainPy, 'utf8') : null)
const applyPatch = `cd ${visionclaw} && git checkout agent/main.py samples/CameraAccess/CameraAccess/OpenClaw/LiveKitSession.swift samples/CameraAccess/CameraAccess/Views/StreamSessionView.swift samples/CameraAccess/CameraAccess/Settings/SettingsManager.swift && git apply ~/claude-glasses-bridge/integrations/visionclaw/claude-code.patch, then restart the worker (rebuild the iPhone app too if the patch's Swift files changed)`
if (patch === 'current') ok('VisionClaw has the current Claude Code patch')
else if (patch === 'old') bad('VisionClaw has an older version of the Claude Code patch', applyPatch)
else if (patch === 'unpatched') bad('VisionClaw is not patched to talk to Claude Code', applyPatch)
else info(`(no VisionClaw checkout at ${visionclaw}; set VISIONCLAW_DIR to check it)`)

console.log('\nRecent questions (from bridge.log)')
const logFile = join(config.dataDir, 'bridge.log')
const trips = existsSync(logFile) ? parseBridgeLog(readFileSync(logFile, 'utf8')).slice(-8) : []
if (trips.length === 0) {
  info('none yet')
} else {
  for (const t of trips) {
    const when = new Date(t.at).toLocaleTimeString()
    const took = t.ms === undefined ? 'no reply' : `${seconds(t.ms)} (${verdict(t.ms)})`
    info(`${when}  ${took.padEnd(18)} ${JSON.stringify(t.text.slice(0, 60))}`)
  }
  const answered = trips.flatMap(t => (t.ms === undefined ? [] : [t.ms]))
  const mid = median(answered)
  if (mid !== undefined) info(`median Claude Code time: ${seconds(mid)}`)
  const unanswered = trips.filter(t => t.ms === undefined).length
  if (unanswered) bad(`${unanswered} of the last ${trips.length} questions never got a reply`, 'check the Claude Code window for a permission prompt or an error')
}

async function ask(text: string): Promise<{ result: AskResult | null; ms: number }> {
  const started = Date.now()
  try {
    const res = await fetch(`${base}/message`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${config.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
      signal: AbortSignal.timeout(config.waitMs + 5_000),
    })
    return { result: res.ok ? ((await res.json()) as AskResult) : null, ms: Date.now() - started }
  } catch {
    return { result: null, ms: Date.now() - started }
  }
}

function report(label: string, { result, ms }: { result: AskResult | null; ms: number }) {
  if (result?.status === 'replied') {
    const v = verdict(ms)
    if (v === 'good') ok(`${label}: ${seconds(ms)}`)
    else bad(`${label}: ${seconds(ms)} (${v})`, 'see "Faster answers" in the README')
    info(`Claude said: ${JSON.stringify(result.reply)}`)
  } else {
    bad(
      `${label}: no reply after ${seconds(ms)}`,
      'look at the Claude Code window: it may be waiting on a permission prompt or still busy with an earlier request',
    )
  }
}

if (!quick && bridgeUp) {
  console.log('\nLive tests (these show up in your Claude Code session)')
  report('simple question', await ask('Health check from glasses-doctor: reply with just the word OK.'))
  report(
    'coding check',
    await ask(
      'Health check from glasses-doctor: run git status in your working folder, then reply in one sentence with the folder name, the current branch and how many files have uncommitted changes.',
    ),
  )
  info('On a call, add 2 to 4 seconds for the voice model to hear you and speak the answer.')
}

console.log(problems === 0 ? '\nEverything is connected.' : `\n${problems} problem${problems === 1 ? '' : 's'} found.`)
process.exit(problems === 0 ? 0 : 1)
