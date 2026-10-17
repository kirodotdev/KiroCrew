/**
 * The file tree's expansion mirror under storage pressure.
 *
 * Both cases here are about the mirror SURVIVING, not about the happy path:
 * the session map already covers a remount, so the only thing `localStorage`
 * buys is persistence across a reload. A write that is silently dropped, or a
 * store that can never be rewritten, costs exactly that and nothing warns.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

import {
  rememberExpandedPaths,
  recallExpandedPaths,
  __resetTreeExpansionMemoryForTests,
} from './treeExpansionMemory'

const KEY = 'mc-files-tree-expanded'
/** A key `safeSetItem`'s tier-1 reclaim is allowed to drop. */
const DISPOSABLE = 'vc_heights_session-abc'

/**
 * What a browser actually throws. It must be a real `DOMException`:
 * `safeStorage.isQuotaExceededError` returns false for anything else, so a
 * plain `Error` subclass merely NAMED `QuotaExceededError` never reaches the
 * reclaim path and this test would pass against a broken fix.
 */
const quotaError = () => new DOMException('quota', 'QuotaExceededError')

beforeEach(() => {
  localStorage.clear()
  __resetTreeExpansionMemoryForTests()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('treeExpansionMemory under storage pressure', () => {
  it('reclaims disposable cache and retries when the origin quota is full', () => {
    // The scenario safeStorage.ts exists for: per-session virtualizer height
    // caches have filled the origin, so the NEXT setItem anywhere throws. A
    // raw setItem in a bare catch loses the write forever even though several
    // MB of re-derivable cache is sitting right there; `safeSetItem` drops a
    // tier and retries.
    localStorage.setItem(DISPOSABLE, 'x'.repeat(64))

    const real = Storage.prototype.setItem
    const setItem = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(function (this: Storage, k: string, v: string) {
        // Full until the disposable cache is gone — which is exactly what the
        // reclaim tier removes.
        if (localStorage.getItem(DISPOSABLE) !== null && k === KEY) {
          throw quotaError()
        }
        return real.call(this, k, v)
      })

    rememberExpandedPaths('/w/proj', ['src', 'src/app'])

    expect(setItem).toHaveBeenCalled()
    // The write landed after reclaim, so a RELOAD still restores the tree.
    expect(localStorage.getItem(KEY)).not.toBeNull()
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({
      '/w/proj': ['src', 'src/app'],
    })
    // Only the disposable tier was sacrificed.
    expect(localStorage.getItem(DISPOSABLE)).toBeNull()
  })

  it('recovers from a corrupt stored value instead of being stuck on it', () => {
    // A value that throws in JSON.parse must not become a permanent dead end:
    // if the read refuses and the write is skipped, the corrupt string stays
    // put and expansion never persists again for the life of the profile.
    localStorage.setItem(KEY, '{not json')

    rememberExpandedPaths('/w/proj', ['src'])

    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({ '/w/proj': ['src'] })

    __resetTreeExpansionMemoryForTests()
    expect(recallExpandedPaths('/w/proj')).toEqual(['src'])
  })

  it('still degrades to session-only memory when storage is unusable', () => {
    // The module's stated tolerance, kept: a private-mode SecurityError is not
    // a quota problem, so nothing is reclaimed and nothing throws — the
    // remount case keeps working off the module-scope map.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })

    expect(() => rememberExpandedPaths('/w/proj', ['src'])).not.toThrow()
    expect(recallExpandedPaths('/w/proj')).toEqual(['src'])
  })
})
