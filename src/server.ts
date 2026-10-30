#!/usr/bin/env bun
// Claude Code spawns this over stdio as a channel. It also serves the HTTP API
// that a phone app (or VisionClaw's voice worker) calls. See README.md.
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { createChannel } from './channel.ts'
import { loadConfig } from './config.ts'
import { type BridgeState, createHandler, log } from './http.ts'
import { Hub } from './hub.ts'

const config = loadConfig()
const hub = new Hub()
const channel = createChannel(hub, { permissionRelay: config.permissionRelay })
const state: BridgeState = { claudeConnected: false }

channel.server.oninitialized = () => {
  state.claudeConnected = true
  log('Claude Code connected')
}

// When Claude Code exits, go with it; otherwise the HTTP listener would keep
// the port held by an orphan and the next session couldn't bind it.
const exit = () => process.exit(0)
process.stdin.on('end', exit)
process.stdin.on('close', exit)
channel.server.onclose = exit

await channel.server.connect(new StdioServerTransport())

try {
  Bun.serve({
    hostname: config.host,
    port: config.port,
    idleTimeout: 0, // requests wait minutes for Claude, and /events streams stay open
    maxRequestBodySize: 16 * 1024 * 1024,
    fetch: createHandler({ hub, channel, config, state }),
  })
} catch (err) {
  log(`could not listen on ${config.host}:${config.port}: ${err instanceof Error ? err.message : err}`)
  log('is another session already running the bridge? check with: lsof -i :' + config.port)
  process.exit(1)
}

log(`listening on http://${config.host}:${config.port} (permission relay ${config.permissionRelay ? 'on' : 'off'})`)
