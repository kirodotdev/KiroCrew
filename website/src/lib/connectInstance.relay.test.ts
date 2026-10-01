import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The relay-mode branch of connectInstanceInto: auto-warm and renewal must reach
// the tunnel through the ATOMIC connected-only pane issue mode (one openInstancePane
// call with { onlyIfConnected: true }), NEVER through the token-returning connect
// route, so a background issue can neither reconnect after an explicit disconnect
// nor receive a remote token. Force relay mode by overriding resolvePaneMode; keep
// every other paneChannel export real (paneAccessFor, parsePaneEndpoint).
const openInstancePane = vi.fn()
const connectInstance = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    openInstancePane: (...a: unknown[]) => openInstancePane(...a),
    connectInstance: (...a: unknown[]) => connectInstance(...a),
  },
}))
vi.mock('./paneChannel', async (importOriginal) => {
  const real = await importOriginal<typeof import('./paneChannel')>()
  return { ...real, resolvePaneMode: () => ({ kind: 'same-origin-relay', origin: 'https://hub.example' }) }
})

import { connectInstanceInto } from './connectInstance'
import { setWarm } from '../store/instancesSlice'

function captureInfo() {
  const lines: string[] = []
  vi.spyOn(console, 'info').mockImplementation((...args: unknown[]) => {
    lines.push(args.join(' '))
  })
  return lines
}

const RELAY_ENDPOINT = {
  kind: 'same-origin-relay',
  instance_id: 'cd-1',
  documentPath: '/instance-pane/CAP/',
  channel: 'ch',
  protocol: 1,
  leaseExpiresAtEpochMs: 123,
}

describe('connectInstanceInto (same-origin-relay)', () => {
  beforeEach(() => {
    openInstancePane.mockReset()
    connectInstance.mockReset()
  })
  afterEach(() => vi.restoreAllMocks())

  it('auto-warm issues connected-only via openInstancePane and never touches the token route', async () => {
    openInstancePane.mockResolvedValue(RELAY_ENDPOINT)
    const dispatch = vi.fn()
    const st = await connectInstanceInto(dispatch as never, 'cd-1', 'auto-warm', { onlyIfConnected: true })
    expect(openInstancePane).toHaveBeenCalledWith('cd-1', 'same-origin-relay', { onlyIfConnected: true })
    // The token-returning connect route is NEVER called on this path — nothing
    // to reconnect a disconnected tunnel, and no remote token to receive.
    expect(connectInstance).not.toHaveBeenCalled()
    expect(dispatch).toHaveBeenCalledWith(
      setWarm({
        id: 'cd-1',
        conn: {
          kind: 'same-origin-relay',
          documentPath: '/instance-pane/CAP/',
          channel: 'ch',
          protocol: 1,
          leaseExpiresAtEpochMs: 123,
        },
      }),
    )
    expect(st.state).toBe('connected')
    // No token field can be present: the returned status is synthesised, never
    // the connect route's token-bearing body.
    expect((st as { token?: string }).token).toBeUndefined()
  })

  it('a connected-only decline (forward down) warms nothing and reports the real state', async () => {
    const lines = captureInfo()
    // The gateway declined: a 200 whose body is a non-connected status, not a
    // relay endpoint. parsePaneEndpoint rejects it, so warm is left untouched.
    openInstancePane.mockResolvedValue({ instance_id: 'cd-1', state: 'disconnected', code: 'instance_not_connected' })
    const dispatch = vi.fn()
    const st = await connectInstanceInto(dispatch as never, 'cd-1', 'auto-warm', { onlyIfConnected: true })
    expect(openInstancePane).toHaveBeenCalledWith('cd-1', 'same-origin-relay', { onlyIfConnected: true })
    expect(connectInstance).not.toHaveBeenCalled()
    expect(dispatch).not.toHaveBeenCalled()
    expect(st.state).toBe('disconnected')
    expect(lines.some(l => l.includes('warm-declined') && l.includes('not_connected'))).toBe(true)
  })

  it('a plain selection issues connect-or-create (no onlyIfConnected flag)', async () => {
    openInstancePane.mockResolvedValue(RELAY_ENDPOINT)
    const dispatch = vi.fn()
    await connectInstanceInto(dispatch as never, 'cd-1', 'select')
    // Selection/Retry may connect: no connected-only gate on the issue call.
    expect(openInstancePane).toHaveBeenCalledWith('cd-1', 'same-origin-relay', undefined)
  })

  it('Retry passes rebuild through (fresh forwarder), never onlyIfConnected', async () => {
    openInstancePane.mockResolvedValue(RELAY_ENDPOINT)
    const dispatch = vi.fn()
    await connectInstanceInto(dispatch as never, 'cd-1', 'retry', { rebuild: true })
    expect(openInstancePane).toHaveBeenCalledWith('cd-1', 'same-origin-relay', { rebuild: true })
  })
})
