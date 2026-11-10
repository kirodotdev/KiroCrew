// The reconnect backoff rule (healthy-open threshold, jitter) has one owner,
// `utils/reconnectBackoff.ts`. The dashboard socket and the two app sockets all
// import it; a second definition drifts silently from the first.

import { describe, it, expect } from 'vitest'
import { join } from 'node:path'
import { readSource } from './readSource'
import * as shared from '../utils/reconnectBackoff'
import * as facade from '../hooks/useWebSocket'

const CONNECTION = join(__dirname, '..', 'hooks', 'websocket', 'connection.ts')

describe('the reconnect backoff rule is written once', () => {
  it('connection.ts defines neither the threshold nor the jitter', () => {
    const src = readSource(CONNECTION)
    expect(src).not.toContain('const RECONNECT_HEALTHY_MS')
    expect(src).not.toContain('function jittered')
    expect(src).toContain('utils/reconnectBackoff')
  })

  it('the facade re-exports the shared threshold, not a copy', () => {
    expect(facade.RECONNECT_HEALTHY_MS).toBe(shared.RECONNECT_HEALTHY_MS)
  })
})
