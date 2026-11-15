import { act, waitFor } from '@testing-library/react'
import { renderHookWithProviders } from '../test/helpers'
import { useUsageDataState } from './privacyNotice'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { pendingRefresh, type RefreshResult } from '../api/refreshOnce'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: true }),
      beaconStatus: vi.fn(),
      collectionStatus: vi.fn(),
    },
  }
})
vi.mock('../api/refreshOnce', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/refreshOnce')>()),
  pendingRefresh: vi.fn(),
}))

const beaconStatus = vi.mocked(api.beaconStatus)
const collectionStatus = vi.mocked(api.collectionStatus)
const renewal = vi.mocked(pendingRefresh)

// What the gateway answers once the dashboard sign-in has run out.
const signInRefused = () => new ApiError(403, 'Token required', '', true)
const renewed = (ok: boolean): RefreshResult => ({ ok, status: ok ? 200 : 401, body: null })

describe('useUsageDataState, a read refused because the sign-in ran out', () => {
  beforeEach(() => {
    beaconStatus.mockReset().mockResolvedValue({ enabled: false } as never)
    collectionStatus.mockReset().mockResolvedValue({ enabled: true, env_pinned: false } as never)
    renewal.mockReset().mockReturnValue(null)
  })

  it('waits for the renewal the refusal started, then reads once more', async () => {
    let finish: (result: RefreshResult) => void = () => {}
    renewal.mockReturnValue(new Promise<RefreshResult>(resolve => { finish = resolve }))
    collectionStatus.mockRejectedValueOnce(signInRefused())
    const { result } = renderHookWithProviders(() => useUsageDataState(true))

    await waitFor(() => expect(renewal).toHaveBeenCalled())
    await act(async () => {})
    // Held: a second read sent now would carry the same lapsed sign-in.
    expect(collectionStatus).toHaveBeenCalledTimes(1)
    expect(result.current.status).toBe('loading')

    await act(async () => { finish(renewed(true)) })
    await waitFor(() => expect(result.current.status).toBe('ready'))
    expect(result.current.recordingOn).toBe(true)
    expect(collectionStatus).toHaveBeenCalledTimes(2)
  })

  it('reads once more at once when the renewal has already answered', async () => {
    collectionStatus.mockRejectedValueOnce(signInRefused())
    const { result } = renderHookWithProviders(() => useUsageDataState(true))

    await waitFor(() => expect(result.current.status).toBe('ready'))
    expect(collectionStatus).toHaveBeenCalledTimes(2)
  })

  it('does not read again when the renewal failed', async () => {
    renewal.mockReturnValue(Promise.resolve(renewed(false)))
    collectionStatus.mockRejectedValueOnce(signInRefused())
    const { result } = renderHookWithProviders(() => useUsageDataState(true))

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(collectionStatus).toHaveBeenCalledTimes(1)
  })

  it('tries one more time only, so a sign-in that stays refused ends as a failed read', async () => {
    collectionStatus.mockRejectedValue(signInRefused())
    const { result } = renderHookWithProviders(() => useUsageDataState(true))

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(collectionStatus).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['a gateway error', () => new Error('502')],
    ['a proxy sign-in page, which no renewal clears', () => new ApiError(403, 'sign in', '', true, true)],
  ])('does not read again after %s', async (_name, failure) => {
    collectionStatus.mockRejectedValueOnce(failure())
    const { result } = renderHookWithProviders(() => useUsageDataState(true))

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(collectionStatus).toHaveBeenCalledTimes(1)
    expect(renewal).not.toHaveBeenCalled()
  })
})
