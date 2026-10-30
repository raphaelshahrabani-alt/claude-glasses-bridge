// Talk to the bridge from a terminal, the way the glasses would.
//   bun run talk "what's the git status of this repo?"
//   bun run talk --image photo.jpg "what am I looking at?"
//   bun run talk --events          (watch everything crossing the bridge)
import { readFileSync } from 'node:fs'
import { loadConfig } from '../src/config.ts'
import type { AskResult } from '../src/http.ts'

const config = loadConfig()
const base = process.env.GLASSES_URL ?? `http://${config.host}:${config.port}`
const auth = { Authorization: `Bearer ${config.token}` }
const args = process.argv.slice(2)

if (args[0] === '--events') {
  const res = await fetch(`${base}/events`, { headers: auth })
  if (!res.ok || !res.body) throw new Error(`GET /events: ${res.status}`)
  const decoder = new TextDecoder()
  for await (const chunk of res.body) {
    for (const line of decoder.decode(chunk, { stream: true }).split('\n')) {
      if (line.startsWith('data: ')) console.log(line.slice(6))
    }
  }
} else {
  let image: string | undefined
  const at = args.indexOf('--image')
  if (at !== -1) {
    image = readFileSync(args[at + 1]).toString('base64')
    args.splice(at, 2)
  }
  const text = args.join(' ').trim()
  if (!text) {
    console.error('usage: bun run talk [--image file.jpg] <what you would say>   |   bun run talk --events')
    process.exit(2)
  }
  const res = await fetch(`${base}/message`, {
    method: 'POST',
    headers: { ...auth, 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, image }),
  })
  const body = (await res.json()) as AskResult
  if (!res.ok) throw new Error(`POST /message: ${res.status} ${JSON.stringify(body)}`)
  if (body.status === 'replied') console.log(body.reply)
  else if (body.status === 'verdict') console.log(`(${body.behavior} ${body.request_id}) ${body.reply ?? ''}`.trim())
  else console.log(`(no reply yet for chat ${body.chat_id}; it will show up in GET /pending-notifications)`)
}
