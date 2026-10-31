// Checks every piece between the glasses and Claude Code, then times Claude
// Code the way the voice worker runs it. Run it while start-glasses is running:
//   bin/glasses-doctor            (or: bun run doctor)
//   bin/glasses-doctor --quick    connection checks only; nothing is sent to Claude
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { loadConfig } from '../src/config.ts'
import {
  claudeLaunch,
  elapsedSeconds,
  firstWordsVerdict,
  median,
  parseBridgeLog,
  parseVoiceLog,
  patchState,
  seconds,
  verdict,
} from '../src/doctor.ts'
import type { AskResult } from '../src/http.ts'

const config = loadConfig()
const root = join(import.meta.dir, '..')
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

/** When a process started, in ms since the epoch, or null. */
function startedAt(pid: string): number | null {
  const secs = elapsedSeconds(sh(['ps', '-o', 'etime=', '-p', pid]))
  return secs === null ? null : Date.now() - secs * 1000
}

function newestChange(files: string[]): number {
  return Math.max(0, ...files.filter(f => existsSync(f)).map(f => statSync(f).mtimeMs))
}

const restart = 'quit Terminal (⌘Q, then Terminate) and run start-glasses again'
const directPid = sh(['pgrep', '-f', process.env.GLASSES_VOICE_MATCH ?? 'voice/worker.py start']).split('\n')[0]
const geminiPid = sh(['pgrep', '-f', 'main.py start']).split('\n')[0]
const mode = process.env.GLASSES_VOICE ?? (directPid ? 'direct' : geminiPid ? 'gemini' : 'direct')

console.log(`Connections (${mode === 'direct' ? 'direct voice: you talk straight to Claude Code' : 'Gemini voice with the glasses channel'})`)

if ((await getJson(`${gatewayUrl}/health`)) === null) {
  bad(`VisionClaw's gateway isn't answering at ${gatewayUrl}`, 'start-glasses starts it')
} else {
  ok("VisionClaw's gateway is running")
}

if (mode === 'direct') await checkDirect()
else await checkGemini()

console.log(problems === 0 ? '\nEverything is connected.' : `\n${problems} problem${problems === 1 ? '' : 's'} found.`)
process.exit(problems === 0 ? 0 : 1)

