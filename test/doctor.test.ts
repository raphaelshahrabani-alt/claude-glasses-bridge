import { afterAll, beforeAll, describe, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { median, parseBridgeLog, patchState, verdict } from '../src/doctor.ts'

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

  test('median and verdict', () => {
    expect(median([])).toBeUndefined()
    expect(median([3, 1, 2])).toBe(2)
    expect(median([4, 1, 2, 3])).toBe(2.5)
    expect(verdict(5_000)).toBe('good')
    expect(verdict(12_000)).toBe('slow')
    expect(verdict(30_000)).toBe('very slow')
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
