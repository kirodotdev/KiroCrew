/**
 * `api.chatSlotEnvironment`'s one benign denial: the 403 `owner_only` a caller that is not
 * the dashboard owner (a Shared Crew guest) gets. The chip and the placement hook handle it
 * as "runs here", on every chat open, so it must not reach the error journal.
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
    url: '/api/chat/slots/chat-1/environment',
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
  }) as unknown as Response

describe('api.chatSlotEnvironment', () => {
  it('does NOT journal the 403 owner_only: a non-owner caller reads "runs here", not a failure', async () => {
    fetchMock.mockResolvedValue(failing(403, { error: 'owner authorization required', code: 'owner_only' }))
    await expect(api.chatSlotEnvironment('chat-1')).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(0)
  })

  it('DOES journal any other failure, a 403 without that code included', async () => {
    fetchMock.mockResolvedValue(failing(500, { error: 'boom', code: 'internal' }))
    await expect(api.chatSlotEnvironment('chat-1')).rejects.toBeInstanceOf(ApiError)
    fetchMock.mockResolvedValue(failing(403, { error: 'forbidden' }))
    await expect(api.chatSlotEnvironment('chat-1')).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors().map((e) => e.status)).toEqual(expect.arrayContaining([500, 403]))
    expect(recentErrors()).toHaveLength(2)
  })

  it('keeps journaling the same refusal on a sibling environment route', async () => {
    fetchMock.mockResolvedValue(failing(403, { error: 'owner authorization required', code: 'owner_only' }))
    await expect(api.chatSlotEnvironmentHealth('chat-1')).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(1)
  })
})
