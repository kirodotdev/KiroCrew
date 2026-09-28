/**
 * Handler-level coverage for the CSRF stale-origin self-heal wired up in
 * `api/client.ts` (`handleCsrfOriginBlocked` + `clearCsrfOriginReloadFlag`),
 * driven through the installed handler via the leaf `noteCsrfOriginResponse`.
 * The leaf's own detection matrix is covered in `csrfOriginSignal.test.ts`;
 * this file asserts the browser-side behavior the leaf tests cannot see:
 * reload-once, the sessionStorage loop guard, and the banner fallback.
 *
 * `_csrfOriginReloadScheduled` in client.ts latches once per MODULE LOAD, so
 * each case that needs a fresh latch uses `vi.resetModules()` + a re-import.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const CSRF_BODY = 'CSRF check failed: request origin not allowed.'
const FLAG = 'kc_csrf_origin_reloaded'

// happy-dom's window.location.reload is a no-op we can spy on.
let reloadSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.resetModules()
  window.sessionStorage.clear()
  document.body.innerHTML = ''
  reloadSpy = vi.spyOn(window.location, 'reload').mockImplementation(() => {})
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback) => {
    cb(0)
    return 0
  })
  vi.spyOn(window, 'setTimeout').mockImplementation(((fn: () => void) => { fn(); return 0 }) as typeof window.setTimeout)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('client CSRF stale-origin self-heal', () => {
  it('first CSRF 403 sets the loop-guard flag and reloads once', async () => {
    // Importing client.ts installs handleCsrfOriginBlocked as the handler.
    const { noteCsrfOriginResponse } = await import('./csrfOriginSignal')
    await import('./client')

    expect(noteCsrfOriginResponse(403, false, CSRF_BODY)).toBe(true)

    expect(window.sessionStorage.getItem(FLAG)).toBe('1')
    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })

  it('does not reload again when the guard flag is already set — shows the banner', async () => {
    // Pre-seed the flag BEFORE the module loads so the fresh handler sees a
    // prior reload and takes the banner branch instead of reloading.
    window.sessionStorage.setItem(FLAG, '1')
    const { noteCsrfOriginResponse } = await import('./csrfOriginSignal')
    await import('./client')

    expect(noteCsrfOriginResponse(403, false, CSRF_BODY)).toBe(true)

    // Banner branch: no reload, flag cleared so a future transient case can heal.
    expect(reloadSpy).not.toHaveBeenCalled()
    expect(window.sessionStorage.getItem(FLAG)).toBeNull()
    // The actionable banner was rendered into the DOM.
    expect(document.body.textContent ?? '').not.toBe('')
  })

  it('a burst of CSRF 403s in one document reloads only once (in-memory latch)', async () => {
    const { noteCsrfOriginResponse } = await import('./csrfOriginSignal')
    await import('./client')

    noteCsrfOriginResponse(403, false, CSRF_BODY)
    noteCsrfOriginResponse(403, false, CSRF_BODY)
    noteCsrfOriginResponse(403, false, CSRF_BODY)

    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })
})
