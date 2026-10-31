import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export interface Config {
  host: string
  port: number
  token: string
  dataDir: string
  framesDir: string
  /** How long an HTTP request holds open waiting for Claude's reply. */
  waitMs: number
  permissionRelay: boolean
  /** "auto": only open the HTTP port when started by a session with --channels or the development flag. */
  listen: 'auto' | 'always'
}

type Env = Record<string, string | undefined>

export function dataDir(env: Env = process.env): string {
  return env.GLASSES_DATA_DIR || join(homedir(), '.claude-glasses-bridge')
}

/**
 * GLASSES_TOKEN wins; otherwise the token lives in <dataDir>/token, generated
 * on first run. A file means the token survives restarts without having to
 * thread an env var through Claude Code's MCP config.
 */
export function loadToken(dir: string, env: Env = process.env): string {
  const fromEnv = env.GLASSES_TOKEN?.trim()
  if (fromEnv) {
    if (fromEnv.length < 16) throw new Error('GLASSES_TOKEN must be at least 16 characters')
    return fromEnv
  }
  const file = join(dir, 'token')
  if (existsSync(file)) return readFileSync(file, 'utf8').trim()
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  const token = randomBytes(24).toString('base64url')
  writeFileSync(file, token + '\n', { mode: 0o600 })
  return token
}

function number(value: string | undefined, fallback: number, name: string): number {
  if (value === undefined || value === '') return fallback
  const n = Number(value)
  if (!Number.isFinite(n) || n < 0) throw new Error(`${name} must be a non-negative number`)
  return n
}

export function loadConfig(env: Env = process.env): Config {
  const dir = dataDir(env)
  return {
    host: env.GLASSES_HOST || '127.0.0.1',
    port: number(env.GLASSES_PORT, 8790, 'GLASSES_PORT'),
    token: loadToken(dir, env),
    dataDir: dir,
    framesDir: join(dir, 'frames'),
    // VisionClaw's voice worker gives up on a task call after 120s, so answer
    // a little before that and let the late reply go to /pending-notifications.
    waitMs: number(env.GLASSES_WAIT_S, 110, 'GLASSES_WAIT_S') * 1000,
    // On with GLASSES_PERMISSION_RELAY=1, or when start-glasses left a
    // permission-relay file in the data folder (a file, because Claude Code
    // may not pass its own environment through to MCP servers).
    permissionRelay:
      env.GLASSES_PERMISSION_RELAY === '1' ||
      (env.GLASSES_PERMISSION_RELAY !== '0' && existsSync(join(dir, 'permission-relay'))),
    listen: env.GLASSES_LISTEN === 'always' ? 'always' : 'auto',
  }
}
