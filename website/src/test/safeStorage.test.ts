import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { safeGetItem, safeSetItem, safeGetSessionItem, safeSetSessionItem, isQuotaExceededError } from '../utils/safeStorage'

/** Build a DOMException that looks like a browser quota error. */
function quotaError(name = 'QuotaExceededError', code = 22): DOMException {
  // jsdom's DOMException constructor sets `name`; `code` is derived from the
  // legacy name table, so for custom names we override it explicitly.
  const e = new DOMException('quota', name)
  Object.defineProperty(e, 'code', { value: code, configurable: true })
  return e
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('isQuotaExceededError', () => {
  it('recognizes the Chrome/Safari QuotaExceededError', () => {
    expect(isQuotaExceededError(quotaError('QuotaExceededError', 22))).toBe(true)
  })

  it('recognizes the Firefox NS_ERROR_DOM_QUOTA_REACHED (code 1014)', () => {
    expect(isQuotaExceededError(quotaError('NS_ERROR_DOM_QUOTA_REACHED', 1014))).toBe(true)
  })

  it('rejects non-quota errors', () => {
    expect(isQuotaExceededError(new Error('nope'))).toBe(false)
    expect(isQuotaExceededError(quotaError('SecurityError', 18))).toBe(false)
    expect(isQuotaExceededError(undefined)).toBe(false)
  })
})

describe('safeSetItem', () => {
  it('writes through to localStorage on the happy path', () => {
    expect(safeSetItem('k', 'v')).toBe(true)
    expect(window.localStorage.getItem('k')).toBe('v')
  })

  it('never throws and returns false when storage rejects with a non-quota error', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError('SecurityError', 18)
    })
    expect(() => safeSetItem('k', 'v')).not.toThrow()
    expect(safeSetItem('k', 'v')).toBe(false)
    spy.mockRestore()
  })

  it('reclaims disposable height caches and retries when quota is exceeded', () => {
    // Seed a couple of disposable height-cache entries that reclaim should drop.
    window.localStorage.setItem('vc_heights_session-A', '{"a":1}')
    window.localStorage.setItem('vc_heights_session-B', '{"b":2}')
    window.localStorage.setItem('keep-me', 'important')

    // First setItem call throws quota; once the height caches are removed the
    // retry succeeds. We simulate that by failing only while the height keys
    // are still present.
    const real = Storage.prototype.setItem
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      const heightKeysPresent =
        this.getItem('vc_heights_session-A') !== null ||
        this.getItem('vc_heights_session-B') !== null
      if (heightKeysPresent && !key.startsWith('vc_heights_')) {
        throw quotaError()
      }
      real.call(this, key, value)
    })

    const ok = safeSetItem('new-key', 'new-value')

    expect(ok).toBe(true)
    expect(window.localStorage.getItem('new-key')).toBe('new-value')
    // Disposable caches were reclaimed...
    expect(window.localStorage.getItem('vc_heights_session-A')).toBeNull()
    expect(window.localStorage.getItem('vc_heights_session-B')).toBeNull()
    // ...but non-disposable data was preserved.
    expect(window.localStorage.getItem('keep-me')).toBe('important')
    spy.mockRestore()
  })

  it('returns false (without throwing) when quota persists after reclaim', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError()
    })
    // No height caches to reclaim → no retry → swallow and report failure.
    expect(() => safeSetItem('k', 'v')).not.toThrow()
    expect(safeSetItem('k', 'v')).toBe(false)
    spy.mockRestore()
  })

  it('escalates past tier 1 to evict mc-paste-store-v1 when height caches are absent', () => {
    // The real-world failure mode behind the looping KIROCREW boot reveal: the
    // quota hog is the multi-MB mc-paste-store-v1 side table, and there are no
    // vc_heights_* keys to reclaim. Single-tier reclaim freed nothing and the
    // write was silently lost; tier escalation must drop mc-paste-store-v1.
    window.localStorage.setItem('mc-paste-store-v1', 'x'.repeat(2000))

    const real = Storage.prototype.setItem
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      // Fail until the paste store is gone (and never block its own removal path).
      if (this.getItem('mc-paste-store-v1') !== null && key !== 'mc-paste-store-v1') {
        throw quotaError()
      }
      real.call(this, key, value)
    })

    const ok = safeSetItem('mc-nav', '1')

    expect(ok).toBe(true)
    expect(window.localStorage.getItem('mc-paste-store-v1')).toBeNull()
    expect(window.localStorage.getItem('mc-nav')).toBe('1')
    spy.mockRestore()
  })

  it('reclaims touched-files lists as part of tier 2', () => {
    window.localStorage.setItem('kirocrew:touched-files:chat-1-100', '["a.ts"]')

    const real = Storage.prototype.setItem
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      if (this.getItem('kirocrew:touched-files:chat-1-100') !== null
        && !key.startsWith('kirocrew:touched-files:')) {
        throw quotaError()
      }
      real.call(this, key, value)
    })

    expect(safeSetItem('k', 'v')).toBe(true)
    expect(window.localStorage.getItem('kirocrew:touched-files:chat-1-100')).toBeNull()
    spy.mockRestore()
  })

  it('never evicts user drafts or config — returns false when only those remain', () => {
    // Quota is permanently exhausted and nothing reclaimable exists. The write
    // fails, but unsaved user input and config MUST survive untouched.
    window.localStorage.setItem('mc-chat-drafts', 'unsaved user input')
    window.localStorage.setItem('mc-onboarded', '1')
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError()
    })

    expect(safeSetItem('k', 'v')).toBe(false)
    spy.mockRestore()

    expect(window.localStorage.getItem('mc-chat-drafts')).toBe('unsaved user input')
    expect(window.localStorage.getItem('mc-onboarded')).toBe('1')
  })

  it('preserves the :toolClearedAt watermark while reclaiming touched-files lists', () => {
    // The watermark (useTouchedFiles) shares the touched-files prefix but must
    // survive a tier-2 sweep — evicting it would resurface previously-cleared
    // agent-touched files. Only the list entry should be reclaimed.
    window.localStorage.setItem('kirocrew:touched-files:chat-1-100', '["a.ts"]')
    window.localStorage.setItem('kirocrew:touched-files:chat-1-100:toolClearedAt', '1700000000000')

    const real = Storage.prototype.setItem
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      // Fail until the (non-watermark) list entry is gone.
      if (this.getItem('kirocrew:touched-files:chat-1-100') !== null
        && !key.startsWith('kirocrew:touched-files:')) {
        throw quotaError()
      }
      real.call(this, key, value)
    })

    expect(safeSetItem('k', 'v')).toBe(true)
    expect(window.localStorage.getItem('kirocrew:touched-files:chat-1-100')).toBeNull()
    // Watermark survived the sweep.
    expect(window.localStorage.getItem('kirocrew:touched-files:chat-1-100:toolClearedAt'))
      .toBe('1700000000000')
    spy.mockRestore()
  })

  it('terminates (no infinite loop) when retries keep failing after reclaim', () => {
    // A reclaimable tier-1 key exists, but the write fails even after it is
    // dropped. The escalate-and-retry loop must end once tiers are exhausted.
    window.localStorage.setItem('vc_heights_session-A', '{"a":1}')
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError()
    })

    expect(() => safeSetItem('k', 'v')).not.toThrow()
    expect(safeSetItem('k', 'v')).toBe(false)
    spy.mockRestore()
    // The reclaimable key was still dropped during the attempt.
    expect(window.localStorage.getItem('vc_heights_session-A')).toBeNull()
  })

  it('terminates even if removeItem silently no-ops (structural tier bound)', () => {
    // Heimdall edge case: a Storage backend whose removeItem returns without
    // removing and without throwing. The reclaim loop bounds iterations by
    // RECLAIM_TIERS.length, so it terminates regardless of removeItem
    // semantics — a naive `while (reclaimSpace())` would spin forever because
    // the same key keeps matching every pass. setItem always throws quota →
    // the write never succeeds.
    window.localStorage.setItem('vc_heights_session-A', '{"a":1}')
    window.localStorage.setItem('mc-paste-store-v1', 'x'.repeat(100))
    const setSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError()
    })
    // removeItem no-ops: matched keys stay present on every reclaim pass, so a
    // pre-fix loop relying on "did we remove something" would never exit.
    const removeSpy = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {})

    expect(() => safeSetItem('k', 'v')).not.toThrow()
    expect(safeSetItem('k', 'v')).toBe(false)
    setSpy.mockRestore()
    removeSpy.mockRestore()
  })
})

