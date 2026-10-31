import { afterAll, beforeAll, describe, expect, test } from 'bun:test'
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { claudeLaunch, credentials, elapsedSeconds, firstWordsVerdict, parseVoiceLog, median, parseBridgeLog, patchState, verdict } from '../src/doctor.ts'

const root = join(import.meta.dir, '..')

describe('parseBridgeLog', () => {
  test('pairs questions with their first reply', () => {
    const log = [
      '2026-10-30T21:32:36.040Z [19556] starting: pid 19556, bun 1.4.4, started by pid 1 (claude)',
      '2026-10-30T21:32:46.705Z [19556] chat d36a7db1: "what\'s the latest commit?"',
      '2026-10-30T21:32:53.703Z [19556] reply d36a7db1 (delivered): "The latest commit is e1cda32."',
      '2026-10-30T21:33:00.000Z [19556] chat 0000beef: "read my screen" +image',
      '2026-10-30T21:35:10.000Z [19556] reply 0000beef (queued for later): "It is a stack trace."',
      '2026-10-30T21:35:11.000Z [19556] reply 0000beef (delivered): "a second reply"',
      '2026-10-30T21:36:00.000Z [19556] chat 12345678: "run the tests"',
      '2026-10-30T21:36:01.000Z [19556] permission kqrtx: allow',
      'garbage line',
    ].join('\n')
    expect(parseBridgeLog(log)).toEqual([
      { chat_id: 'd36a7db1', text: "what's the latest commit?", at: Date.parse('2026-10-30T21:32:46.705Z'), ms: 6998, delivered: true },
      { chat_id: '0000beef', text: 'read my screen', at: Date.parse('2026-10-30T21:33:00.000Z'), ms: 130000, delivered: false },
      { chat_id: '12345678', text: 'run the tests', at: Date.parse('2026-10-30T21:36:00.000Z') },
    ])
  })
})

describe('helpers', () => {
  test('patchState tells the patch versions apart', () => {
    expect(patchState(null)).toBe('missing')
    expect(patchState('async def execute(')).toBe('unpatched')
    expect(patchState('async def claude_code(ctx')).toBe('old')
    expect(patchState('CLAUDE_CODE_VOICE_INSTRUCTIONS = ...\nasync def claude_code(ctx')).toBe('old')
    expect(patchState('async def _look(ctx): ...\nasync def claude_code(ctx')).toBe('old')
    expect(patchState('async def _relay_claude_code_followups(s, u): ...\nasync def claude_code(ctx')).toBe('old')
    expect(patchState('def _with_user_words(t, h): ...\nasync def claude_code(ctx')).toBe('old')
    expect(patchState('CLAUDE_CODE_PATCH_VERSION = 4\nasync def claude_code(ctx')).toBe('old')
    expect(patchState('CLAUDE_CODE_PATCH_VERSION = 5\nasync def claude_code(ctx')).toBe('current')
  })

  test('elapsedSeconds reads ps etime', () => {
    expect(elapsedSeconds('  05:12')).toBe(312)
    expect(elapsedSeconds('1:02:03')).toBe(3723)
    expect(elapsedSeconds('2-01:02:03')).toBe(2 * 86400 + 3723)
    expect(elapsedSeconds('')).toBeNull()
  })

  test('claudeLaunch reads the start-glasses flags', () => {
    const cmd = 'claude --dangerously-load-development-channels server:glasses --model sonnet --effort low --permission-mode auto --add-dir /Users/r/Downloads --append-system-prompt-file /Users/r/.claude-glasses-bridge/persona.md'
    expect(claudeLaunch(cmd)).toEqual({
      model: 'sonnet',
      effort: 'low',
      permissionMode: 'auto',
      persona: '/Users/r/.claude-glasses-bridge/persona.md',
      addDirs: ['/Users/r/Downloads'],
    })
    expect(claudeLaunch('claude --dangerously-load-development-channels server:glasses')).toEqual({ addDirs: [] } as any)
  })

  test('median and verdict', () => {
    expect(median([])).toBeUndefined()
    expect(median([3, 1, 2])).toBe(2)
    expect(median([4, 1, 2, 3])).toBe(2.5)
    expect(verdict(5_000)).toBe('good')
    expect(verdict(12_000)).toBe('slow')
    expect(verdict(30_000)).toBe('very slow')
  })
})

