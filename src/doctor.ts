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

/** Seconds from `ps -o etime=` ("[[dd-]hh:]mm:ss"), or null. */
export function elapsedSeconds(etime: string): number | null {
  const match = /^\s*(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)\s*$/.exec(etime)
  if (!match) return null
  const [, days, hours, minutes, secs] = match
  return Number(days ?? 0) * 86400 + Number(hours ?? 0) * 3600 + Number(minutes) * 60 + Number(secs)
}

export interface ClaudeLaunch {
  model?: string
  effort?: string
  permissionMode?: string
  persona?: string
  addDirs: string[]
}

/** The start-glasses flags a running `claude` command line carries. */
export function claudeLaunch(command: string): ClaudeLaunch {
  const value = (flag: string) => new RegExp(`${flag} (\\S+)`).exec(command)?.[1]
  const addDirs = [...command.matchAll(/--add-dir (\S+)/g)].map(m => m[1])
  return {
    model: value('--model'),
    effort: value('--effort'),
    permissionMode: value('--permission-mode'),
    persona: value('--append-system-prompt-file'),
    addDirs,
  }
}

export interface VoiceStats {
  /** Seconds from the user finishing a sentence to the quick voice's first words, oldest first. */
  quick: number[]
  /** Seconds to the work model's first words, oldest first. */
  firstWords: number[]
  /** Seconds until each request was done, and how many tool calls it took. */
  done: { s: number; tools: number }[]
  calls: number
  lastCall?: string
  errors: string[]
  creditsOut: boolean
  /** Times Claude Code quit unexpectedly. */
  crashes: number
  /** The connectors the most recent Claude Code start could not connect. */
  notConnected: string[]
  /** An API error (billing_error, authentication_failed, ...) with no successful answer since. */
  apiError?: string
}

const VOICE_LINE_RE = /^(\S+ \S+) \[\d+\] (.*)$/

/** Reads ~/.claude-glasses-bridge/voice.log, written by voice/worker.py. */
export function parseVoiceLog(text: string): VoiceStats {
  const stats: VoiceStats = { quick: [], firstWords: [], done: [], calls: 0, errors: [], creditsOut: false, crashes: 0, notConnected: [] }
  for (const line of text.split('\n')) {
    const parsed = VOICE_LINE_RE.exec(line)
    if (!parsed) continue
    const [, at, msg] = parsed
    let m: RegExpExecArray | null
    if ((m = /^(.+?): first words after ([\d.]+)s$/.exec(msg))) (m[1] === 'quick voice' ? stats.quick : stats.firstWords).push(Number(m[2]))
    else if ((m = /: done after ([\d.]+)s, (\d+) tool call/.exec(msg))) {
      stats.done.push({ s: Number(m[1]), tools: Number(m[2]) })
      if (!msg.includes(', failed:')) stats.apiError = undefined
    } else if ((m = /: Claude API error \(([^)]*)\)/.exec(msg))) stats.apiError = m[1]
    else if (msg.startsWith('call started in room')) {
      stats.calls++
      stats.lastCall = at
    } else if ((m = /^voice error \(([^)]*)\): (.*)$/.exec(msg))) stats.errors.push(`${at}  ${m[1]}: ${m[2]}`.slice(0, 220))
    else if (msg.includes('out of free credits')) stats.creditsOut = true
    else if ((m = /: claude exited with code (-?\d+)$/.exec(msg)) && ![0, -15, -9, 143, 137].includes(Number(m[1]))) stats.crashes++
    else if ((m = /: connectors not connected: (.*)$/.exec(msg))) stats.notConnected = m[1].split(', ')
  }
  return stats
}

/** How long the user waits for the first words of an answer on a call. */
export function firstWordsVerdict(s: number): string {
  if (s < 3.5) return 'good'
  if (s < 6) return 'slow'
  return 'very slow'
}

/** The same for the work model, which speaks after the quick voice has already said "on it". */
export function workVerdict(s: number): string {
  if (s < 8) return 'good'
  if (s < 15) return 'slow'
  return 'very slow'
}

/** What a Claude Code credential source means for the user. */
export function credentials(source: string): { ok: boolean; say: string; fix?: string } {
  switch (source) {
    case 'none':
      return { ok: true, say: 'Claude Code uses your Claude subscription login' }
    case 'ANTHROPIC_API_KEY':
      return {
        ok: false,
        say: 'Claude Code is paying with an API key (ANTHROPIC_API_KEY) instead of your Claude subscription',
        fix: 'take GLASSES_USE_API_KEY=1 out of ~/.claude-glasses-bridge/voice.env, then restart start-glasses',
      }
    case 'apiKeyHelper':
      return {
        ok: false,
        say: 'Claude Code gets an API key from an apiKeyHelper setting instead of your Claude subscription',
        fix: 'remove "apiKeyHelper" from ~/.claude/settings.json',
      }
    case '/login managed key':
      return {
        ok: false,
        say: 'Claude Code is logged in with an Anthropic Console account (prepaid API credit), not your Claude subscription',
        fix: 'run claude in Terminal, type /logout, then /login and choose your Claude account',
      }
    default:
      return { ok: true, say: `Claude Code's credentials: ${source}` }
  }
}

export const API_ERROR_HELP: Record<string, { say: string; fix: string }> = {
  billing_error: {
    say: 'Claude Code said the credit balance is too low',
    fix: 'run glasses-doctor without --quick: its live test shows which account Claude Code uses',
  },
  authentication_failed: { say: "Claude Code isn't logged in", fix: 'run claude in Terminal and type /login' },
  rate_limit: { say: 'you hit your Claude usage limit', fix: 'wait for it to reset, or use a lighter model (GLASSES_MODEL=haiku)' },
}
