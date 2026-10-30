import { appendFileSync, mkdirSync, renameSync, statSync } from 'node:fs'
import { dirname } from 'node:path'

const MAX_BYTES = 1024 * 1024

let file: string | undefined

/**
 * Also write log lines to a file. Claude Code only surfaces a channel server's
 * stderr in its debug log, so without this a crash leaves no trace you can find.
 */
export function logToFile(path: string): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
  try {
    if (statSync(path).size > MAX_BYTES) renameSync(path, path + '.1')
  } catch {
    // no log yet
  }
  file = path
}

export function log(message: string): void {
  // stdout is the MCP transport; anything printed there corrupts it
  process.stderr.write(`[glasses] ${message}\n`)
  if (!file) return
  try {
    appendFileSync(file, `${new Date().toISOString()} [${process.pid}] ${message}\n`, { mode: 0o600 })
  } catch {
    // logging must never take the bridge down
  }
}
