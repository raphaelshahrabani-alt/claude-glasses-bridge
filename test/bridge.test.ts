// Runs the real server as a subprocess and plays Claude Code's side of the
// channel with an MCP client: receive <channel> events, call the reply tool,
// send permission requests.
import { afterAll, beforeAll, describe, expect, test } from 'bun:test'
import { existsSync, mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { parseVerdict } from '../src/hub.ts'
import { DEFERRAL, lastUserTurn } from '../src/http.ts'

const TOKEN = 'test-token-0123456789abcdef'
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 0x01, 0xff, 0xd9])

interface Note {
  method: string
  params?: Record<string, any>
}

class FakeClaudeCode {
  client = new Client({ name: 'fake-claude-code', version: '0.0.0' })
  base: string
  dataDir = mkdtempSync(join(tmpdir(), 'glasses-test-'))
  private seen: Note[] = []
  private waiting: Array<{ method: string; resolve: (n: Note) => void }> = []

  constructor(
    readonly port: number,
    private env: Record<string, string> = {},
    // Claude Code's own command line, which the bridge reads to decide whether to open the port
    private parentArgs = ['--dangerously-load-development-channels', 'server:glasses'],
  ) {
    this.base = `http://127.0.0.1:${port}`
    this.client.fallbackNotificationHandler = async n => {
      const note = n as Note
      const i = this.waiting.findIndex(w => w.method === note.method)
      if (i === -1) this.seen.push(note)
      else this.waiting.splice(i, 1)[0].resolve(note)
    }
  }

  async start(expectListening = true) {
    const dataDir = this.dataDir
    await this.client.connect(
      new StdioClientTransport({
        // a shell parent standing in for the claude process; "; exit" keeps sh from exec'ing away
        command: 'sh',
        args: ['-c', '"$0" "$1"; exit $?', process.execPath, join(import.meta.dir, '..', 'src', 'server.ts'), ...this.parentArgs],
        env: {
          PATH: process.env.PATH ?? '',
          HOME: dataDir,
          GLASSES_TOKEN: TOKEN,
          GLASSES_PORT: String(this.port),
          GLASSES_DATA_DIR: dataDir,
          GLASSES_WAIT_S: '2',
          ...this.env,
        },
        stderr: 'ignore',
      }),
    )
    if (!expectListening) return
    for (let i = 0; i < 50; i++) {
      try {
        if ((await fetch(`${this.base}/health`)).ok) return
      } catch {}
      await Bun.sleep(50)
    }
    throw new Error('bridge did not start')
  }

  next(method = 'notifications/claude/channel', ms = 3000): Promise<Note> {
    const i = this.seen.findIndex(n => n.method === method)
    if (i !== -1) return Promise.resolve(this.seen.splice(i, 1)[0])
    return new Promise((resolve, reject) => {
      const entry = { method, resolve: (n: Note) => (clearTimeout(timer), resolve(n)) }
      const timer = setTimeout(() => {
        this.waiting.splice(this.waiting.indexOf(entry), 1)
        reject(new Error(`no ${method} within ${ms}ms`))
      }, ms)
      this.waiting.push(entry)
    })
  }

  reply(chat_id: string, text: string) {
    return this.client.callTool({ name: 'reply', arguments: { chat_id, text } })
  }

  post(path: string, body: unknown, token = TOKEN) {
    return fetch(`${this.base}${path}`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  }

  get(path: string) {
    return fetch(`${this.base}${path}`, { headers: { Authorization: `Bearer ${TOKEN}` } })
  }
}

/** Collects parsed `data:` events from GET /events. */
async function openEvents(base: string) {
  const res = await fetch(`${base}/events?token=${TOKEN}`)
  expect(res.status).toBe(200)
  const events: any[] = []
  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  ;(async () => {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) return
      buffer += decoder.decode(value, { stream: true })
      let end
      while ((end = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, end)
        buffer = buffer.slice(end + 2)
        if (frame.startsWith('data: ')) events.push(JSON.parse(frame.slice(6)))
      }
    }
  })().catch(() => {})
  return { events, close: () => reader.cancel() }
}

