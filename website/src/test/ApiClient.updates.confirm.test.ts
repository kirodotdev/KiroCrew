/**
 * One confirmation for a restart that would cut running work.
 *
 * `restartGateway` is the only call that can restart the gateway: Update only
 * ARMS the update for the next start. The gateway answers a restart under running
 * work with `409 work_in_flight`; the client wrapper shows the counts once and
 * sends `force: true` only after the person says yes. These tests drive the real
 * client against a fetch double, so a surface that calls it gets the dialog
 * without carrying any of it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, ApiError, __resetAuthRecoveryStateForTests } from '../api/client'
import { WorkConfirmDeclined } from '../utils/workInFlight'
import { i18nT } from '../i18n/t'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { confirmText, stubAppConfirm } from './appConfirmStub'

function res(status: number, body: unknown): Response {
  const text = JSON.stringify(body)
  return {
    ok: status >= 200 && status < 300,
    status,
    url: 'http://localhost:6776/api/probe',
    headers: { get: () => null },
    json: async () => JSON.parse(text),
    text: async () => text,
    clone: () => res(status, body),
  } as unknown as Response
}

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  __resetAuthRecoveryStateForTests()
  __resetErrorJournalForTests()
})

afterEach(() => {
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
})

const posted = () => fetchMock.mock.calls.map(([url, init]) => ({
  url: url as string,
  body: (init as RequestInit | undefined)?.body ? JSON.parse(String((init as RequestInit).body)) : undefined,
}))

const WORK = { code: 'work_in_flight', busy: 3, turns: 2, quiet_remaining_secs: 0, force_available: true }

describe('restartGateway', () => {
  it('asks without force first and never confirms when nothing is running', async () => {
    const ask = stubAppConfirm(true)
    fetchMock.mockResolvedValue(res(200, { ok: true }))

    await expect(api.restartGateway()).resolves.toEqual({ ok: true })

    expect(posted()).toEqual([{ url: '/api/restart', body: { force: false } }])
    expect(ask).not.toHaveBeenCalled()
  })

  it('shows what is running once, and sends force only after the person confirms', async () => {
    const ask = stubAppConfirm(true)
    fetchMock
      .mockResolvedValueOnce(res(409, { ...WORK, error: 'busy' }))
      .mockResolvedValueOnce(res(200, { ok: true }))

    await expect(api.restartGateway()).resolves.toEqual({ ok: true })

    expect(posted()).toEqual([
      { url: '/api/restart', body: { force: false } },
      { url: '/api/restart', body: { force: true } },
    ])
    expect(ask).toHaveBeenCalledTimes(1)
    const text = confirmText(ask.mock.calls[0][0])
    expect(text).toContain(i18nT('components.updateFoundModal.work_running_title'))
    expect(text).toContain(i18nT('components.updateFoundModal.work_running_busy', { active: 3 }))
    expect(ask.mock.calls[0][0].confirmLabel).toBe(i18nT('components.updateFoundModal.restart_anyway'))
  })

  it('sends nothing more when the person declines, and says it was cancelled', async () => {
    stubAppConfirm(false)
    fetchMock.mockResolvedValue(res(409, WORK))

    const err = await api.restartGateway().catch((e: unknown) => e)

    expect(err).toBeInstanceOf(WorkConfirmDeclined)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as Error).message).toBe(i18nT('components.updateFoundModal.restart_cancelled'))
    expect(posted()).toEqual([{ url: '/api/restart', body: { force: false } }])
  })

  it('declines when no dialog host is mounted: the restart is not taken', async () => {
    fetchMock.mockResolvedValue(res(409, WORK))

    const err = await api.restartGateway().catch((e: unknown) => e)

    expect(err).toBeInstanceOf(WorkConfirmDeclined)
    expect(posted()).toHaveLength(1)
  })

  it('leaves every other 409 an ordinary refusal, with no dialog', async () => {
    const ask = stubAppConfirm(true)
    fetchMock.mockResolvedValue(res(409, { error: 'restart pending', code: 'restart_pending' }))

    const err = await api.restartGateway().catch((e: unknown) => e)

    expect(err).toBeInstanceOf(ApiError)
    expect(err).not.toBeInstanceOf(WorkConfirmDeclined)
    expect(ask).not.toHaveBeenCalled()
  })

  it('names the minutes owed when only a recent chat holds the restart', async () => {
    const ask = stubAppConfirm(false)
    fetchMock.mockResolvedValue(res(409, { code: 'work_in_flight', busy: 0, quiet_remaining_secs: 600 }))

    await api.restartGateway().catch(() => undefined)

    expect(confirmText(ask.mock.calls[0][0])).toContain(
      i18nT('components.updateFoundModal.work_running_quiet', { minutes: 10 }),
    )
  })
})

describe('applyUpdate', () => {
  it('arms the update for the next start and never confirms, whatever is running', async () => {
    const ask = stubAppConfirm(true)
    fetchMock.mockResolvedValue(res(202, { armed: true, version: '9.9.9' }))

    await expect(api.applyUpdate()).resolves.toEqual({ armed: true, version: '9.9.9' })

    expect(posted()).toEqual([{ url: '/api/update', body: { when: 'next-start' } }])
    expect(ask).not.toHaveBeenCalled()
  })

  it('never sends force, and never restarts on its own', async () => {
    fetchMock.mockResolvedValue(res(202, { armed: true, version: '9.9.9' }))

    await api.applyUpdate()

    expect(posted().every(p => p.url === '/api/update' && !('force' in (p.body ?? {})))).toBe(true)
  })

  it('disarms with when: cancel', async () => {
    fetchMock.mockResolvedValue(res(200, { ok: true, armed: false }))

    await expect(api.disarmUpdate()).resolves.toEqual({ ok: true, armed: false })

    expect(posted()).toEqual([{ url: '/api/update', body: { when: 'cancel' } }])
  })
})
