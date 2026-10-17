/**
 * `api.browseRemoteArtifacts`'s one benign denial: the 400 `scope_unsupported` a
 * provider answers for a scope it does not list. The `/artifacts` page probes
 * every discovery provider on each load and moves on to the next scope, so it
 * must not reach the error journal; every other browse failure still does.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import { recentErrors, __resetErrorJournalForTests } from '../utils/errorReport'

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  __resetErrorJournalForTests()
})

const failing = (status: number, body: Record<string, unknown>) =>
  ({
    ok: false,
    status,
    url: '/api/remote-artifacts/companion/browse?scope=mine',
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
  }) as unknown as Response

describe('api.browseRemoteArtifacts', () => {
  it('does NOT journal the 400 scope_unsupported: a scope the provider does not list is an answer', async () => {
    fetchMock.mockResolvedValue(failing(400, { error: 'companion does not support mine listing', code: 'scope_unsupported' }))
    await expect(api.browseRemoteArtifacts('companion')).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(0)
  })

  it('DOES journal any other failure, a 400 without that code included', async () => {
    fetchMock.mockResolvedValue(failing(502, { error: 'credentials expired' }))
    await expect(api.browseRemoteArtifacts('companion')).rejects.toBeInstanceOf(ApiError)
    fetchMock.mockResolvedValue(failing(400, { error: 'companion does not support full-text search' }))
    await expect(api.browseRemoteArtifacts('companion', { q: 'x' })).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors().map((e) => e.status)).toEqual(expect.arrayContaining([502, 400]))
    expect(recentErrors()).toHaveLength(2)
  })
})
