/**
 * listInstances — the states core distinguishes must survive the bridge.
 *
 * This function used to return a bare array and `[]` on any non-200, which
 * collapsed three answers a user needs told apart: the feature being OFF
 * (`instances.enabled` defaults to false, so the route 403s), being ON but not
 * yet ACTIVE (the SSH manager only exists if the flag was set at gateway
 * startup — core's own comment says the UI should say "restart"), and simply
 * having none configured. These pin that they stay apart.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { listInstances } from '../panel/panelBridge'

const originalFetch = global.fetch

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  })
  global.fetch = fn as unknown as typeof fetch
  return fn
}

describe('listInstances', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => {
    global.fetch = originalFetch
  })

  it('403 means the multi-instance feature is off, NOT "no instances"', async () => {
    mockFetch(403, { error: 'disabled' })
    expect(await listInstances()).toEqual({ state: 'disabled' })
  })

  it('active:false means enabled-but-needs-a-gateway-restart, and still lists', async () => {
    mockFetch(200, { active: false, instances: [{ id: 'a', name: 'A' }] })
    const res = await listInstances()
    expect(res.state).toBe('inactive')
    // The rows must still come through: the user should see WHICH instances are
    // waiting on the restart, not an empty box.
    expect(res.state === 'inactive' && res.instances).toHaveLength(1)
  })

  it('active:true with instances is ready', async () => {
    mockFetch(200, {
      active: true,
      instances: [{ id: 'a', name: 'A', local_port: 7778, status: { state: 'connected' } }],
    })
    const res = await listInstances()
    expect(res.state).toBe('ready')
    expect(res.state === 'ready' && res.instances[0].local_port).toBe(7778)
  })

  it('ready with an empty list is distinct from the feature being off', async () => {
    mockFetch(200, { active: true, instances: [] })
    expect(await listInstances()).toEqual({ state: 'ready', instances: [] })
  })

  it('a bare array body is accepted (older payload shape)', async () => {
    mockFetch(200, [{ id: 'a', name: 'A' }])
    const res = await listInstances()
    expect(res.state).toBe('ready')
    expect(res.state === 'ready' && res.instances).toHaveLength(1)
  })

  it('other non-200s are an error, not a disabled feature', async () => {
    mockFetch(500, {})
    expect(await listInstances()).toEqual({ state: 'error' })
  })

  it('a thrown fetch is an error, never an empty list', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('offline')) as unknown as typeof fetch
    expect(await listInstances()).toEqual({ state: 'error' })
  })

  it('a malformed body degrades to ready-with-nothing rather than throwing', async () => {
    mockFetch(200, { active: true, instances: 'not-an-array' })
    expect(await listInstances()).toEqual({ state: 'ready', instances: [] })
  })
})
