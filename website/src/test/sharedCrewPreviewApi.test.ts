/**
 * `api.sharedCrewPreview`'s one benign denial: the 403 `owner_only` a caller that is not
 * the dashboard owner gets. Feature Previews hides the card on it, on every visit, so it
 * must not reach the error journal; a 404 still reads as "not available".
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
    url: '/api/shared-crew/preview',
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
  }) as unknown as Response

describe('api.sharedCrewPreview', () => {
  it('does NOT journal the 403 owner_only from a non-owner caller', async () => {
    fetchMock.mockResolvedValue(failing(403, { error: 'owner authorization required', code: 'owner_only' }))
    await expect(api.sharedCrewPreview()).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(0)
  })

  it('DOES journal any other failure, a 403 without that code included', async () => {
    fetchMock.mockResolvedValue(failing(500, { error: 'boom', code: 'internal' }))
    await expect(api.sharedCrewPreview()).rejects.toBeInstanceOf(ApiError)
    fetchMock.mockResolvedValue(failing(403, { error: 'forbidden' }))
    await expect(api.sharedCrewPreview()).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors().map((e) => e.status)).toEqual(expect.arrayContaining([500, 403]))
    expect(recentErrors()).toHaveLength(2)
  })

  it('still resolves null on a 404, without journaling', async () => {
    fetchMock.mockResolvedValue(failing(404, { error: 'not found' }))
    await expect(api.sharedCrewPreview()).resolves.toBeNull()
    expect(recentErrors()).toHaveLength(0)
  })
})
