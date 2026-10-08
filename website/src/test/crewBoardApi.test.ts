/**
 * `api.crewBoard`'s one benign denial: the 404 `no_ledger` a session that never
 * ran a goal answers. The crewmate resume greeting probes it on every crewmate
 * open, so it must not reach the error journal; every other failure still does.
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
    url: '/api/crew-board?conductor=member-ops',
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
  }) as unknown as Response

describe('api.crewBoard', () => {
  it('does NOT journal the 404 no_ledger: a crewmate with no goal is an answer, not a failure', async () => {
    fetchMock.mockResolvedValue(failing(404, { error: 'This session owns no work ledger.', code: 'no_ledger' }))
    await expect(api.crewBoard('member-ops')).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(0)
  })

  it('DOES journal any other failure, a 404 without that code included', async () => {
    fetchMock.mockResolvedValue(failing(409, { error: 'ledger needs repair', code: 'ledger_dirty' }))
    await expect(api.crewBoard('member-ops')).rejects.toBeInstanceOf(ApiError)
    fetchMock.mockResolvedValue(failing(404, { error: 'not found' }))
    await expect(api.crewBoard('member-ops')).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors().map((e) => e.status)).toEqual(expect.arrayContaining([409, 404]))
    expect(recentErrors()).toHaveLength(2)
  })
})
