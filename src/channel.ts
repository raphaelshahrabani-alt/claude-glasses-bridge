import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import type { Behavior, Hub } from './hub.ts'

export const INSTRUCTIONS = `Requests spoken through the user's Meta smart glasses arrive as <channel source="glasses" chat_id="..."> tags. A voice assistant on their phone relays them, so the wording may be paraphrased or contain speech-to-text mistakes.

- If the tag has an image_path attribute, Read that file first: it is a photo from the glasses camera of what the user is looking at.
- Do what is asked, then call the reply tool exactly once with the chat_id from the tag and your answer. Every message needs a reply, even when you could not do it.
- Your reply is read aloud. Write one to three short sentences of plain spoken text: no markdown, code, URLs, or long file paths unless the user asked for them. Say what you did and what you found.
- If the request is ambiguous, or it would do something destructive or hard to undo, ask a short question in your reply instead of acting. The answer arrives as a new message.`

const PermissionRequestSchema = z.object({
  method: z.literal('notifications/claude/channel/permission_request'),
  params: z.object({
    request_id: z.string(),
    tool_name: z.string(),
    description: z.string(),
    input_preview: z.string(),
  }),
})

export interface Channel {
  server: Server
  /** Push a request from the glasses into the Claude Code session. */
  push(content: string, meta: Record<string, string>): Promise<void>
  /** Answer an open permission dialog. */
  verdict(request_id: string, behavior: Behavior): Promise<void>
}

export function createChannel(hub: Hub, opts: { permissionRelay: boolean }): Channel {
  const server = new Server(
    { name: 'glasses', version: '0.1.0' },
    {
      capabilities: {
        experimental: {
          'claude/channel': {},
          // Anyone holding the bridge token can approve tool calls once this is on.
          ...(opts.permissionRelay ? { 'claude/channel/permission': {} } : {}),
        },
        tools: {},
      },
      instructions: INSTRUCTIONS,
    },
  )

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [
      {
        name: 'reply',
        description: 'Answer a request from the glasses. The text is read aloud to the user.',
        inputSchema: {
          type: 'object',
          properties: {
            chat_id: { type: 'string', description: 'The chat_id from the <channel> tag you are answering' },
            text: { type: 'string', description: 'One to three short spoken sentences, plain text' },
          },
          required: ['chat_id', 'text'],
        },
      },
    ],
  }))

  server.setRequestHandler(CallToolRequestSchema, async req => {
    if (req.params.name !== 'reply') throw new Error(`unknown tool: ${req.params.name}`)
    const { chat_id, text } = (req.params.arguments ?? {}) as { chat_id?: unknown; text?: unknown }
    if (typeof chat_id !== 'string' || typeof text !== 'string' || !text.trim()) {
      return { isError: true, content: [{ type: 'text', text: 'reply needs a chat_id and non-empty text' }] }
    }
    const live = hub.isWaiting(chat_id)
    hub.deliverReply(chat_id, text.trim())
    return {
      content: [{ type: 'text', text: live ? 'sent' : 'queued: the user will hear it on their next call' }],
    }
  })

  if (opts.permissionRelay) {
    server.setNotificationHandler(PermissionRequestSchema, async ({ params }) => {
      hub.openPermission(params)
    })
  }

  return {
    server,
    async push(content, meta) {
      await server.notification({ method: 'notifications/claude/channel', params: { content, meta } })
    },
    async verdict(request_id, behavior) {
      await server.notification({ method: 'notifications/claude/channel/permission', params: { request_id, behavior } })
      hub.closePermission(request_id, behavior)
    },
  }
}
