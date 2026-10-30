import { mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const MAX_BYTES = 10 * 1024 * 1024
const KEEP = 100

function extension(bytes: Buffer): string | null {
  if (bytes[0] === 0xff && bytes[1] === 0xd8) return 'jpg'
  if (bytes.subarray(0, 4).toString('hex') === '89504e47') return 'png'
  if (bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WEBP') return 'webp'
  return null
}

/**
 * Writes a base64 image (bare or as a data: URL) where Claude Code can Read it
 * and returns the path. Returns null for anything that isn't a JPEG, PNG, or
 * WebP, so arbitrary bytes from the network never land on disk.
 */
export function saveImage(dir: string, base64: string, tag: string): string | null {
  const bare = base64.replace(/^data:[^,]*,/, '')
  const bytes = Buffer.from(bare, 'base64')
  if (bytes.length === 0 || bytes.length > MAX_BYTES) return null
  const ext = extension(bytes)
  if (!ext) return null
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  const path = join(dir, `${Date.now()}-${tag}.${ext}`)
  writeFileSync(path, bytes, { mode: 0o600 })
  prune(dir)
  return path
}

function prune(dir: string): void {
  const files = readdirSync(dir)
    .map(name => ({ path: join(dir, name), mtime: statSync(join(dir, name)).mtimeMs }))
    .sort((a, b) => b.mtime - a.mtime)
  for (const { path } of files.slice(KEEP)) rmSync(path, { force: true })
}