describe('parseVoiceLog', () => {
  test('collects timings, errors and connector problems', () => {
    const log = [
      '2026-10-31 14:00:01,000 [10] call started in room room-1',
      '2026-10-31 14:00:04,000 [10] quick voice: first words after 0.9s',
      '2026-10-31 14:00:05,100 [10] line 1: first words after 2.1s',
      '2026-10-31 14:00:05,300 [10] line 1: done after 2.4s, 0 tool call(s)',
      '2026-10-31 14:01:00,000 [10] line 1: connectors not connected: plugin:small-business:gmail, plugin:small-business:hubspot',
      '2026-10-31 14:01:05,100 [10] line 1: first words after 3.9s',
      '2026-10-31 14:01:12,300 [10] line 1: done after 11.0s, 3 tool call(s)',
      '2026-10-31 14:02:00,000 [10] voice error (tts): 429 Too Many Requests',
      '2026-10-31 14:02:00,001 [10] LiveKit Inference may be out of free credits. Put a Deepgram key in ...',
      '2026-10-31 14:03:00,000 [10] line 2: claude exited with code 1',
      '2026-10-31 14:03:01,000 [10] line 1: claude exited with code 0',
      'not a log line',
    ].join('\n')
    const stats = parseVoiceLog(log)
    expect(stats.firstWords).toEqual([2.1, 3.9])
    expect(stats.quick).toEqual([0.9])
    expect(stats.done).toEqual([{ s: 2.4, tools: 0 }, { s: 11, tools: 3 }])
    expect(stats.calls).toBe(1)
    expect(stats.lastCall).toBe('2026-10-31 14:00:01,000')
    expect(stats.errors).toEqual(['2026-10-31 14:02:00,000  tts: 429 Too Many Requests'])
    expect(stats.creditsOut).toBe(true)
    expect(stats.crashes).toBe(1)
    expect(stats.notConnected).toEqual(['plugin:small-business:gmail', 'plugin:small-business:hubspot'])
    expect(firstWordsVerdict(2.1)).toBe('good')
    expect(firstWordsVerdict(4)).toBe('slow')
    expect(firstWordsVerdict(9)).toBe('very slow')
  })

  test('remembers an API error until an answer succeeds', () => {
    const failing = [
      '2026-10-31 15:00:00,000 [10] line 1: Claude API error (billing_error): Credit balance is too low',
      '2026-10-31 15:00:00,100 [10] line 1: done after 0.9s, 0 tool call(s), failed: success',
    ]
    expect(parseVoiceLog(failing.join('\n')).apiError).toBe('billing_error')
    const fixed = [...failing, '2026-10-31 15:05:00,000 [10] line 1: done after 2.0s, 0 tool call(s)']
    expect(parseVoiceLog(fixed.join('\n')).apiError).toBeUndefined()
  })

  test('credentials', () => {
    expect(credentials('none')).toEqual({ ok: true, say: 'Claude Code uses your Claude subscription login' })
    expect(credentials('ANTHROPIC_API_KEY').ok).toBe(false)
    expect(credentials('/login managed key').fix).toContain('/logout')
  })
})

