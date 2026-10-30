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
  return { root, visionclaw, project, log, bin }
}

function run(env: ReturnType<typeof setup>, extraEnv: Record<string, string> = {}) {
  const proc = Bun.spawnSync(['bash', script, env.project], {
    env: { PATH: `${env.bin}:/usr/bin:/bin`, HOME: env.root, VISIONCLAW_DIR: env.visionclaw, ...extraEnv },
  })
  const windows = existsSync(env.log)
    ? readFileSync(env.log, 'utf8').trim().split('\n').map(line => line.split('\t'))
    : []
  return { code: proc.exitCode, out: proc.stdout.toString(), err: proc.stderr.toString(), windows }
}

describe('start-glasses', () => {
  test('opens the gateway, worker and Claude Code windows', () => {
    const env = setup()
    const { code, windows, out } = run(env)
    expect(code).toBe(0)
    expect(windows.map(w => w[0])).toEqual(['glasses: gateway', 'glasses: worker', 'glasses: claude'])
    expect(windows[0][1]).toBe(`cd ${env.visionclaw}/gateway && npm run dev`)
    expect(windows[1][1]).toBe(`CLAUDE_CODE_ONLY=1 caffeinate -dims ${env.visionclaw}/agent/start.sh`)
    // the space in the project path is escaped, not split
    expect(windows[2][1]).toBe(
      `cd ${env.root}/my\\ project && claude --dangerously-load-development-channels server:glasses --model sonnet --effort low --permission-mode auto`,
    )
    expect(out).toContain('I am using this for local development')
  })

  test('leaves running pieces alone', () => {
    const env = setup({ listening: [8788, 8790], workerRunning: true })
    const { code, windows, out } = run(env)
    expect(code).toBe(0)
    expect(windows).toEqual([])
    expect(out).toContain('gateway: already running')
    expect(out).toContain('worker:  already running')
    expect(out).toContain('claude:  a session with the glasses channel is already running')
  })

  test('model, effort and permission mode can be overridden', () => {
    const env = setup({ listening: [8788], workerRunning: true })
    const { windows } = run(env, { GLASSES_MODEL: 'opus', GLASSES_EFFORT: 'high', GLASSES_PERMISSION_MODE: 'manual' })
    expect(windows).toHaveLength(1)
    expect(windows[0][1]).toEndWith('--model opus --effort high --permission-mode manual')
  })

  test('stops with a pointer to the guide when the worker script is missing', () => {
    const env = setup({ withStartScript: false })
    const { code, err, windows } = run(env)
    expect(code).toBe(1)
    expect(err).toContain('IPHONE.md, step 5')
    expect(windows).toEqual([])
  })
})
