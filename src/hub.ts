import { randomBytes } from 'node:crypto'

export interface PermissionRequest {
  request_id: string
  tool_name: string
  description: string
  input_preview: string
}

export type Behavior = 'allow' | 'deny'

/** Everything that crosses the bridge, streamed to GET /events listeners. */
export type BridgeEvent =
  | { type: 'message'; chat_id: string; text: string; image_path?: string; at: number }
  | { type: 'reply'; chat_id: string; text: string; at: number }
  | ({ type: 'permission_request'; prompt: string; at: number } & PermissionRequest)
  | { type: 'permission_verdict'; request_id: string; behavior: Behavior; at: number }

const MAX_PENDING = 50
const MAX_OPEN_PERMISSIONS = 20

/**
 * In-memory state shared by the MCP side and the HTTP side: who is waiting
 * for which reply, replies nobody waited for, and open permission prompts.
 */
export class Hub {
  private waiters = new Map<string, (text: string) => void>()
  private pending: string[] = []
  private listeners = new Set<(event: BridgeEvent) => void>()
  private permissions = new Map<string, PermissionRequest>()
  /** request_id -> the chat whose waiting request was answered with that prompt */
  private permissionChats = new Map<string, string>()

  newChatId(): string {
    return randomBytes(4).toString('hex')
  }

  subscribe(listener: (event: BridgeEvent) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  emit(event: BridgeEvent): void {
    for (const listener of this.listeners) {
      try {
        listener(event)
      } catch {
        // one broken SSE stream must not stop delivery to the others
      }
    }
  }

  /**
   * Resolves with the next reply for chat_id, or null on timeout or abort.
   * Register before notifying Claude so a fast reply can't slip past.
   * A reply that lands after we stop waiting goes to the pending queue.
   */
  waitForReply(chat_id: string, ms: number, signal?: AbortSignal): Promise<string | null> {
    return new Promise(resolve => {
      const stop = (value: string | null) => {
        clearTimeout(timer)
        signal?.removeEventListener('abort', onAbort)
        this.waiters.delete(chat_id)
        resolve(value)
      }
      const onAbort = () => stop(null)
      const timer = setTimeout(() => stop(null), ms)
      signal?.addEventListener('abort', onAbort)
      this.waiters.set(chat_id, text => stop(text))
      if (signal?.aborted) stop(null)
    })
  }

  isWaiting(chat_id: string): boolean {
    return this.waiters.has(chat_id)
  }

  deliverReply(chat_id: string, text: string): void {
    this.emit({ type: 'reply', chat_id, text, at: Date.now() })
    const waiter = this.waiters.get(chat_id)
    if (waiter) waiter(text)
    else this.pushPending(text)
  }

  pushPending(text: string): void {
    this.pending.push(text)
    if (this.pending.length > MAX_PENDING) this.pending.splice(0, this.pending.length - MAX_PENDING)
  }

  takePending(): string[] {
    return this.pending.splice(0)
  }

  /**
   * A permission dialog opened in Claude Code. The person on the glasses is
   * most likely waiting on an answer right now, so the prompt is handed to the
   * newest waiting request (to be spoken) instead of sitting in the queue.
   */
  openPermission(request: PermissionRequest): string {
    this.permissions.set(request.request_id, request)
    if (this.permissions.size > MAX_OPEN_PERMISSIONS) {
      const oldest = this.permissions.keys().next().value
      if (oldest !== undefined) this.forgetPermission(oldest)
    }
    const prompt = permissionPrompt(request)
    this.emit({ type: 'permission_request', ...request, prompt, at: Date.now() })
    const newest = [...this.waiters.entries()].at(-1)
    if (newest) {
      const [chat_id, waiter] = newest
      this.permissionChats.set(request.request_id, chat_id)
      waiter(prompt)
    } else {
      this.pushPending(prompt)
    }
    return prompt
  }

  /** The chat that was interrupted by this prompt; its real answer comes after the verdict. */
  chatForPermission(request_id: string): string | undefined {
    return this.permissionChats.get(request_id)
  }

  hasOpenPermission(request_id: string): boolean {
    return this.permissions.has(request_id)
  }

  closePermission(request_id: string, behavior: Behavior): void {
    this.forgetPermission(request_id)
    this.emit({ type: 'permission_verdict', request_id, behavior, at: Date.now() })
  }

  private forgetPermission(request_id: string): void {
    this.permissions.delete(request_id)
    this.permissionChats.delete(request_id)
  }
}

function clip(text: string, max: number): string {
  return text.length <= max ? text : text.slice(0, max - 1) + '…'
}

/** Written to be read aloud: the ID is spelled out so a voice model can repeat it. */
export function permissionPrompt(request: PermissionRequest): string {
  const spelled = request.request_id.split('').join(' ')
  const preview = request.input_preview ? ` Details: ${clip(request.input_preview, 240)}.` : ''
  return (
    `Claude Code is asking for approval to use ${request.tool_name}: ${clip(request.description, 200)}.${preview} ` +
    `To allow it, say "yes ${spelled}". To deny it, say "no ${spelled}".`
  )
}

// Claude Code's request IDs are five letters from a–z without "l". Speech-to-text
// turns "yes kqrtx" into things like "Yes, k q r t x." or "no k-q-r-t-x", so
// separators between the letters are tolerated and stripped.
const VERDICT_RE = /^\s*(y|yes|n|no)\b[\s,.:;-]*((?:[a-km-z][\s,.-]*){5})[.!?\s]*$/i

export function parseVerdict(text: string): { request_id: string; behavior: Behavior } | null {
  const match = VERDICT_RE.exec(text)
  if (!match) return null
  const request_id = match[2].replace(/[^a-z]/gi, '').toLowerCase()
  return { request_id, behavior: match[1].toLowerCase().startsWith('y') ? 'allow' : 'deny' }
}
