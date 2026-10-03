import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { installApiTransport, type ApiTransport } from './apiTransport'
import {
  GUIDE_PENDING_QUERY_KEY,
  applyGuideUpdate,
  guideApi,
  guideRequestHeaders,
  isGuide,
  mergeGuide,
  type Guide,
} from './guide'
import { TAB_ID } from './tabId'

const guide = (over: Partial<Guide> = {}): Guide => ({
  guide_id: 'g1',
  slot_key: 's1',
  status: 'active',
  revision: 3,
  owner_tab: TAB_ID,
  action_index: 1,
  step_index: 2,
  actions: [],
  reason: null,
  expires_at: null,
  lease_expires_at: null,
  ...over,
})

let answer: unknown
const transport = {
  get: vi.fn(async () => new Response('{}')),
  post: vi.fn(async () => new Response('{}')),
  put: vi.fn(),
  del: vi.fn(),
  patch: vi.fn(),
  j: vi.fn(async () => answer),
  jNullable: vi.fn(),
}

beforeEach(() => {
  vi.clearAllMocks()
  answer = undefined
  installApiTransport(transport as unknown as ApiTransport)
})

describe('guideApi', () => {
  it('reads pending guides, scoped to a slot only when one is given', async () => {
    answer = { guides: [] }
    await expect(guideApi.pending()).resolves.toEqual({ guides: [] })
    expect(transport.get).toHaveBeenLastCalledWith('/api/guide/pending')
    await guideApi.pending('a b')
    expect(transport.get).toHaveBeenLastCalledWith('/api/guide/pending?slot=a%20b')
  })

  it('claims with the write identity, adding take_over only for an explicit takeover', async () => {
    answer = { guide: guide({ revision: 4 }) }
    await expect(guideApi.claim(guide())).resolves.toMatchObject({ revision: 4 })
    expect(transport.post).toHaveBeenLastCalledWith('/api/guide/claim', { guide_id: 'g1', tab_id: TAB_ID, revision: 3 })
    await guideApi.claim(guide(), true)
    expect(transport.post).toHaveBeenLastCalledWith('/api/guide/claim', { guide_id: 'g1', tab_id: TAB_ID, revision: 3, take_over: true })
  })

  it('reports progress for the exact step it observed', async () => {
    answer = guide({ step_index: 3 })
    await expect(guideApi.progress(guide(), 'observed')).resolves.toMatchObject({ step_index: 3 })
    expect(transport.post).toHaveBeenLastCalledWith('/api/guide/progress', {
      guide_id: 'g1', tab_id: TAB_ID, revision: 3, action_index: 1, step_index: 2, outcome: 'observed',
    })
  })

  it('heartbeats and cancels; an answer that is not a guide reads as null', async () => {
    answer = 'nope'
    await expect(guideApi.heartbeat(guide())).resolves.toBeNull()
    expect(transport.post).toHaveBeenLastCalledWith('/api/guide/heartbeat', { guide_id: 'g1', tab_id: TAB_ID, revision: 3 })
    answer = { guide: 'not an object' }
    await expect(guideApi.cancel(guide())).resolves.toBeNull()
    expect(transport.post).toHaveBeenLastCalledWith('/api/guide/cancel', { guide_id: 'g1', tab_id: TAB_ID, revision: 3 })
    answer = null
    await expect(guideApi.cancel(guide())).resolves.toBeNull()
  })
})

describe('guide helpers', () => {
  it('builds the three per-request headers', () => {
    expect(guideRequestHeaders(guide())).toEqual({ 'X-Guide-Id': 'g1', 'X-Guide-Tab': TAB_ID, 'X-Guide-Revision': '3' })
  })

  it('merges by revision: a newer or equal one replaces, an older one is ignored, a new id appends', () => {
    expect(mergeGuide(undefined, guide())).toEqual([guide()])
    const list = [guide()]
    expect(mergeGuide(list, guide({ revision: 2, status: 'cancelled' }))[0].status).toBe('active')
    expect(mergeGuide(list, guide({ revision: 3, status: 'expired' }))[0].status).toBe('expired')
    expect(mergeGuide(list, guide({ guide_id: 'g2' }))).toHaveLength(2)
  })

  it('recognises only a well-formed guide', () => {
    expect(isGuide(guide())).toBe(true)
    expect(isGuide(null)).toBe(false)
    expect(isGuide({ ...guide(), revision: '3' })).toBe(false)
    expect(isGuide({ ...guide(), actions: undefined })).toBe(false)
  })

  it('folds a guide_update frame into the pending cache and ignores a malformed one', () => {
    const qc = new QueryClient()
    applyGuideUpdate(qc, { bogus: true })
    expect(qc.getQueryData(GUIDE_PENDING_QUERY_KEY)).toBeUndefined()
    applyGuideUpdate(qc, guide())
    expect(qc.getQueryData(GUIDE_PENDING_QUERY_KEY)).toEqual([guide()])
  })
})
