// Runs bin/start-glasses with stand-ins for osascript, lsof and pgrep, so the
// windows it would open are recorded instead of opened.
import { describe, expect, test } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const script = join(import.meta.dir, '..', 'bin', 'start-glasses')

function stub(dir: string, name: string, body: string) {
  writeFileSync(join(dir, name), `#!/bin/bash\n${body}\n`)
  chmodSync(join(dir, name), 0o755)
}

function setup(opts: { listening?: number[]; workerRunning?: boolean; withStartScript?: boolean } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'start-glasses-'))
  const bin = join(root, 'bin')
  const visionclaw = join(root, 'VisionClaw')
  const project = join(root, 'my project')
  mkdirSync(bin)
  mkdirSync(join(visionclaw, 'gateway'), { recursive: true })
  mkdirSync(join(visionclaw, 'agent'), { recursive: true })
  mkdirSync(project)
  if (opts.withStartScript !== false) stub(join(visionclaw, 'agent'), 'start.sh', 'exit 0')
  const log = join(root, 'windows.log')
  stub(bin, 'osascript', `cat >/dev/null; printf '%s\\t%s\\n' "$2" "$3" >> '${log}'`)
  stub(bin, 'lsof', `for a in "$@"; do case "$a" in ${(opts.listening ?? []).map(p => `-iTCP:${p}`).join('|') || '__none__'}) exit 0;; esac; done; exit 1`)
  stub(bin, 'pgrep', `exit ${opts.workerRunning ? 0 : 1}`)
  stub(bin, 'claude', 'exit 0')
  stub(bin, 'pkill', `printf '%s\\n' "$*" >> '${root}/pkill.log'`)
  return { root, visionclaw, project, log, bin }
}

function run(env: ReturnType<typeof setup>, extraEnv: Record<string, string> = {}, extraArgs: string[] = []) {
  const proc = Bun.spawnSync(['bash', script, env.project, ...extraArgs], {
    env: { PATH: `${env.bin}:/usr/bin:/bin`, HOME: env.root, VISIONCLAW_DIR: env.visionclaw, ...extraEnv },
  })
  const windows = existsSync(env.log)
    ? readFileSync(env.log, 'utf8').trim().split('\n').map(line => line.split('\t'))
    : []
  return { code: proc.exitCode, out: proc.stdout.toString(), err: proc.stderr.toString(), windows }
}

const voiceScript = join(import.meta.dir, '..', 'bin', 'glasses-voice')

describe('start-glasses (direct voice, the default)', () => {
  test('opens the gateway and the voice worker, with Claude Code inside it', () => {
    const env = setup({ withStartScript: false })
    const { code, windows, out } = run(env)
    expect(code).toBe(0)
    expect(windows.map(w => w[0])).toEqual(['glasses: gateway', 'glasses: voice'])
    expect(windows[0][1]).toBe(`cd ${env.visionclaw}/gateway && npm run dev`)
    // the space in the project path is escaped, not split
    expect(windows[1][1]).toBe(
      `GLASSES_PROJECT=${env.root}/my\\ project GLASSES_MODEL=sonnet GLASSES_EFFORT=low GLASSES_PERMISSION_MODE=auto caffeinate -dims ${voiceScript}`,
    )
    expect(out).toContain('you talk straight to Claude Code')
    expect(out).toContain(`calls are written down in ${env.root}/.claude-glasses-bridge/calls/`)
  })

  test('extra folders and settings reach the voice worker', () => {
    const env = setup({ listening: [8788] })
    mkdirSync(join(env.root, 'poker-tracker'))
    mkdirSync(join(env.root, 'ugc automation'))
    const { windows, out } = run(env, { GLASSES_MODEL: 'haiku' }, [join(env.root, 'poker-tracker'), join(env.root, 'ugc automation')])
    expect(windows).toHaveLength(1)
    expect(windows[0][1]).toContain('GLASSES_MODEL=haiku ')
    expect(windows[0][1]).toContain(`GLASSES_ADD_DIRS=${env.root}/poker-tracker:${env.root}/ugc\\ automation `)
    expect(out).toContain(`also has access to ${env.root}/ugc automation`)
  })

  test('mentions the profile when there is one', () => {
    const env = setup({ listening: [8788] })
    mkdirSync(join(env.root, '.claude-glasses-bridge'))
    writeFileSync(join(env.root, '.claude-glasses-bridge', 'persona.md'), '# Who you are to me\n')
    expect(run(env).out).toContain('with your profile from')
  })

  test('leaves a running voice worker alone', () => {
    const env = setup({ listening: [8788], workerRunning: true })
    const { code, windows, out } = run(env)
    expect(code).toBe(0)
    expect(windows).toEqual([])
    expect(out).toContain('gateway: already running')
    expect(out).toContain('voice:   already running')
  })
})

