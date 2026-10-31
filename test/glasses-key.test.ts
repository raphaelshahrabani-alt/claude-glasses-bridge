// bin/glasses-key saves a key in voice.env without echoing it, replacing an earlier value.
import { describe, expect, test } from 'bun:test'
import { chmodSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const script = join(import.meta.dir, '..', 'bin', 'glasses-key')

function run(dir: string, args: string[], input: string, env: Record<string, string> = {}) {
  const proc = Bun.spawnSync(['bash', script, ...args], { env: { PATH: '/usr/bin:/bin', GLASSES_DATA_DIR: dir, ...env }, stdin: Buffer.from(input) })
  return { code: proc.exitCode, out: proc.stdout.toString() + proc.stderr.toString() }
}

/** A stand-in for the Python that runs voicecheck.py: prints the given reply as ElevenLabs' answer. */
function checker(dir: string, reply: string): Record<string, string> {
  const path = join(dir, 'fake-python')
  writeFileSync(path, `#!/bin/bash\nprintf '%s\\n' '${reply}'\n`)
  chmodSync(path, 0o755)
  return { GLASSES_VOICE_PYTHON: path }
}

describe('glasses-key', () => {
  test('saves the key, replacing an earlier or empty one, and keeps other settings', () => {
    const dir = mkdtempSync(join(tmpdir(), 'glasses-key-'))
    writeFileSync(join(dir, 'voice.env'), 'ELEVEN_API_KEY=\nGLASSES_WORK_EFFORT=medium\n')
    const { code, out } = run(dir, ['ELEVEN_API_KEY'], '  sk_live_123456  \n', checker(dir, '{"ok": true}'))
    expect(code).toBe(0)
    expect(out).toContain('it ends in 3456')
    expect(out).not.toContain('sk_live_123456')
    expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toBe('GLASSES_WORK_EFFORT=medium\nELEVEN_API_KEY=sk_live_123456\n')
    expect(statSync(join(dir, 'voice.env')).mode & 0o777).toBe(0o600)
  })

  describe('an ElevenLabs key is checked with ElevenLabs', () => {
    const fresh = () => {
      const dir = mkdtempSync(join(tmpdir(), 'glasses-key-'))
      writeFileSync(join(dir, 'voice.env'), 'ELEVEN_API_KEY=old_key_1234\nGLASSES_WORK_EFFORT=medium\n')
      return dir
    }

    test('a good key is kept', () => {
      const dir = fresh()
      const { code, out } = run(dir, ['ELEVEN_API_KEY'], 'sk_new_5678\n', checker(dir, '{"ok": true, "key_ends": "5678"}'))
      expect(code).toBe(0)
      expect(out).toContain('✓ ElevenLabs accepts this key')
      expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toContain('ELEVEN_API_KEY=sk_new_5678')
    })

    test('a key ElevenLabs says is invalid is not kept, and the old one stays', () => {
      const dir = fresh()
      const { code, out } = run(dir, ['ELEVEN_API_KEY'], 'sk_typo_0000\n', checker(dir, '{"ok": false, "status": "invalid_api_key", "problem": "ElevenLabs says the key is not valid"}'))
      expect(code).toBe(1)
      expect(out).toContain("✗ ElevenLabs says this key isn't valid, so I did not keep it")
      expect(out).toContain('Create API Key')
      expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toBe('ELEVEN_API_KEY=old_key_1234\nGLASSES_WORK_EFFORT=medium\n')
    })

    test('a valid key on an account with a problem is kept, and the problem is said', () => {
      const dir = fresh()
      const { code, out } = run(dir, ['ELEVEN_API_KEY'], 'sk_new_5678\n', checker(dir, '{"ok": false, "status": "quota_exceeded", "problem": "the ElevenLabs account is out of credits for this month", "fix": "upgrade the plan"}'))
      expect(code).toBe(0)
      expect(out).toContain('The key itself is fine, but: the ElevenLabs account is out of credits for this month')
      expect(out).toContain('upgrade the plan')
      expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toContain('ELEVEN_API_KEY=sk_new_5678')
    })

    test('no internet keeps the key and does not claim it is fine', () => {
      const dir = fresh()
      const { code, out } = run(dir, ['ELEVEN_API_KEY'], 'sk_new_5678\n', checker(dir, '{"ok": false, "status": "network", "problem": "could not reach ElevenLabs"}'))
      expect(code).toBe(0)
      expect(out).toContain("Couldn't reach ElevenLabs to check it")
      expect(out).not.toContain('key itself is fine')
      expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toContain('ELEVEN_API_KEY=sk_new_5678')
    })

    test('no answer from the checker keeps the key and says it could not check', () => {
      const dir = fresh()
      const { code, out } = run(dir, ['ELEVEN_API_KEY'], 'sk_new_5678\n', checker(dir, ''))
      expect(code).toBe(0)
      expect(out).toContain("Couldn't check it just now")
      expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toContain('ELEVEN_API_KEY=sk_new_5678')
    })

    test('other keys are not sent to ElevenLabs', () => {
      const dir = fresh()
      const { out } = run(dir, ['DEEPGRAM_API_KEY'], 'dg_key\n', checker(dir, '{"ok": false, "status": "invalid_api_key"}'))
      expect(out).not.toContain('ElevenLabs')
      expect(readFileSync(join(dir, 'voice.env'), 'utf8')).toContain('DEEPGRAM_API_KEY=dg_key')
    })
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