describe('safeSetSessionItem', () => {
  it('writes through to sessionStorage on the happy path', () => {
    expect(safeSetSessionItem('k', 'v')).toBe(true)
    expect(window.sessionStorage.getItem('k')).toBe('v')
  })

  it('never throws and returns false when sessionStorage rejects', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw quotaError()
    })
    expect(() => safeSetSessionItem('k', 'v')).not.toThrow()
    expect(safeSetSessionItem('k', 'v')).toBe(false)
    spy.mockRestore()
  })
})

/**
 * The storage-DENIED platform, which is different from the storage-rejects-a-write
 * platform above. Chrome with cookies blocked (and a sandboxed iframe) makes the
 * global `sessionStorage` / `localStorage` accessor itself throw SecurityError.
 * `typeof` does NOT suppress that: it only suppresses ReferenceError for an
 * undeclared identifier, so a `typeof x === 'undefined'` availability probe
 * sitting outside the try/catch throws on exactly the platform it exists to
 * survive — taking the calling component's render down with it.
 */
describe('storage-denied platform (global accessor throws)', () => {
  /** Replace a storage global with a throwing accessor for one test. */
  function denyStorage(name: 'localStorage' | 'sessionStorage'): () => void {
    const original = Object.getOwnPropertyDescriptor(window, name)
    Object.defineProperty(window, name, {
      configurable: true,
      get() {
        throw new DOMException('access denied', 'SecurityError')
      },
    })
    return () => {
      if (original) Object.defineProperty(window, name, original)
      else delete (window as unknown as Record<string, unknown>)[name]
    }
  }

  it('safeGetSessionItem returns null instead of throwing', () => {
    const restore = denyStorage('sessionStorage')
    try {
      expect(() => safeGetSessionItem('k')).not.toThrow()
      expect(safeGetSessionItem('k')).toBeNull()
    } finally {
      restore()
    }
  })

  it('safeSetSessionItem returns false instead of throwing', () => {
    const restore = denyStorage('sessionStorage')
    try {
      expect(() => safeSetSessionItem('k', 'v')).not.toThrow()
      expect(safeSetSessionItem('k', 'v')).toBe(false)
    } finally {
      restore()
    }
  })

  it('safeGetItem returns null instead of throwing', () => {
    const restore = denyStorage('localStorage')
    try {
      expect(() => safeGetItem('k')).not.toThrow()
      expect(safeGetItem('k')).toBeNull()
    } finally {
      restore()
    }
  })

  it('safeSetItem returns false instead of throwing', () => {
    const restore = denyStorage('localStorage')
    try {
      expect(() => safeSetItem('k', 'v')).not.toThrow()
      expect(safeSetItem('k', 'v')).toBe(false)
    } finally {
      restore()
    }
  })
})
