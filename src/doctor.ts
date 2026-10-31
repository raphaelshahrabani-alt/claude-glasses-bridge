// Pure helpers for scripts/doctor.ts, kept here so they can be tested.

export interface RoundTrip {
  chat_id: string
  text: string
  at: number
  /** Milliseconds from the question reaching the bridge to Claude's reply, if it replied. */
  ms?: number
  delivered?: boolean
}

const CHAT_RE = /^(\S+) \[\d+\] chat ([0-9a-f]+): (".*?")( \+image)?$/
const REPLY_RE = /^(\S+) \[\d+\] reply ([0-9a-f]+) \((delivered|queued for later)\): /

function unquote(json: string): string {
  try {
    return JSON.parse(json)
  } catch {
    return json
  }
}

/** Pairs each question in bridge.log with Claude's first reply to it. */
export function parseBridgeLog(text: string): RoundTrip[] {
  const trips = new Map<string, RoundTrip>()
  for (const line of text.split('\n')) {
    const chat = CHAT_RE.exec(line)
    if (chat) {
      trips.set(chat[2], { chat_id: chat[2], text: unquote(chat[3]), at: Date.parse(chat[1]) })
      continue
    }
    const reply = REPLY_RE.exec(line)
    const trip = reply ? trips.get(reply[2]) : undefined
    if (reply && trip && trip.ms === undefined) {
      trip.ms = Date.parse(reply[1]) - trip.at
      trip.delivered = reply[3] === 'delivered'
    }
  }
  return [...trips.values()]
}

export function median(values: number[]): number | undefined {
  if (values.length === 0) return undefined
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

export type PatchState = 'missing' | 'unpatched' | 'old' | 'current'

/** CLAUDE_CODE_PATCH_VERSION in integrations/visionclaw/claude-code.patch; bump both together. */
export const PATCH_VERSION = 5

/** Which version of integrations/visionclaw/claude-code.patch a VisionClaw agent/main.py carries. */
export function patchState(mainPy: string | null): PatchState {
  if (mainPy === null) return 'missing'
  if (!mainPy.includes('def claude_code(')) return 'unpatched'
  return mainPy.includes(`CLAUDE_CODE_PATCH_VERSION = ${PATCH_VERSION}\n`) ? 'current' : 'old'
}

export function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`
}

/** How a Claude Code round trip feels on a voice call, where the voice model adds a few seconds on top. */
export function verdict(ms: number): string {
  if (ms < 8_000) return 'good'
  if (ms < 20_000) return 'slow'
  return 'very slow'
}