async function checkDirect() {
  if (!directPid) {
    bad("the voice worker isn't running", 'run start-glasses, which opens a "glasses: voice" window')
  } else {
    ok('the voice worker is running')
    const voiceDir = join(root, 'voice')
    const code = readdirSync(voiceDir).filter(f => f.endsWith('.py') || f.endsWith('.md')).map(f => join(voiceDir, f))
    const started = startedAt(directPid)
    if (started !== null && started < newestChange(code) - 1000) {
      bad('the voice worker is still running code from before your last update', restart)
    } else if (started !== null) {
      ok('the voice worker is running the current code')
    }
  }
  if (geminiPid) {
    bad("VisionClaw's Gemini worker is running too, so two voices may answer", 'pkill -f "main.py start"')
  }

  const gatewayEnv = join(visionclaw, 'gateway', '.env')
  const envText = existsSync(gatewayEnv) ? readFileSync(gatewayEnv, 'utf8') : ''
  const missing = ['LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET'].filter(k => !new RegExp(`^${k}=\\S`, 'm').test(envText))
  if (missing.length) bad(`${gatewayEnv} is missing ${missing.join(', ')}`, 'see integrations/visionclaw/IPHONE.md, step 2')
  else ok('the LiveKit keys are in VisionClaw\'s gateway/.env')

  const python = process.env.GLASSES_VOICE_PYTHON ?? join(config.dataDir, 'voice-venv', 'bin', 'python')
  if (!existsSync(python)) bad('the voice worker is not set up yet', 'run ~/claude-glasses-bridge/bin/glasses-voice --setup')

  const persona = process.env.GLASSES_PERSONA ?? join(config.dataDir, 'persona.md')
  if (existsSync(persona)) ok(`your profile is loaded on every call (${persona})`)
  else info(`(no profile yet: copy templates/business-partner.md to ${persona} and fill it in)`)

  console.log('\nRecent calls (from voice.log)')
  const logFile = join(config.dataDir, 'voice.log')
  const stats = parseVoiceLog(existsSync(logFile) ? readFileSync(logFile, 'utf8') : '')
  if (stats.calls) info(`${stats.calls} call${stats.calls === 1 ? '' : 's'} in the log, the last at ${stats.lastCall}`)
  const first = stats.firstWords.slice(-10)
  const mid = median(first)
  if (mid === undefined) {
    info('no answers yet')
  } else {
    const v = firstWordsVerdict(mid)
    const line = `first words after ${mid.toFixed(1)}s (median of the last ${first.length} answer${first.length === 1 ? '' : 's'})`
    if (v === 'good') ok(line)
    else bad(`${line}: ${v}`, 'see "Faster answers" in the README')
    const done = stats.done.slice(-10)
    const doneMid = median(done.map(d => d.s))
    if (doneMid !== undefined) {
      info(`requests were done after ${doneMid.toFixed(1)}s on median; ${done.filter(d => d.tools).length} of ${done.length} used tools`)
    }
  }
  if (stats.creditsOut) {
    bad('LiveKit Inference ran out of free credits, so speech-to-text or the voice stopped', 'add a Deepgram key: see "Direct voice" in the README')
  }
  if (stats.crashes) {
    bad(`Claude Code quit unexpectedly ${stats.crashes} time${stats.crashes === 1 ? '' : 's'}`, `read the "glasses: voice" window or ${logFile}`)
  }
  for (const e of stats.errors.slice(-3)) info(`voice error: ${e}`)
  for (const name of ['gmail', 'calendar']) {
    const down = stats.notConnected.find(n => n.toLowerCase().includes(name))
    if (down) bad(`${down} was not connected the last time Claude Code started`, 'run claude in Terminal, type /mcp, choose it and log in')
  }
  const today = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const callLog = join(config.dataDir, 'calls', `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}.md`)
  if (existsSync(callLog)) {
    info(`today's calls are written down in ${callLog}; the last lines:`)
    for (const l of readFileSync(callLog, 'utf8').trim().split('\n').slice(-4)) info(`  ${l.slice(0, 110)}`)
  } else {
    info(`calls are written down in ${join(config.dataDir, 'calls')}/`)
  }

  if (quick || !existsSync(python)) return
  console.log('\nLive test (Claude Code, run exactly as the voice worker runs it)')
  const proc = Bun.spawn([python, join(root, 'voice', 'selftest.py')], { stdout: 'pipe', stderr: 'ignore' })
  const timer = setTimeout(() => proc.kill(), 300_000)
  const out = await new Response(proc.stdout).text()
  clearTimeout(timer)
  const rows = out.split('\n').flatMap(l => {
    try {
      return [JSON.parse(l)]
    } catch {
      return []
    }
  })
  if (rows.length === 0) bad('Claude Code did not start', 'run claude in Terminal and check that you are logged in')
  for (const row of rows) {
    if (row.test === 'startup') {
      if (row.ok) ok(`Claude Code started in ${row.total_s}s (${row.model}, effort ${row.effort}, in ${row.cwd})`)
      else bad('Claude Code quit right after starting', 'run claude in Terminal and check that you are logged in')
      continue
    }
    if (row.ok && row.first_s != null) {
      const v = firstWordsVerdict(row.first_s)
      const line = `${row.test}: first words after ${row.first_s}s, done in ${row.total_s}s`
      if (v === 'good') ok(line)
      else bad(`${line} (${v})`, 'see "Faster answers" in the README')
    } else {
      bad(`${row.test}: ${row.error ?? 'failed'}`, `read ${logFile}`)
    }
    if (row.reply) info(`Claude said: ${JSON.stringify(row.reply)}`)
    for (const [name, status] of Object.entries((row.connectors ?? {}) as Record<string, string>)) {
      if (!/gmail|calendar/i.test(name)) continue
      if (status === 'connected') ok(`${name} is connected`)
      else bad(`${name} is ${status}`, 'run claude in Terminal, type /mcp, choose it and log in')
    }
  }
  info('On a call, add about a second for speech-to-text to notice you stopped talking.')
}

async function checkGemini() {
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
    if (health.permission_relay) ok('permission prompts are read out on the glasses, to approve by voice')
    else info('(voice approvals are off: a permission prompt waits on the Mac until someone answers it there)')
    checkClaudeSession()
  }

  /** The Claude Code session that started this bridge: is it on the current code, and how was it launched? */
  function checkClaudeSession() {
    for (const pid of sh(['pgrep', '-f', join(root, 'src', 'server.ts')]).split('\n').filter(Boolean)) {
      const parent = sh(['ps', '-o', 'ppid=', '-p', pid])
      const command = parent ? sh(['ps', '-ww', '-o', 'command=', '-p', parent]) : ''
      if (!/--(dangerously-load-development-)?channels\b/.test(command)) continue
      const started = startedAt(pid)
      const src = join(root, 'src')
      const changed = Math.max(...readdirSync(src).map(f => statSync(join(src, f)).mtimeMs))
      if (started !== null && started < changed - 1000) {
        bad('Claude Code was started before the bridge was updated, so it still has the old instructions', restart)
      }
      const launch = claudeLaunch(command)
      if (launch.model) ok(`Claude Code runs on ${launch.model}, effort ${launch.effort ?? 'default'}, ${launch.permissionMode ?? 'default'} permissions`)
      else info('(Claude Code was not started by start-glasses: default model and effort)')
      const persona = join(config.dataDir, 'persona.md')
      if (launch.persona) ok(`your profile is loaded (${launch.persona})`)
      else if (existsSync(persona)) bad('your profile (persona.md) exists but this session did not load it', restart)
      if (launch.addDirs.length) info(`(also has access to ${launch.addDirs.join(', ')})`)
      return
    }
  }

  if (directPid) {
    bad('the direct voice worker is running too, so two voices may answer', 'pkill -f "voice/worker.py start"')
  }

  const workerPid = geminiPid
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
  if (workerPid && existsSync(mainPy)) {
    const started = startedAt(workerPid)
    if (started !== null && started < statSync(mainPy).mtimeMs - 1000) {
      bad("the voice worker is still running code from before your last update", restart)
    } else if (started !== null) {
      ok('the voice worker is running the current code')
    }
  }
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
}