describe('parseVerdict', () => {
  test('accepts typed and spoken forms', () => {
    expect(parseVerdict('yes abcde')).toEqual({ request_id: 'abcde', behavior: 'allow' })
    expect(parseVerdict('Yes, a b c d e.')).toEqual({ request_id: 'abcde', behavior: 'allow' })
    expect(parseVerdict('no K-Q-R-T-X')).toEqual({ request_id: 'kqrtx', behavior: 'deny' })
    expect(parseVerdict('n kqrtx!')).toEqual({ request_id: 'kqrtx', behavior: 'deny' })
  })

  test('rejects anything else', () => {
    expect(parseVerdict('yes')).toBeNull()
    expect(parseVerdict('yes please do it')).toBeNull()
    expect(parseVerdict('no thanks')).toBeNull() // six letters
    expect(parseVerdict('yes hello')).toBeNull() // "l" is not in the ID alphabet
    expect(parseVerdict('maybe abcde')).toBeNull()
  })
})

describe('lastUserTurn', () => {
  test('reads string and multipart content, inline images only', () => {
    expect(lastUserTurn([{ role: 'user', content: ' hi ' }])).toEqual({ text: 'hi' })
    const parts = [
      { type: 'text', text: 'what is this?' },
      { type: 'image_url', image_url: { url: 'https://example.com/x.jpg' } },
      { type: 'image_url', image_url: { url: 'data:image/jpeg;base64,AAAA' } },
    ]
    expect(lastUserTurn([{ role: 'user', content: 'old' }, { role: 'assistant', content: 'a' }, { role: 'user', content: parts }])).toEqual({
      text: 'what is this?',
      image: 'data:image/jpeg;base64,AAAA',
    })
    expect(lastUserTurn([{ role: 'assistant', content: 'a' }])).toBeNull()
    expect(lastUserTurn('nope')).toBeNull()
  })
})

