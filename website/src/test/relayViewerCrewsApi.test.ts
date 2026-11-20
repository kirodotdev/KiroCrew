/**
 * `api.relayViewerCrews`'s routine refusals: every dashboard reads the list with no
 * setting and hides it on a signed-out, remote-access-off, not-granted or not-owner answer, so
 * those must not reach the error journal. A server failure and a wrong dev setting
 * still do, and so does a refused browser open, which a click asked for.
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

const failing = (url: string, status: number, code: string) =>
  ({
    ok: false,
    status,
    url,
    headers: { get: () => null },
    text: async () => JSON.stringify({ error: 'refused', code }),
  }) as unknown as Response

describe('api.relayViewerCrews', () => {
  it.each([
    [404, 'RELAY_VIEWER_OFF'], [409, 'SIGNED_OUT'], [403, 'REMOTE_ACCESS_DISABLED'],
    [403, 'CREW_ACCESS_NOT_GRANTED'], [403, 'ACCESS_DENIED'], [403, 'owner_only'],
  ])('does NOT journal its routine %i %s', async (status, code) => {
    fetchMock.mockResolvedValue(failing('/api/relay-viewer/crews', status, code))
    await expect(api.relayViewerCrews()).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(0)
  })

  it.each([[502, 'SERVICE_ERROR'], [409, 'NOT_SET_UP'], [403, 'something_else']])(
    'DOES journal %i %s',
    async (status, code) => {
      fetchMock.mockResolvedValue(failing('/api/relay-viewer/crews', status, code))
      await expect(api.relayViewerCrews()).rejects.toBeInstanceOf(ApiError)
      expect(recentErrors()).toHaveLength(1)
    },
  )

  it('DOES journal a refused browser open', async () => {
    fetchMock.mockResolvedValue(failing('/api/relay-viewer/browser-open', 403, 'CREW_ACCESS_NOT_GRANTED'))
    await expect(api.relayViewerBrowserOpen('a'.repeat(24))).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(1)
  })
})
