/**
 * All four app API clients must surface a refusal as the shared typed error.
 *
 * Before this, each threw `new Error(body || \`HTTP ${status}\`)`, so a refusal
 * body reached the UI as raw wire JSON and no caller could branch on the status.
 * These assertions are per-client on purpose: the bug was four independent
 * copies of the same mistake, so one shared-helper test would not catch a single
 * client drifting back.
 */
import { ApiError } from '../api/apiError'
import { api as designTweakApi } from '../apps/design-tweak/api'
import { fileExplorerApi } from '../apps/file-explorer/api'
import { designCritiqueApi } from '../apps/design-critique/api'
import { getWatchlist } from '../apps/mochi/api'

/** A refusal whose body is the JSON envelope the backends actually send. */
const REFUSAL = JSON.stringify({ error: 'path is outside the sandbox' })

const refuse = (status = 403, body = REFUSAL) =>
  vi.fn().mockResolvedValue({
    ok: false,
    status,
    headers: { get: () => null },
    text: () => Promise.resolve(body),
    json: () => Promise.reject(new Error('should not be parsed on a refusal')),
  } as unknown as Response)

/** Assert the shared contract: typed, status-bearing, message already unwrapped. */
async function expectTypedRefusal(call: () => Promise<unknown>): Promise<void> {
  await expect(call()).rejects.toThrow(ApiError)
  const e = await call().catch((err: unknown) => err)
  const apiErr = e as ApiError
  expect(apiErr.status).toBe(403)
  // The human message, not the envelope — this is what a toast renders.
  expect(apiErr.message).toBe('path is outside the sandbox')
  expect(apiErr.message).not.toContain('{"error"')
  // The envelope stays reachable for a caller that wants structured fields.
  expect(apiErr.body).toBe(REFUSAL)
}

describe('app API clients surface refusals as ApiError', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('design-tweak', async () => {
    vi.stubGlobal('fetch', refuse())
    await expectTypedRefusal(() => designTweakApi.get('/projects'))
  })

  it('file-explorer', async () => {
    vi.stubGlobal('fetch', refuse())
    await expectTypedRefusal(() => fileExplorerApi.health())
  })

  it('design-critique', async () => {
    vi.stubGlobal('fetch', refuse())
    await expectTypedRefusal(() => designCritiqueApi.getSlot('slot-1'))
  })

  it('mochi', async () => {
    vi.stubGlobal('fetch', refuse())
    await expectTypedRefusal(() => getWatchlist())
  })

  it('relabels a 429 with the tunnel-edge message instead of the body', async () => {
    // NOT purely additive, and worth pinning as its own case: friendlyErrText's
    // 429 branch replaces the body outright, because a dashboard behind API
    // Gateway gets the opaque {"message":"Rate exceeded","throttlingReasons":null}
    // and rendering that verbatim is useless. So a 429 through any of these four
    // clients now reads as the rate-limit sentence, where before it showed the
    // raw throttle envelope.
    vi.stubGlobal('fetch', refuse(429, '{"message":"Rate exceeded","throttlingReasons":null}'))
    const e = (await fileExplorerApi.health().catch((err: unknown) => err)) as ApiError
    expect(e.status).toBe(429)
    expect(e.message).toContain('Rate limited')
    expect(e.message).not.toContain('throttlingReasons')
    // The original envelope is still on .body for diagnostics.
    expect(e.body).toContain('throttlingReasons')
  })

  it('all four keep the HTTP <status> fallback for an empty body', async () => {
    // The bare-Error calls used `body || HTTP <status>`, so an empty-body refusal
    // must read exactly as it did before the change.
    for (const call of [
      () => designTweakApi.get('/projects'),
      () => fileExplorerApi.health(),
      () => designCritiqueApi.getSlot('slot-1'),
      () => getWatchlist(),
    ]) {
      vi.stubGlobal('fetch', refuse(500, ''))
      const e = (await call().catch((err: unknown) => err)) as ApiError
      expect(e).toBeInstanceOf(ApiError)
      expect(e.message).toBe('HTTP 500')
    }
  })
})
