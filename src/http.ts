import { createHash, timingSafeEqual } from 'node:crypto'
import type { Channel } from './channel.ts'
import type { Config } from './config.ts'
import { type Behavior, type BridgeEvent, type Hub, parseVerdict } from './hub.ts'
import { saveImage } from './images.ts'
import { log } from './log.ts'

/** VisionClaw's voice worker treats a result with this prefix as "still running". */
export const DEFERRAL =
  '[task started] Claude Code is still working on this. Tell the user it is still running and that the answer will come later.'

export type AskResult =
  | { status: 'replied'; chat_id: string; reply: string }
  | { status: 'pending'; chat_id: string }
  /** reply is Claude's answer to the request the permission prompt interrupted, if it came in time. */
  | { status: 'verdict'; request_id: string; behavior: Behavior; reply: string | null }

export interface BridgeState {
  claudeConnected: boolean
}

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status })
}

function tokenMatches(given: string | null | undefined, expected: string): boolean {
  if (!given) return false
  const a = createHash('sha256').update(given).digest()
  const b = createHash('sha256').update(expected).digest()
  return timingSafeEqual(a, b)
}

async function readJson(req: Request): Promise<Record<string, unknown> | null> {
  try {
    const body = await req.json()
    return body && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/** Pulls the last user turn out of an OpenAI-style messages array: its text and first inline image. */
export function lastUserTurn(messages: unknown): { text: string; image?: string } | null {
  if (!Array.isArray(messages)) return null
  const turn = [...messages].reverse().find(m => m && typeof m === 'object' && m.role === 'user')
  if (!turn) return null
  if (typeof turn.content === 'string') return { text: turn.content.trim() }
  if (!Array.isArray(turn.content)) return null
  const texts: string[] = []
  let image: string | undefined
  for (const part of turn.content) {
    if (part?.type === 'text' && typeof part.text === 'string') texts.push(part.text)
    // only inline images: fetching arbitrary URLs from here would be an SSRF hole
    const url = part?.type === 'image_url' ? part.image_url?.url : undefined
    if (!image && typeof url === 'string' && url.startsWith('data:image/')) image = url
  }
  return { text: texts.join('\n').trim(), image }
}

function completion(content: string) {
  return {
    id: `chatcmpl-${crypto.randomUUID()}`,
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: 'claude-code',
    choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
  }
}

function streamedCompletion(content: string): Response {
  const id = `chatcmpl-${crypto.randomUUID()}`
  const created = Math.floor(Date.now() / 1000)
  const chunk = (delta: object, finish_reason: string | null = null) =>
    `data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: 'claude-code', choices: [{ index: 0, delta, finish_reason }] })}\n\n`
  const body = chunk({ role: 'assistant' }) + chunk({ content }) + chunk({}, 'stop') + 'data: [DONE]\n\n'
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' } })
}

export function createHandler(deps: { hub: Hub; channel: Channel; config: Config; state: BridgeState }) {
  const { hub, channel, config, state } = deps

  async function ask(text: string, opts: { image?: string; wait: boolean; signal?: AbortSignal }): Promise<AskResult> {
    if (config.permissionRelay) {
      const verdict = parseVerdict(text)
      // An unknown ID falls through as an ordinary message, so "yes, maybe" still reaches Claude.
      if (verdict && hub.hasOpenPermission(verdict.request_id)) {
        // The person who approved is still listening, so hold this request open
        // for the answer to the task the prompt interrupted.
        const chat = hub.chatForPermission(verdict.request_id)
        const reply = chat && opts.wait ? hub.waitForReply(chat, config.waitMs, opts.signal) : null
        await channel.verdict(verdict.request_id, verdict.behavior)
        log(`permission ${verdict.request_id}: ${verdict.behavior}`)
        return { status: 'verdict', ...verdict, reply: reply ? await reply : null }
      }
    }

    const chat_id = hub.newChatId()
    const meta: Record<string, string> = { chat_id }
    if (opts.image) {
      const path = saveImage(config.framesDir, opts.image, chat_id)
      if (path) meta.image_path = path
      else log(`chat ${chat_id}: ignored an image that is not a JPEG, PNG, or WebP`)
    }
    hub.emit({ type: 'message', chat_id, text, image_path: meta.image_path, at: Date.now() })
    const reply = opts.wait ? hub.waitForReply(chat_id, config.waitMs, opts.signal) : null
    await channel.push(text, meta)
    log(`chat ${chat_id}: ${JSON.stringify(text.slice(0, 120))}${meta.image_path ? ' +image' : ''}`)
    const answer = reply ? await reply : null
    return answer === null ? { status: 'pending', chat_id } : { status: 'replied', chat_id, reply: answer }
  }

  function authorized(req: Request, url: URL, allowQueryToken = false): boolean {
    const bearer = req.headers.get('authorization')?.match(/^Bearer\s+(.+)$/i)?.[1]?.trim()
    return tokenMatches(bearer ?? (allowQueryToken ? url.searchParams.get('token') : null), config.token)
  }

  function events(req: Request): Response {
    let unsubscribe = () => {}
    let ping: ReturnType<typeof setInterval> | undefined
    const stream = new ReadableStream<string>({
      start(ctrl) {
        ctrl.enqueue(': connected\n\n')
        unsubscribe = hub.subscribe((event: BridgeEvent) => ctrl.enqueue(`data: ${JSON.stringify(event)}\n\n`))
        ping = setInterval(() => {
          try {
            ctrl.enqueue(': ping\n\n')
          } catch {
            // the stream closed without an abort; a throw here would be uncaught
            unsubscribe()
            clearInterval(ping)
          }
        }, 20_000)
        req.signal.addEventListener('abort', () => {
          unsubscribe()
          clearInterval(ping)
        })
      },
      cancel() {
        unsubscribe()
        clearInterval(ping)
      },
    })
    return new Response(stream, { headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' } })
  }

  return async function handle(req: Request): Promise<Response> {
    const url = new URL(req.url)
    const route = `${req.method} ${url.pathname}`

    // Unauthenticated probes. VisionClaw's settings screen GETs the completions path to test reachability.
    if (route === 'GET /health') {
      return json({ ok: true, claude_connected: state.claudeConnected, permission_relay: config.permissionRelay })
    }
    if (route === 'GET /v1/chat/completions') return json({ ok: true, service: 'claude-glasses-bridge' })

    if (!authorized(req, url, route === 'GET /events')) {
      return json({ error: { message: 'invalid or missing bridge token' } }, 401)
    }

    switch (route) {
      case 'POST /v1/chat/completions': {
        const body = await readJson(req)
        const turn = lastUserTurn(body?.messages)
        if (!turn?.text) return json({ error: { message: 'no user message found' } }, 400)
        // VisionClaw sends the camera frame as a top-level base64 "image" field
        const image = typeof body?.image === 'string' ? body.image : turn.image
        const result = await ask(turn.text, { image, wait: true, signal: req.signal })
        const content =
          result.status === 'replied' ? result.reply
          : result.status === 'verdict' ? (result.reply ?? (result.behavior === 'allow' ? DEFERRAL : 'Denied.'))
          : DEFERRAL
        return body?.stream === true ? streamedCompletion(content) : json(completion(content))
      }

      case 'POST /message': {
        const body = await readJson(req)
        const text = typeof body?.text === 'string' ? body.text.trim() : ''
        if (!text) return json({ error: { message: 'text is required' } }, 400)
        const image = typeof body?.image === 'string' ? body.image : undefined
        return json(await ask(text, { image, wait: body?.wait !== false, signal: req.signal }))
      }

      case 'POST /permission': {
        if (!config.permissionRelay) return json({ error: { message: 'permission relay is off (GLASSES_PERMISSION_RELAY=1)' } }, 409)
        const body = await readJson(req)
        const request_id = typeof body?.request_id === 'string' ? body.request_id.toLowerCase() : ''
        const behavior = body?.behavior
        if (!/^[a-km-z]{5}$/.test(request_id) || (behavior !== 'allow' && behavior !== 'deny')) {
          return json({ error: { message: 'need request_id (5 letters) and behavior "allow" or "deny"' } }, 400)
        }
        if (!hub.hasOpenPermission(request_id)) return json({ error: { message: 'no open request with that id' } }, 404)
        await channel.verdict(request_id, behavior)
        log(`permission ${request_id}: ${behavior}`)
        return json({ ok: true })
      }

      case 'GET /pending-notifications':
        return json({ notifications: hub.takePending() })

      case 'POST /pending-notifications': {
        const body = await readJson(req)
        if (typeof body?.text !== 'string' || !body.text.trim()) return json({ error: { message: 'text is required' } }, 400)
        hub.pushPending(body.text.trim())
        return json({ ok: true })
      }

      case 'GET /events':
        return events(req)
    }
    return json({ error: { message: 'not found' } }, 404)
  }
}
