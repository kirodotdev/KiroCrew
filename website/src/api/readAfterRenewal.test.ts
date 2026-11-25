import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './apiError'
import { pendingRefresh, type RefreshResult } from './refreshOnce'
import { readAfterRenewal } from './readAfterRenewal'

vi.mock('./refreshOnce', async importOriginal => ({
  ...(await importOriginal<typeof import('./refreshOnce')>()),
  pendingRefresh: vi.fn(),
}))

const renewal = vi.mocked(pendingRefresh)

// What the gateway answers once the dashboard sign-in has run out.
const signInRefused = () => new ApiError(403, 'Token required', '', true)
const renewed = (ok: boolean): RefreshResult => ({ ok, status: ok ? 200 : 401, body: null })

// Waiting for a renewal still running is covered where the two reads use it:
// App.shell.mobileConnect.test.tsx and milestones.test.tsx.
describe('readAfterRenewal', () => {
  beforeEach(() => { renewal.mockReset().mockReturnValue(null) })

  it('reads once more at once when the renewal has already answered', async () => {
    const read = vi.fn<() => Promise<string>>().mockRejectedValueOnce(signInRefused()).mockResolvedValue('ok')
    await expect(readAfterRenewal(read)).resolves.toBe('ok')
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('does not read again when the renewal failed', async () => {
    renewal.mockReturnValue(Promise.resolve(renewed(false)))
    const refusal = signInRefused()
    const read = vi.fn<() => Promise<string>>().mockRejectedValueOnce(refusal).mockResolvedValue('ok')
    await expect(readAfterRenewal(read)).rejects.toBe(refusal)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('tries one more time only, so a sign-in that stays refused ends as a failed read', async () => {
    const read = vi.fn<() => Promise<string>>().mockRejectedValue(signInRefused())
    await expect(readAfterRenewal(read)).rejects.toBeInstanceOf(ApiError)
    expect(read).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['a gateway error', () => new Error('502')],
    ['a proxy sign-in page, which no renewal clears', () => new ApiError(403, 'sign in', '', true, true)],
  ])('does not read again after %s', async (_name, failure) => {
    const error = failure()
    const read = vi.fn<() => Promise<string>>().mockRejectedValueOnce(error).mockResolvedValue('ok')
    await expect(readAfterRenewal(read)).rejects.toBe(error)
    expect(read).toHaveBeenCalledTimes(1)
    expect(renewal).not.toHaveBeenCalled()
  })
})
