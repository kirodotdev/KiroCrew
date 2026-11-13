/**
 * The crewmate capabilities client: each method's route and body, and the
 * deadline on the two reads the Abilities pane waits on. The editor suites mock
 * this module whole, so these are the only calls through the real request
 * builders.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const transport = vi.hoisted(() => ({
  get: vi.fn(),
  getWithSignal: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  j: vi.fn((r: unknown) => r),
}))
vi.mock('../api/apiTransport', () => ({ apiTransport: transport }))

import {
  ABILITIES_READ_TIMEOUT_MS, crewCapabilitiesApi, crewCapabilitiesKey, capabilityKey, crewWorkerKitApi, crewWorkerKitKey,
  type CapabilityDraft,
} from '../api/crewCapabilities'

const draft: CapabilityDraft = { revision: 'r1', enroll: false, operations: [], accept_parent: [], accept_members: [] }

beforeEach(() => {
  for (const fn of [transport.get, transport.getWithSignal, transport.post, transport.put]) {
    fn.mockReset().mockResolvedValue({ ok: true })
  }
})

describe('crewCapabilitiesApi', () => {
  it('reads the member capability view, escaping the name', async () => {
    await expect(crewCapabilitiesApi.get('on call/1')).resolves.toEqual({ ok: true })
    expect(transport.getWithSignal).toHaveBeenCalledWith('/api/agents/b64/b24gY2FsbC8x/capabilities', expect.any(AbortSignal))
  })

  it('posts a draft to the preview route', async () => {
    await crewCapabilitiesApi.preview('oncall', draft)
    expect(transport.post).toHaveBeenCalledWith('/api/agents/oncall/capabilities/preview', draft)
  })

  it('puts a previewed draft, token included, to save it', async () => {
    const reviewed = { ...draft, preview_token: 'tok' }
    await crewCapabilitiesApi.save('oncall', reviewed)
    expect(transport.put).toHaveBeenCalledWith('/api/agents/oncall/capabilities', reviewed)
  })

  it('keys the query by member and a row by section and id', () => {
    expect(crewCapabilitiesKey('oncall')).toEqual(['crew-capabilities', 'oncall'])
    expect(capabilityKey({ section: 'autoApprove', id: 'git' })).toBe('["autoApprove","git"]')
  })
})

describe('crewWorkerKitApi', () => {
  it('reads, previews and applies the worker kit, escaping the name', async () => {
    const body = { template: 'release-bot' }
    await expect(crewWorkerKitApi.get('on call')).resolves.toEqual({ ok: true })
    expect(transport.getWithSignal).toHaveBeenCalledWith('/api/agents/b64/b24gY2FsbA/worker-kit', expect.any(AbortSignal))
    await crewWorkerKitApi.preview('pixel', body)
    expect(transport.post).toHaveBeenCalledWith('/api/agents/pixel/worker-kit/preview', body)
    await crewWorkerKitApi.apply('pixel', { ...body, revision: 'r2' })
    expect(transport.put).toHaveBeenCalledWith('/api/agents/pixel/worker-kit', { ...body, revision: 'r2' })
    expect(crewWorkerKitKey('pixel')).toEqual(['crew-worker-kit', 'pixel'])
  })
})

describe('the Abilities pane reads', () => {
  // A request that never answers and, like `fetch`, rejects with its signal's
  // reason once aborted: what a hung mount behind the gateway looks like here.
  const neverAnswers = (_url: string, signal?: AbortSignal) =>
    new Promise<Response>((_resolve, reject) => signal?.addEventListener('abort', () => reject(signal.reason)))

  beforeEach(() => {
    vi.useFakeTimers()
    transport.get.mockImplementation(neverAnswers)
    transport.getWithSignal.mockImplementation(neverAnswers)
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it.each([
    ['capabilities', () => crewCapabilitiesApi.get('oncall')],
    ['worker-kit', () => crewWorkerKitApi.get('oncall')],
  ])('a %s read that never answers rejects with the deadline error', async (_name, read) => {
    const settled = vi.fn()
    void read().then(settled, settled)
    await vi.advanceTimersByTimeAsync(ABILITIES_READ_TIMEOUT_MS)
    expect(settled).toHaveBeenCalledWith(expect.objectContaining({ name: 'TimeoutError' }))
  })
})
