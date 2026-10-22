/**
 * The crewmate capabilities client: each method's route and body. The editor
 * suites mock this module whole, so these are the only calls through the real
 * request builders.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const transport = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  j: vi.fn((r: unknown) => r),
}))
vi.mock('../api/apiTransport', () => ({ apiTransport: transport }))

import {
  crewCapabilitiesApi, crewCapabilitiesKey, capabilityKey, crewWorkerKitApi, crewWorkerKitKey, type CapabilityDraft,
} from '../api/crewCapabilities'

const draft: CapabilityDraft = { revision: 'r1', enroll: false, operations: [], accept_parent: [], accept_members: [] }

beforeEach(() => {
  for (const fn of [transport.get, transport.post, transport.put]) fn.mockReset().mockResolvedValue({ ok: true })
})

describe('crewCapabilitiesApi', () => {
  it('reads the member capability view, escaping the name', async () => {
    await expect(crewCapabilitiesApi.get('on call/1')).resolves.toEqual({ ok: true })
    expect(transport.get).toHaveBeenCalledWith('/api/agents/on%20call%2F1/capabilities')
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
    const body = { template: 'release-bot', keep_additions: true }
    await expect(crewWorkerKitApi.get('on call')).resolves.toEqual({ ok: true })
    expect(transport.get).toHaveBeenCalledWith('/api/agents/on%20call/worker-kit')
    await crewWorkerKitApi.preview('pixel', body)
    expect(transport.post).toHaveBeenCalledWith('/api/agents/pixel/worker-kit/preview', body)
    await crewWorkerKitApi.apply('pixel', { ...body, revision: 'r2' })
    expect(transport.put).toHaveBeenCalledWith('/api/agents/pixel/worker-kit', { ...body, revision: 'r2' })
    expect(crewWorkerKitKey('pixel')).toEqual(['crew-worker-kit', 'pixel'])
  })
})
