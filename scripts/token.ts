// Prints the bridge token (creating it on first run) and the address to use.
import { loadConfig } from '../src/config.ts'

const config = loadConfig()
console.log(`url:   http://${config.host}:${config.port}`)
console.log(`token: ${config.token}`)