describe('start-glasses (GLASSES_VOICE=gemini)', () => {
  const gemini = { GLASSES_VOICE: 'gemini' }

  test('opens the gateway, worker and Claude Code windows', () => {
    const env = setup()
    const { code, windows, out } = run(env, gemini)
    expect(code).toBe(0)
    expect(windows.map(w => w[0])).toEqual(['glasses: gateway', 'glasses: worker', 'glasses: claude'])
    expect(windows[0][1]).toBe(`cd ${env.visionclaw}/gateway && npm run dev`)
    expect(windows[1][1]).toBe(`CLAUDE_CODE_ONLY=1 caffeinate -dims ${env.visionclaw}/agent/start.sh`)
    expect(windows[2][1]).toBe(
      `cd ${env.root}/my\\ project && claude --dangerously-load-development-channels server:glasses --model sonnet --effort low --permission-mode auto`,
    )
    expect(out).toContain('I am using this for local development')
  })

  test('leaves running pieces alone', () => {
    const env = setup({ listening: [8788, 8790], workerRunning: true })
    const { code, windows, out } = run(env, gemini)
    expect(code).toBe(0)
    expect(windows).toEqual([])
    expect(out).toContain('gateway: already running')
    expect(out).toContain('worker:  already running')
    expect(out).toContain('claude:  a session with the glasses channel is already running')
    // the direct voice worker would answer calls too
    expect(readFileSync(join(env.root, 'pkill.log'), 'utf8')).toContain('-f voice/worker.py start')
  })

  test('model, effort and permission mode can be overridden', () => {
    const env = setup({ listening: [8788], workerRunning: true })
    const { windows } = run(env, { ...gemini, GLASSES_MODEL: 'opus', GLASSES_EFFORT: 'high', GLASSES_PERMISSION_MODE: 'manual' })
    expect(windows).toHaveLength(1)
    expect(windows[0][1]).toEndWith('--model opus --effort high --permission-mode manual')
  })

  test('extra folders become --add-dir, and voice approvals are switched on', () => {
    const env = setup({ listening: [8788], workerRunning: true })
    mkdirSync(join(env.root, 'poker-tracker'))
    mkdirSync(join(env.root, 'ugc automation'))
    const { windows, out } = run(env, gemini, [join(env.root, 'poker-tracker'), join(env.root, 'ugc automation')])
    expect(windows[0][1]).toEndWith(
      `--permission-mode auto --add-dir ${env.root}/poker-tracker --add-dir ${env.root}/ugc\\ automation`,
    )
    expect(out).toContain(`also has access to ${env.root}/ugc automation`)
    expect(existsSync(join(env.root, '.claude-glasses-bridge', 'permission-relay'))).toBe(true)
    run(env, { ...gemini, GLASSES_PERMISSION_RELAY: '0' })
    expect(existsSync(join(env.root, '.claude-glasses-bridge', 'permission-relay'))).toBe(false)
  })

  test('loads the profile in persona.md when there is one', () => {
    const env = setup({ listening: [8788], workerRunning: true })
    expect(run(env, gemini).windows[0][1]).not.toContain('--append-system-prompt-file')
    const persona = join(env.root, '.claude-glasses-bridge', 'persona.md')
    writeFileSync(persona, '# Who you are to me\n')
    const { windows, out } = run(env, gemini)
    expect(windows.at(-1)![1]).toEndWith(`--append-system-prompt-file ${persona}`)
    expect(out).toContain(`with your profile from ${persona}`)
  })

  test('stops with a pointer to the guide when the worker script is missing', () => {
    const env = setup({ withStartScript: false })
    const { code, err, windows } = run(env, gemini)
    expect(code).toBe(1)
    expect(err).toContain('IPHONE.md, step 5')
    expect(windows).toEqual([])
  })
})
