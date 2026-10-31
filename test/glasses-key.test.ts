// bin/glasses-key saves a key in voice.env without echoing it, replacing an earlier value.
import { describe, expect, test } from 'bun:test'
import { mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const script = join(import.meta.dir, '..', 'bin', 'glasses-key')

function run(dir: string, args: string[], input: string) {
  const proc = Bun.spawnSync(['bash', script, ...args], { env: { PATH: '/usr/bin:/bin', GLASSES_DATA_DIR: dir }, stdin: Buffer.from(input) })
  return { code: proc.exitCode, out: proc.stdout.toString() + proc.stderr.toString() }
}

describe('glasses-key', () => {
  test('saves the key, replacing an earlier or empty one, and keeps other settings', () => {
    const dir = mkdtempSync(join(tmpdir(), 'glasses-key-'))
    writeFileSync(join(dir, 'voice.env'), 'ELEVEN_API_KEY=\nGLASSES_WORK_EFFORT=medium\n')
    const { code, out } = run(dir, ['ELEVEN_API_KEY'], '  sk_live_123456  \n')
    expect(code).toBe(0)
    expect(out).toContain('it ends in 3456')
    expect(out).not.toContain('sk_live_123456')
    expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toBe('GLASSES_WORK_EFFORT=medium\nELEVEN_API_KEY=sk_live_123456\n')
    expect(statSync(join(dir, 'voice.env')).mode & 0o777).toBe(0o600)
  })

  test('changes nothing when nothing is pasted', () => {
    const dir = mkdtempSync(join(tmpdir(), 'glasses-key-'))
    writeFileSync(join(dir, 'voice.env'), 'ELEVEN_API_KEY=old\n')
    expect(run(dir, ['ELEVEN_API_KEY'], '\n').code).toBe(1)
    expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toBe('ELEVEN_API_KEY=old\n')
  })

  test('refuses names that are not variable names', () => {
    expect(run(mkdtempSync(join(tmpdir(), 'glasses-key-')), ['bad name'], 'x\n').code).toBe(1)
  })
})
