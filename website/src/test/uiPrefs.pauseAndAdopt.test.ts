/**
 * uiPrefs — pausing the sync around an import, and adopting the restored host
 * copy on the next load.
 *
 * One topic of the suite `uiPrefs.test.ts` opens; the shared fixtures live in
 * `uiPrefsTestUtils.tsx`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  hydrateUiPrefs,
  needsHydrate,
  flushUiPrefs,
  startUiPrefsSync,
  pauseUiPrefsSync,
  resumeUiPrefsSync,
  adoptHostUiPrefsOnNextLoad,
  markCompositeFieldsDirty,
  __resetUiPrefsSyncForTests,
} from '../lib/uiPrefs'
import {
  SYNCED_KEYS_KEY,
  mockFetch,
  okJson,
  lastPatch,
} from './uiPrefsTestUtils'

describe('uiPrefs: pausing and adopting a restored host copy', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetUiPrefsSyncForTests()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    __resetUiPrefsSyncForTests()
  })

  it('a paused sync uploads nothing, and resuming flushes what changed', async () => {
    mockFetch(() => okJson({ prefs: {} }))
    await hydrateUiPrefs()
    startUiPrefsSync()
    await pauseUiPrefsSync()
    localStorage.setItem('mc-crews-view', 'grid')
    const spy = mockFetch(() => okJson({ prefs: {} }))
    await flushUiPrefs()
    await flushUiPrefs(true) // the pagehide path too
    expect(spy).not.toHaveBeenCalled()

    resumeUiPrefsSync()
    await vi.advanceTimersByTimeAsync(2000)
    expect(lastPatch(spy)).toEqual({ 'mc-crews-view': 'grid' })
  })

  it('pausing waits for a PUT already in flight', async () => {
    mockFetch(() => okJson({ prefs: {} }))
    await hydrateUiPrefs()
    localStorage.setItem('mc-crews-view', 'grid')
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    mockFetch(async () => { await gate; return okJson({ prefs: {} }) })
    const flushing = flushUiPrefs()
    let paused = false
    const pausing = pauseUiPrefsSync().then(() => { paused = true })
    await Promise.resolve()
    expect(paused).toBe(false)
    release()
    await flushing
    await pausing
    expect(paused).toBe(true)
  })

  /** A warm, running sync whose first debounced flush has already gone. */
  async function runningSync() {
    mockFetch(() => okJson({ prefs: {} }))
    await hydrateUiPrefs()
    startUiPrefsSync()
    await vi.advanceTimersByTimeAsync(2000)
  }

  it('pausing uploads a change still inside the debounce window before it stops', async () => {
    await runningSync()
    localStorage.setItem('mc-crews-view', 'grid')
    window.dispatchEvent(new Event('mc-config-changed')) // arms the debounce timer
    const spy = mockFetch(() => okJson({ prefs: {} }))

    await pauseUiPrefsSync()
    expect(lastPatch(spy)).toEqual({ 'mc-crews-view': 'grid' })

    // The debounce it pre-empted does not fire a second upload, and nothing
    // after the pause goes up.
    spy.mockClear()
    localStorage.setItem('mc-crews-view', 'list')
    await vi.advanceTimersByTimeAsync(31_000)
    await flushUiPrefs(true)
    expect(spy).not.toHaveBeenCalled()
  })

  it('pausing uploads a change only the poll would have noticed', async () => {
    await runningSync()
    localStorage.setItem('mc-crews-view', 'grid') // no event: a silent writer
    const spy = mockFetch(() => okJson({ prefs: {} }))
    await pauseUiPrefsSync()
    expect(lastPatch(spy)).toEqual({ 'mc-crews-view': 'grid' })
  })

  it('pausing during a PUT waits it out and still uploads the change made after it started', async () => {
    await runningSync()
    localStorage.setItem('mc-crews-view', 'grid')
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    const spy = mockFetch(async () => { await gate; return okJson({ prefs: {} }) })
    const flushing = flushUiPrefs()
    localStorage.setItem('mc-nav', 'collapsed')

    let done = false
    const pausing = pauseUiPrefsSync().then(() => { done = true })
    await Promise.resolve()
    expect(done).toBe(false)
    release()
    await flushing
    await pausing

    const puts = spy.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
    const sent = Object.assign({}, ...puts.map((c) => JSON.parse((c[1] as RequestInit).body as string).prefs))
    expect(sent).toEqual({ 'mc-crews-view': 'grid', 'mc-nav': 'collapsed' })
  })

  it('a failed flush does not throw out of pause, and the pause still holds', async () => {
    await runningSync()
    localStorage.setItem('mc-crews-view', 'grid')
    mockFetch(() => Promise.reject(new Error('offline')))
    await expect(pauseUiPrefsSync()).resolves.toBeUndefined()

    const spy = mockFetch(() => okJson({ prefs: {} }))
    await flushUiPrefs()
    await flushUiPrefs(true)
    expect(spy).not.toHaveBeenCalled()
  })

  it('pausing a page whose sync never started uploads nothing', async () => {
    // The hydrate failed, so main.tsx never started the sync: the locals are
    // untrusted and must not go over the host copy on the way to an import.
    mockFetch(() => ({ ok: false, status: 503, json: () => Promise.resolve({}) }))
    await hydrateUiPrefs()
    localStorage.setItem('mc-crews-view', 'DEFAULT')
    const spy = mockFetch(() => okJson({ prefs: {} }))
    await pauseUiPrefsSync()
    expect(spy).not.toHaveBeenCalled()
  })

  it('keeps the profile synced and resumes uploads when the pending marker cannot be written', async () => {
    await runningSync()
    await pauseUiPrefsSync()
    const synced = localStorage.getItem(SYNCED_KEYS_KEY)
    const setItem = Storage.prototype.setItem
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, value) {
      if (key === 'mc-ui-prefs-hydrate-pending') throw new DOMException('full', 'QuotaExceededError')
      setItem.call(this, key, value)
    })

    try {
      expect(await adoptHostUiPrefsOnNextLoad()).toBe(false)
      expect(localStorage.getItem(SYNCED_KEYS_KEY)).toBe(synced)
      expect(localStorage.getItem('mc-ui-prefs-hydrate-pending')).toBeNull()
      expect(needsHydrate()).toBe(false)

      localStorage.setItem('mc-crews-view', 'grid')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await vi.advanceTimersByTimeAsync(2000)
      expect(lastPatch(spy)).toEqual({ 'mc-crews-view': 'grid' })
    } finally {
      storageSpy.mockRestore()
    }
  })

  it('rolls back the pending marker and resumes uploads when the synced marker cannot be removed', async () => {
    await runningSync()
    await pauseUiPrefsSync()
    const synced = localStorage.getItem(SYNCED_KEYS_KEY)
    const removeItem = Storage.prototype.removeItem
    const storageSpy = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (key) {
      if (key === SYNCED_KEYS_KEY) throw new DOMException('blocked', 'SecurityError')
      removeItem.call(this, key)
    })

    try {
      expect(await adoptHostUiPrefsOnNextLoad()).toBe(false)
      expect(localStorage.getItem(SYNCED_KEYS_KEY)).toBe(synced)
      expect(localStorage.getItem('mc-ui-prefs-hydrate-pending')).toBeNull()
      expect(needsHydrate()).toBe(false)

      localStorage.setItem('mc-crews-view', 'grid')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await vi.advanceTimersByTimeAsync(2000)
      expect(lastPatch(spy)).toEqual({ 'mc-crews-view': 'grid' })
    } finally {
      storageSpy.mockRestore()
    }
  })

  it('after adopting, the next load takes the HOST value for every key it holds', async () => {
    // A warm, synced profile whose host copy was just rewritten by an import.
    localStorage.setItem('mc-crews-view', 'MINE')
    localStorage.setItem('mc-nav', 'LOCAL-ONLY')
    mockFetch(() => okJson({ prefs: { 'mc-crews-view': 'MINE' } }))
    await hydrateUiPrefs()
    expect(needsHydrate()).toBe(false)

    expect(await adoptHostUiPrefsOnNextLoad()).toBe(true)
    expect(needsHydrate()).toBe(true)
    // Nothing uploads before the reload, the pagehide flush included.
    const quiet = mockFetch(() => okJson({ prefs: {} }))
    await flushUiPrefs(true)
    expect(quiet).not.toHaveBeenCalled()

    // The next load: the restored host copy wins where it has a value.
    __resetUiPrefsSyncForTests()
    mockFetch(() => okJson({ prefs: { 'mc-crews-view': 'FROM-ARCHIVE' } }))
    expect(await hydrateUiPrefs()).toBe(1)
    expect(localStorage.getItem('mc-crews-view')).toBe('FROM-ARCHIVE')
    expect(localStorage.getItem('mc-nav')).toBe('LOCAL-ONLY')
    expect(localStorage.getItem('mc-ui-prefs-hydrate-pending')).toBeNull()
    expect(needsHydrate()).toBe(false)
  })

  it('clears pre-import chat dirty markers when adoption is armed, so the import value is not overridden (c622 GPT 6.1 F1)', async () => {
    // An unsent chat edit left a dirty marker; a Replace import then rewrote the
    // host copy and arms adoption. Without clearing the marker, the re-armed
    // hydrate would treat the dirty child as locally-owned and KEEP the stale
    // local edit over the explicitly-imported value, then flush it back over the
    // host. Adoption must clear the chat dirty markers.
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTimestamps: true }))
    mockFetch(() => okJson({ prefs: {} }))
    await hydrateUiPrefs()
    // The user edits a chat field; the save seam records it dirty.
    markCompositeFieldsDirty(['showTimestamps'])
    expect(
      JSON.parse(localStorage.getItem('mc-chat-config-dirty') || '[]') as string[],
    ).toContain('showTimestamps')

    expect(await adoptHostUiPrefsOnNextLoad()).toBe(true)
    // The pre-import dirty marker is cleared, so the dirty child no longer counts
    // as locally-owned on the next hydrate.
    expect(localStorage.getItem('mc-chat-config-dirty')).toBeNull()

    // Proof: the next load takes the imported host value, not the stale edit.
    __resetUiPrefsSyncForTests()
    const hex = (f: string) =>
      [...f].map((c) => c.charCodeAt(0).toString(16).padStart(4, '0')).join('')
    mockFetch(() => okJson({ prefs: { [`mc-chat-config.${hex('showTimestamps')}`]: 'false' } }))
    await hydrateUiPrefs()
    expect(JSON.parse(localStorage.getItem('mc-chat-config') || '{}').showTimestamps).toBe(false)
  })
})
