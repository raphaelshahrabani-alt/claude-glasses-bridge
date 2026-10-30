#!/usr/bin/env bun
// Claude Code spawns this over stdio as a channel. It also serves the HTTP API
// that a phone app (or VisionClaw's voice worker) calls. See README.md.
import { join } from 'node:path'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { createChannel } from './channel.ts'
import { loadConfig } from './config.ts'
import { type BridgeState, createHandler } from './http.ts'
import { Hub } from './hub.ts'
import { log, logToFile } from './log.ts'

const config = loadConfig()
logToFile(join(config.dataDir, 'bridge.log'))
const parent = parentCommand()
log(`starting: pid ${process.pid}, bun ${Bun.version}, started by pid ${process.ppid} (${parent ?? 'unknown'})`)

/** Every Claude Code process that loads this MCP config spawns a bridge; this says which one. */
function parentCommand(): string | null {
  try {
    return Bun.spawnSync(['ps', '-o', 'command=', '-p', String(process.ppid)]).stdout.toString().trim().slice(0, 300) || null
  } catch {
    return null
  }
}

// A user-scoped MCP server is spawned by every Claude Code process: other
// sessions, background spares (`claude bg-spare`), Agent SDK runs. Only the
// session started with a channels flag delivers our events, so only it may
// hold the port; otherwise a spare that binds first would swallow every
// message. If the parent can't be read, fall back to listening.
const channelSession = parent === null || /--(dangerously-load-development-)?channels\b/.test(parent)

const hub = new Hub()
const channel = createChannel(hub, { permissionRelay: config.permissionRelay })
const state: BridgeState = { claudeConnected: false }

channel.server.oninitialized = () => {
  state.claudeConnected = true
  const client = channel.server.getClientVersion()
  log(`Claude Code connected (${client?.name ?? 'unknown client'} ${client?.version ?? ''})`.trim())
}
channel.server.onerror = err => log(`MCP error: ${err.stack ?? err.message}`)

// When Claude Code exits, go with it; otherwise the HTTP listener would keep
// the port held by an orphan and the next session couldn't bind it. Every
// way out is logged so a dropped connection can be traced in bridge.log.
const exitOn = (reason: string) => () => {
  log(`exiting: ${reason}`)
  process.exit(0)
}
process.stdin.on('end', exitOn('stdin ended'))
process.stdin.on('close', exitOn('stdin closed'))
channel.server.onclose = exitOn('MCP connection closed')
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) process.on(signal, exitOn(`received ${signal}`))
// Stay up on stray errors: one bad request shouldn't drop the channel.
process.on('uncaughtException', err => log(`uncaught exception: ${err.stack ?? err}`))
process.on('unhandledRejection', err => log(`unhandled rejection: ${err instanceof Error ? err.stack : err}`))

await channel.server.connect(new StdioServerTransport())

if (config.listen === 'always' || channelSession) listen()
else log('not listening: this Claude Code process was not started with channels enabled (set GLASSES_LISTEN=always to override)')

function listen(): void {
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
    log(`is another Claude Code session with channels already running the bridge? check with: lsof -i :${config.port}`)
    process.exit(1)
  }
  log(`listening on http://${config.host}:${config.port} (permission relay ${config.permissionRelay ? 'on' : 'off'})`)
}