describe('glasses-doctor end to end', () => {
  const dataDir = mkdtempSync(join(tmpdir(), 'doctor-'))
  const visionclaw = join(dataDir, 'VisionClaw')
  const env = {
    PATH: process.env.PATH ?? '',
    HOME: dataDir,
    GLASSES_TOKEN: 'doctor-token-0123456789',
    GLASSES_PORT: '18796',
    GLASSES_DATA_DIR: dataDir,
    GLASSES_WAIT_S: '5',
    GLASSES_LISTEN: 'always',
    GLASSES_VOICE: 'gemini',
    // a real voice worker on this machine is none of this test's business
    GLASSES_VOICE_MATCH: 'no-such-voice-worker-for-doctor-test',
  }
  const claude = new Client({ name: 'fake-claude-code', version: '0' })
  let gateway: ReturnType<typeof Bun.serve>
  let worker: ReturnType<typeof Bun.spawn>

  beforeAll(async () => {
    mkdirSync(join(visionclaw, 'agent'), { recursive: true })
    writeFileSync(join(visionclaw, 'agent', 'main.py'), 'CLAUDE_CODE_PATCH_VERSION = 5\nasync def claude_code(ctx): ...\n')
    gateway = Bun.serve({ port: 18797, hostname: '127.0.0.1', fetch: () => Response.json({ ok: true }) })
    // something pgrep -f "main.py start" will find
    worker = Bun.spawn(['bash', '-c', 'exec -a "python main.py start" sleep 30'])
    // a Claude Code stand-in that answers every message
    claude.fallbackNotificationHandler = async (n: any) => {
      if (n.method !== 'notifications/claude/channel') return
      const { content, meta } = n.params
      const text = content.includes('git status') ? 'You are in my-app on main with 2 uncommitted files.' : 'OK'
      setTimeout(() => claude.callTool({ name: 'reply', arguments: { chat_id: meta.chat_id, text } }).catch(() => {}), 50)
    }
    await claude.connect(
      new StdioClientTransport({ command: process.execPath, args: [join(root, 'src', 'server.ts')], env, stderr: 'ignore' }),
    )
    for (let i = 0; i < 50; i++) {
      try {
        if ((await fetch('http://127.0.0.1:18796/health')).ok) break
      } catch {}
      await Bun.sleep(50)
    }
  })

  afterAll(async () => {
    await claude.close()
    gateway.stop(true)
    worker.kill()
  })

  // Not spawnSync: the stand-in gateway and Claude live in this process and
  // must keep answering while the doctor runs.
  async function doctor(args: string[] = [], extraEnv: Record<string, string> = {}) {
    const proc = Bun.spawn([process.execPath, join(root, 'scripts', 'doctor.ts'), ...args], {
      env: { ...env, VISIONCLAW_DIR: visionclaw, GATEWAY_URL: 'http://127.0.0.1:18797', ...extraEnv },
      stdout: 'pipe',
    })
    const out = await new Response(proc.stdout).text()
    return { code: await proc.exited, out }
  }

  test('checks every connection and times two live round trips', async () => {
    const { code, out } = await doctor()
    expect(out).toContain('✓ Claude Code is connected to the bridge at http://127.0.0.1:18796')
    expect(out).toContain("✓ VisionClaw's gateway is running")
    expect(out).toContain("✓ VisionClaw's voice worker is running")
    expect(out).toContain('✓ the voice worker is running the current code')
    expect(out).toContain('✓ VisionClaw has the current Claude Code patch')
    expect(out).toMatch(/✓ simple question: \d+\.\ds/)
    expect(out).toContain('Claude said: "OK"')
    expect(out).toMatch(/✓ coding check: \d+\.\ds/)
    expect(out).toContain('Claude said: "You are in my-app on main with 2 uncommitted files."')
    expect(out).toContain('Everything is connected.')
    expect(code).toBe(0)
  })

  test('the next run lists those questions with their times', async () => {
    const { out } = await doctor(['--quick'])
    expect(out).toContain('Health check from glasses-doctor: reply with just the word OK.'.slice(0, 60))
    expect(out).toMatch(/median Claude Code time: \d+\.\ds/)
    expect(out).not.toContain('Live tests')
  })

  test('spots a worker that started before its code changed', async () => {
    await Bun.sleep(2100)
    writeFileSync(join(visionclaw, 'agent', 'main.py'), 'CLAUDE_CODE_PATCH_VERSION = 5\nasync def claude_code(ctx): ...\n')
    const { out } = await doctor(['--quick'])
    expect(out).toContain('✗ the voice worker is still running code from before your last update')
    expect(out).toContain('run start-glasses again')
  })

  test('says what to run when nothing is up', async () => {
    const { code, out } = await doctor(['--quick'], {
      GLASSES_PORT: '18798',
      VISIONCLAW_DIR: join(dataDir, 'nowhere'),
      GATEWAY_URL: 'http://127.0.0.1:18799',
    })
    expect(code).toBe(1)
    expect(out).toContain("✗ Claude Code with the glasses channel isn't running")
    expect(out).toContain('run start-glasses')
    expect(out).toContain("✗ VisionClaw's gateway isn't answering")
  })
})