describe('bridge with permission relay', () => {
  const cc = new FakeClaudeCode(18791, { GLASSES_PERMISSION_RELAY: '1' })
  beforeAll(() => cc.start())
  afterAll(() => cc.client.close())

  test('health is public and reports the connection', async () => {
    expect(await (await fetch(`${cc.base}/health`)).json()).toEqual({ ok: true, claude_connected: true, permission_relay: true })
    expect((await fetch(`${cc.base}/v1/chat/completions`)).status).toBe(200)
  })

  test('everything else needs the token', async () => {
    expect((await cc.post('/message', { text: 'hi' }, 'wrong-token-wrong-token')).status).toBe(401)
    expect((await fetch(`${cc.base}/message`, { method: 'POST', body: '{"text":"hi"}' })).status).toBe(401)
    expect((await fetch(`${cc.base}/pending-notifications`)).status).toBe(401)
    expect((await fetch(`${cc.base}/events`)).status).toBe(401)
  })

  test('the reply tool is listed', async () => {
    const { tools } = await cc.client.listTools()
    expect(tools.map(t => t.name)).toEqual(['reply'])
  })

  test('message round trip', async () => {
    const response = cc.post('/message', { text: 'what branch am I on?' })
    const note = await cc.next()
    expect(note.params?.content).toBe('what branch am I on?')
    expect(note.params?.meta?.chat_id).toMatch(/^[0-9a-f]{8}$/)
    expect(note.params?.meta?.image_path).toBeUndefined()
    const result = await cc.reply(note.params!.meta.chat_id, 'You are on main.')
    expect(result.content).toEqual([{ type: 'text', text: 'sent' }])
    expect(await (await response).json()).toEqual({ status: 'replied', chat_id: note.params!.meta.chat_id, reply: 'You are on main.' })
  })

  test('VisionClaw-style completion with a camera frame', async () => {
    const response = cc.post('/v1/chat/completions', {
      messages: [{ role: 'user', content: 'read the error on my screen' }],
      image: JPEG.toString('base64'),
    })
    const note = await cc.next()
    const imagePath = note.params?.meta?.image_path
    expect(imagePath).toEndWith('.jpg')
    expect(existsSync(imagePath)).toBe(true)
    expect(readFileSync(imagePath).equals(JPEG)).toBe(true)
    await cc.reply(note.params!.meta.chat_id, 'It says the port is in use.')
    const body: any = await (await response).json()
    expect(body.object).toBe('chat.completion')
    expect(body.choices[0].message).toEqual({ role: 'assistant', content: 'It says the port is in use.' })
  })

  test('streamed completion with an image_url part', async () => {
    const response = cc.post('/v1/chat/completions', {
      stream: true,
      messages: [{ role: 'user', content: [{ type: 'text', text: 'what is this?' }, { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${JPEG.toString('base64')}` } }] }],
    })
    const note = await cc.next()
    expect(note.params?.meta?.image_path).toEndWith('.jpg')
    await cc.reply(note.params!.meta.chat_id, 'A coffee mug.')
    const text = await (await response).text()
    expect(text).toContain('"content":"A coffee mug."')
    expect(text.trim().endsWith('data: [DONE]')).toBe(true)
  })

  test('bytes that are not an image are not written to disk', async () => {
    const response = cc.post('/message', { text: 'look', image: Buffer.from('#!/bin/sh\nrm -rf /').toString('base64') })
    const note = await cc.next()
    expect(note.params?.meta?.image_path).toBeUndefined()
    await cc.reply(note.params!.meta.chat_id, 'ok')
    await response
  })

  test('slow tasks defer, and the late reply waits in pending-notifications', async () => {
    const response = cc.post('/v1/chat/completions', { messages: [{ role: 'user', content: 'run the full test suite' }] })
    const note = await cc.next()
    const body: any = await (await response).json() // GLASSES_WAIT_S=2 in tests
    expect(body.choices[0].message.content).toBe(DEFERRAL)
    const result = await cc.reply(note.params!.meta.chat_id, 'All 212 tests passed.')
    expect((result.content as any)[0].text).toStartWith('queued')
    expect(await (await cc.get('/pending-notifications')).json()).toEqual({ notifications: ['All 212 tests passed.'] })
    expect(await (await cc.get('/pending-notifications')).json()).toEqual({ notifications: [] })
  })

  test('wait:false returns at once', async () => {
    const body: any = await (await cc.post('/message', { text: 'start the dev server', wait: false })).json()
    expect(body.status).toBe('pending')
    await cc.next()
  })

  test('permission prompt is spoken back and a spoken verdict answers it', async () => {
    const stream = await openEvents(cc.base)
    const response = cc.post('/message', { text: 'delete the build folder' })
    const note = await cc.next()

    await cc.client.notification({
      method: 'notifications/claude/channel/permission_request',
      params: { request_id: 'kqrtx', tool_name: 'Bash', description: 'Remove the build directory', input_preview: '{"command": "rm -rf build"}' },
    } as any)
    const body: any = await (await response).json()
    expect(body.status).toBe('replied')
    expect(body.reply).toContain('approval to use Bash: Remove the build directory')
    expect(body.reply).toContain('rm -rf build')
    expect(body.reply).toContain('say "yes k q r t x"')

    // an ID nobody issued is just conversation
    const stray = cc.post('/message', { text: 'yes maybe', wait: false })
    expect((await cc.next()).params?.content).toBe('yes maybe')
    await stray

    // the verdict request stays open for the answer to the task the prompt interrupted
    const verdict = cc.post('/message', { text: 'Yes, k q r t x.' })
    expect((await cc.next('notifications/claude/channel/permission')).params).toEqual({ request_id: 'kqrtx', behavior: 'allow' })
    await cc.reply(note.params!.meta.chat_id, 'Deleted the build folder.')
    expect(await (await verdict).json()).toEqual({ status: 'verdict', request_id: 'kqrtx', behavior: 'allow', reply: 'Deleted the build folder.' })
    expect(await (await cc.get('/pending-notifications')).json()).toEqual({ notifications: [] })

    await Bun.sleep(50)
    stream.close()
    expect(stream.events.map(e => e.type)).toEqual(['message', 'permission_request', 'message', 'permission_verdict', 'reply'])
    expect(stream.events[1].prompt).toBe(body.reply)
  })

  test('a denied prompt with no follow-up answers "Denied." to the voice worker', async () => {
    const response = cc.post('/v1/chat/completions', { messages: [{ role: 'user', content: 'push to main' }] })
    const note = await cc.next()
    await cc.client.notification({
      method: 'notifications/claude/channel/permission_request',
      params: { request_id: 'pushx', tool_name: 'Bash', description: 'Push to origin', input_preview: '{"command": "git push"}' },
    } as any)
    const prompt: any = await (await response).json()
    expect(prompt.choices[0].message.content).toContain('"no p u s h x"')
    const denied: any = await (await cc.post('/v1/chat/completions', { messages: [{ role: 'user', content: 'no p u s h x' }] })).json()
    expect(denied.choices[0].message.content).toBe('Denied.') // GLASSES_WAIT_S=2 passed with no reply
    expect((await cc.next('notifications/claude/channel/permission')).params).toEqual({ request_id: 'pushx', behavior: 'deny' })
    await cc.reply(note.params!.meta.chat_id, 'Okay, I did not push.')
    expect(await (await cc.get('/pending-notifications')).json()).toEqual({ notifications: ['Okay, I did not push.'] })
  })

  test('POST /permission answers a prompt directly', async () => {
    await cc.client.notification({
      method: 'notifications/claude/channel/permission_request',
      params: { request_id: 'mnpqr', tool_name: 'Write', description: 'Write notes.md', input_preview: '{}' },
    } as any)
    await Bun.sleep(50)
    expect((await cc.post('/permission', { request_id: 'zzzzz', behavior: 'allow' })).status).toBe(404)
    expect((await cc.post('/permission', { request_id: 'mnpqr', behavior: 'maybe' })).status).toBe(400)
    expect((await cc.post('/permission', { request_id: 'mnpqr', behavior: 'deny' })).status).toBe(200)
    expect((await cc.next('notifications/claude/channel/permission')).params).toEqual({ request_id: 'mnpqr', behavior: 'deny' })
    await cc.get('/pending-notifications') // drop the queued prompt
  })
})

describe('bridge without permission relay', () => {
  const cc = new FakeClaudeCode(18792)
  beforeAll(() => cc.start())

  test('verdict-shaped text is forwarded as a message and /permission is refused', async () => {
    const response = cc.post('/message', { text: 'yes abcde' })
    const note = await cc.next()
    expect(note.params?.content).toBe('yes abcde')
    await cc.reply(note.params!.meta.chat_id, 'Yes to what?')
    expect(((await (await response).json()) as any).reply).toBe('Yes to what?')
    expect((await cc.post('/permission', { request_id: 'abcde', behavior: 'allow' })).status).toBe(409)
  })

  test('--channels counts as a channel session too', async () => {
    const plugin = new FakeClaudeCode(18794, {}, ['--channels', 'plugin:glasses@local'])
    await plugin.start()
    expect((await fetch(`${plugin.base}/health`)).ok).toBe(true)
    await plugin.client.close()
  })

  test('a Claude Code process without channels does not take the port', async () => {
    const spare = new FakeClaudeCode(18793, {}, ['bg-spare', '--bg-spare'])
    await spare.start(false)
    expect((await spare.client.listTools()).tools.map(t => t.name)).toEqual(['reply'])
    await Bun.sleep(200)
    await expect(fetch(`${spare.base}/health`)).rejects.toThrow()
    expect(readFileSync(join(spare.dataDir, 'bridge.log'), 'utf8')).toContain('not listening: this Claude Code process was not started with channels')
    await spare.client.close()

    const forced = new FakeClaudeCode(18793, { GLASSES_LISTEN: 'always' }, ['bg-spare', '--bg-spare'])
    await forced.start()
    expect((await fetch(`${forced.base}/health`)).ok).toBe(true)
    await forced.client.close()
  })

  test('exits with Claude Code, frees the port, and says why in bridge.log', async () => {
    await cc.client.close()
    await Bun.sleep(300)
    await expect(fetch(`${cc.base}/health`)).rejects.toThrow()
    const lines = readFileSync(join(cc.dataDir, 'bridge.log'), 'utf8')
    expect(lines).toContain('--dangerously-load-development-channels server:glasses)')
    expect(lines).toContain('Claude Code connected (fake-claude-code 0.0.0)')
    expect(lines).toContain('chat ')
    expect(lines).toContain('reply ')
    expect(lines).toMatch(/exiting: (stdin ended|stdin closed|MCP connection closed)/)
  })
})