describe('glasses-doctor with the direct voice worker', () => {
  const dataDir = mkdtempSync(join(tmpdir(), 'doctor-direct-'))
  const visionclaw = join(dataDir, 'VisionClaw')
  const python = join(dataDir, 'fake-python')
  let gateway: ReturnType<typeof Bun.serve>
  let worker: ReturnType<typeof Bun.spawn>
  const today = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const day = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`

  beforeAll(() => {
    mkdirSync(join(visionclaw, 'gateway'), { recursive: true })
    writeFileSync(join(visionclaw, 'gateway', '.env'), 'LIVEKIT_URL=wss://x.livekit.cloud\nLIVEKIT_API_KEY=k\nLIVEKIT_API_SECRET=s\n')
    mkdirSync(join(dataDir, 'calls'))
    writeFileSync(join(dataDir, 'calls', `${day}.md`), '\n## Call at 14:00\n\n14:00:02  You: check my email\n14:00:04  Claude: Checking your inbox.\n')
    writeFileSync(join(dataDir, 'voice.log'), [
      '2026-10-31 14:00:01,000 [10] call started in room room-1',
      '2026-10-31 14:00:03,000 [10] quick voice: first words after 1.0s',
      '2026-10-31 14:00:04,100 [10] line 1: first words after 4.2s',
      '2026-10-31 14:00:09,300 [10] line 1: done after 7.4s, 2 tool call(s)',
    ].join('\n'))
    // stands in for the voice worker's Python running voice/selftest.py
    writeFileSync(python, `#!/bin/bash
echo '{"test": "quick voice startup", "ok": true, "total_s": 0.7, "model": "haiku", "effort": "low", "cwd": "/Users/me/Desktop"}'
echo '{"test": "quick answer", "ok": true, "first_s": 1.1, "total_s": 1.2, "reply": "OK", "tools": [], "credentials": "none"}'
echo '{"test": "hand-off", "ok": true, "first_s": 1.2, "total_s": 1.4, "reply": "[work] Sure, one sec.", "tools": [], "handed_off": true}'
echo '{"test": "work model startup", "ok": true, "total_s": 0.8, "model": "opus", "effort": "medium", "cwd": "/Users/me/Desktop"}'
echo '{"test": "simple question", "ok": true, "first_s": 4.4, "total_s": 4.5, "reply": "OK", "tools": [], "credentials": "none", "connectors": {"plugin:small-business:gmail": "connected", "plugin:small-business:google-calendar": "needs-auth", "plugin:small-business:hubspot": "failed"}}'
echo '{"test": "email check", "ok": true, "first_s": 3.6, "total_s": 7.1, "reply": "Three.", "tools": ["mcp__gmail__search_threads"]}'
`)
    chmodSync(python, 0o755)
    gateway = Bun.serve({ port: 18795, hostname: '127.0.0.1', fetch: () => Response.json({ ok: true }) })
    worker = Bun.spawn(['bash', '-c', 'exec -a "python doctor-test-voice-worker" sleep 30'])
  })

  afterAll(() => {
    gateway.stop(true)
    worker.kill()
  })

  async function doctor(args: string[] = []) {
    const proc = Bun.spawn([process.execPath, join(root, 'scripts', 'doctor.ts'), ...args], {
      env: {
        PATH: process.env.PATH ?? '',
        HOME: dataDir,
        GLASSES_TOKEN: 'doctor-token-0123456789',
        GLASSES_DATA_DIR: dataDir,
        GLASSES_VOICE: 'direct',
        GLASSES_VOICE_MATCH: 'doctor-test-voice-worker',
        GLASSES_VOICE_PYTHON: python,
        VISIONCLAW_DIR: visionclaw,
        GATEWAY_URL: 'http://127.0.0.1:18795',
      },
      stdout: 'pipe',
    })
    const out = await new Response(proc.stdout).text()
    return { code: await proc.exited, out }
  }

  test('checks the worker, recent calls and times Claude Code', async () => {
    const { code, out } = await doctor()
    expect(out).toContain('direct voice: you talk straight to Claude Code')
    expect(out).toContain("✓ VisionClaw's gateway is running")
    expect(out).toContain('✓ the voice worker is running the current code')
    expect(out).toContain("✓ the LiveKit keys are in VisionClaw's gateway/.env")
    expect(out).toContain('(no profile yet')
    expect(out).toContain('✓ the quick voice answered after 1.0s (median of the last 1)')
    expect(out).toContain('✓ the work model started talking after 4.2s (median of the last 1)')
    expect(out).toContain('work requests were done after 7.4s on median; 1 of 1 used tools')
    expect(out).toContain('14:00:04  Claude: Checking your inbox.')
    expect(out).toContain('✓ quick voice: Claude Code started in 0.7s (haiku, effort low, in /Users/me/Desktop)')
    expect(out).toContain('✓ quick answer: first words after 1.1s, done in 1.2s')
    expect(out).toContain('✓ hand-off: the quick voice passed an email question to the work model, saying "Sure, one sec." after 1.2s')
    expect(out).toContain('✓ work model: Claude Code started in 0.8s (opus, effort medium, in /Users/me/Desktop)')
    expect(out).toContain('✓ simple question: first words after 4.4s, done in 4.5s')
    expect(out).toContain('✓ Claude Code uses your Claude subscription login')
    expect(out).toContain('✓ plugin:small-business:gmail is connected')
    expect(out).toContain('✗ plugin:small-business:google-calendar is needs-auth')
    expect(out).not.toContain('hubspot')
    expect(out).toContain('✓ email check: first words after 3.6s, done in 7.1s')
    expect(out).toContain('Claude said: "Three."')
    expect(out).toContain('1 problem found.')
    expect(code).toBe(1)
  })

  test('--quick skips the live test', async () => {
    const { out } = await doctor(['--quick'])
    expect(out).not.toContain('Live test')
    expect(out).toContain('Everything is connected.')
  })
})
