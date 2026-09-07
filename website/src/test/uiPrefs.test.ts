import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  DURABLE_PREF_KEYS,
  hydrateUiPrefs,
  needsHydrate,
  flushUiPrefs,
  startUiPrefsSync,
  __resetUiPrefsSyncForTests,
} from '../lib/uiPrefs'

const SYNCED_KEYS_KEY = 'mc-ui-prefs-synced'

function mockFetch(impl: (url: string, init?: RequestInit) => unknown) {
  const spy = vi.fn((url: string, init?: RequestInit) => Promise.resolve(impl(url, init)))
  vi.stubGlobal('fetch', spy as unknown as typeof fetch)
  return spy
}

function okJson(body: unknown) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) }
}

/** The last PUT body the spy saw, parsed. */
function lastPatch(spy: ReturnType<typeof mockFetch>): Record<string, string | null> {
  const puts = spy.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
  const body = (puts.at(-1)?.[1] as RequestInit).body as string
  return JSON.parse(body).prefs
}

describe('uiPrefs', () => {
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

  describe('the allowlist', () => {
    it('holds no duplicates', () => {
      expect(new Set(DURABLE_PREF_KEYS).size).toBe(DURABLE_PREF_KEYS.length)
    })

    it('excludes session-scoped and derived state', () => {
      // These are per-session or pure caches: mirroring them would grow without
      // bound and resurrect stale UI on an unrelated profile.
      for (const forbidden of [
        'vc_heights_',
        'mc-panel-tabs',
        'mc-chat-drafts',
        'mc-comment-drafts',
        'mc-paste-store-v1',
        'kirocrew:touched-files:',
        'mc-webpreview-url',
        'mc-active-slot-chat',
      ]) {
        expect(DURABLE_PREF_KEYS.some((k) => k.startsWith(forbidden))).toBe(false)
      }
    })

    it('excludes the bearer token and the keys config.json already owns', () => {
      // The token is a credential; theme/language/onboarding reconcile through
      // /api/config/theme, so backing them up here would fork the truth.
      for (const owned of [
        'kiro_crew_token',
        'mc-lang',
        'mc-color-theme',
        'mc-theme',
        'mc-onboarded',
        'mc-import-onboarded',
      ]) {
        expect(DURABLE_PREF_KEYS).not.toContain(owned)
      }
    })

    it('excludes any key that gates a safety confirmation', () => {
      // mc-yolo-ack's presence makes ApprovalModePicker skip the confirmation and
      // enable full auto-approval. This backup lives in the agent-writable data
      // home, so restoring it would let an agent pre-satisfy a human safety gate.
      expect(DURABLE_PREF_KEYS).not.toContain('mc-yolo-ack')
    })

    it('excludes the legacy scaling keys a migration deliberately deletes', () => {
      // hooks/useZoom.ts folds both into the native zoom factor and REMOVES
      // them; backing them up would restore them and re-run the migration.
      expect(DURABLE_PREF_KEYS).not.toContain('mc-zoom')
      expect(DURABLE_PREF_KEYS).not.toContain('mc-font-scale')
    })

    it('excludes its own sync bookkeeping keys', () => {
      expect(DURABLE_PREF_KEYS).not.toContain(SYNCED_KEYS_KEY)
      expect(DURABLE_PREF_KEYS).not.toContain('mc-ui-prefs-hydrate-pending')
    })
  })

  describe('needsHydrate', () => {
    it('is true on a profile that has never reached the host', () => {
      localStorage.setItem('mc-chat-config', '{}') // warm, but never synced
      expect(needsHydrate()).toBe(true)
    })

    it('is false once a fetch has succeeded', async () => {
      mockFetch(() => okJson({ prefs: {} }))
      await hydrateUiPrefs()
      expect(needsHydrate()).toBe(false)
    })

    it('stays true after a failed fetch, so the next boot retries', async () => {
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      await hydrateUiPrefs()
      expect(needsHydrate()).toBe(true)
    })
  })

  describe('hydrateUiPrefs', () => {
    it('restores keys that are missing locally', async () => {
      mockFetch(() => okJson({ prefs: { 'mc-chat-config': '{"sendOnEnter":"ctrl-enter"}' } }))
      expect(await hydrateUiPrefs()).toBe(1)
      expect(localStorage.getItem('mc-chat-config')).toBe('{"sendOnEnter":"ctrl-enter"}')
    })

    it('never overwrites a value this profile already has', async () => {
      localStorage.setItem('mc-chat-config', 'local-wins')
      mockFetch(() => okJson({ prefs: { 'mc-chat-config': 'server-copy' } }))
      expect(await hydrateUiPrefs()).toBe(0)
      expect(localStorage.getItem('mc-chat-config')).toBe('local-wins')
    })

    it('ignores keys outside the allowlist', async () => {
      mockFetch(() => okJson({ prefs: { 'vc_heights_x': '{}', 'kiro_crew_token': 'leak' } }))
      expect(await hydrateUiPrefs()).toBe(0)
      expect(localStorage.getItem('kiro_crew_token')).toBeNull()
    })

    it('ignores non-string values', async () => {
      mockFetch(() => okJson({ prefs: { 'mc-font-family': 1.5 } }))
      expect(await hydrateUiPrefs()).toBe(0)
    })

    it('notifies readers only when something was restored', async () => {
      const onChange = vi.fn()
      window.addEventListener('mc-config-changed', onChange)
      mockFetch(() => okJson({ prefs: {} }))
      await hydrateUiPrefs()
      expect(onChange).not.toHaveBeenCalled()

      mockFetch(() => okJson({ prefs: { 'mc-font-family': '1.2' } }))
      await hydrateUiPrefs()
      expect(onChange).toHaveBeenCalledTimes(1)
      window.removeEventListener('mc-config-changed', onChange)
    })

    it('after a FAILED restore, the host wins for keys the profile did NOT hold at the failure', async () => {
      // Boot 1: GET fails (expired cookie). The app renders (login screen), and a
      // hook persists a default locally.
      mockFetch(() => ({ ok: false, status: 403, json: () => Promise.resolve({}) }))
      await hydrateUiPrefs()
      expect(needsHydrate()).toBe(true)
      localStorage.setItem('mc-crews-view', 'DEFAULT-WRITTEN-WHILE-LOGGED-OUT')

      // Boot 2: GET succeeds. Without the pending flag the local default would
      // have been kept, fingerprinted as "differs", and the first flush would
      // have uploaded it over the real backup.
      mockFetch(() => okJson({ prefs: { 'mc-crews-view': 'HOST' } }))
      const restored = await hydrateUiPrefs()
      expect(restored).toBe(1)
      expect(localStorage.getItem('mc-crews-view')).toBe('HOST')
      expect(needsHydrate()).toBe(false)

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('a normal (never-failed) first restore still lets local win', async () => {
      localStorage.setItem('mc-crews-view', 'MINE')
      mockFetch(() => okJson({ prefs: { 'mc-crews-view': 'HOST' } }))
      await hydrateUiPrefs()
      expect(localStorage.getItem('mc-crews-view')).toBe('MINE')
    })

    it('after a FAILED restore, a key the profile ALREADY held stays local -- including a later change', async () => {
      // A returning user: the profile holds a real value, one GET fails
      // transiently, the user then changes the preference. The host's copy is
      // now the STALE one; letting it win would erase the change at next launch.
      localStorage.setItem('mc-crews-view', 'MINE')
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      await hydrateUiPrefs()
      localStorage.setItem('mc-crews-view', 'MINE-CHANGED-AFTER-FAILURE')
      // ...while a default written by the settings-less render is still untrusted.
      localStorage.setItem('mc-nav', 'DEFAULT-WRITTEN-WHILE-DOWN')

      mockFetch(() => okJson({ prefs: { 'mc-crews-view': 'HOST-STALE', 'mc-nav': 'HOST' } }))
      const restored = await hydrateUiPrefs()
      expect(restored).toBe(1)
      expect(localStorage.getItem('mc-crews-view')).toBe('MINE-CHANGED-AFTER-FAILURE')
      expect(localStorage.getItem('mc-nav')).toBe('HOST')
    })

    it('a repeat failure does not widen the owned-at-failure snapshot', async () => {
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      await hydrateUiPrefs()
      localStorage.setItem('mc-crews-view', 'DEFAULT-AFTER-FIRST-FAILURE')
      await hydrateUiPrefs() // second failure: the default above must NOT become "owned"
      mockFetch(() => okJson({ prefs: { 'mc-crews-view': 'HOST' } }))
      await hydrateUiPrefs()
      expect(localStorage.getItem('mc-crews-view')).toBe('HOST')
    })

    it('the pending marker records the owned keys and is cleared by a successful restore', async () => {
      localStorage.setItem('mc-crews-view', 'MINE')
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      await hydrateUiPrefs()
      expect(JSON.parse(localStorage.getItem('mc-ui-prefs-hydrate-pending') ?? 'null')).toEqual([
        'mc-crews-view',
      ])
      mockFetch(() => okJson({ prefs: {} }))
      await hydrateUiPrefs()
      expect(localStorage.getItem('mc-ui-prefs-hydrate-pending')).toBeNull()
    })

    it('baselines a local value that differs from the host instead of uploading it', async () => {
      // This origin has not been syncing (it is hydrating); the host's copy came
      // from one that has. Uploading the local value here would clobber the
      // newer backup with a possibly months-stale one on first flush.
      localStorage.setItem('mc-crews-view', 'STALE-FROM-OLD-ORIGIN')
      mockFetch(() => okJson({ prefs: { 'mc-crews-view': 'NEWER-HOST' } }))
      await hydrateUiPrefs()
      expect(localStorage.getItem('mc-crews-view')).toBe('STALE-FROM-OLD-ORIGIN') // still in use here

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()

      // ...but the moment the user changes it here, it is theirs and goes up.
      localStorage.setItem('mc-crews-view', 'CHANGED-HERE')
      const spy2 = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy2)).toEqual({ 'mc-crews-view': 'CHANGED-HERE' })
    })

    it('marks a local value that already equals the host as synced', async () => {
      localStorage.setItem('mc-crews-view', 'SAME')
      mockFetch(() => okJson({ prefs: { 'mc-crews-view': 'SAME' } }))
      await hydrateUiPrefs()

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('records only the keys that actually landed locally', async () => {
      // A value safeSetItem has to drop (quota) must NOT be recorded as synced:
      // the first flush would read it as a deletion and null out a good backup.
      const realSet = Storage.prototype.setItem
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
        this: Storage,
        k: string,
        v: string,
      ) {
        if (k === 'mc-dev-mode') throw new DOMException('full', 'QuotaExceededError')
        realSet.call(this, k, v)
      })
      mockFetch(() => okJson({ prefs: { 'mc-dev-mode': 'true', 'mc-crews-view': 'grid' } }))
      await hydrateUiPrefs()
      vi.restoreAllMocks()

      expect(localStorage.getItem('mc-dev-mode')).toBeNull()
      expect(Object.keys(JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!))).toEqual([
        'mc-crews-view',
      ])

      // The follow-up flush must not delete the host's copy of the key it failed
      // to store locally.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      const puts = spy.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
      const patch = puts.length
        ? JSON.parse((puts.at(-1)![1] as RequestInit).body as string).prefs
        : {}
      expect(patch['mc-dev-mode']).toBeUndefined()
    })

    it.each([
      ['a non-2xx response', () => ({ ok: false, status: 500, json: () => Promise.resolve({}) })],
      ['a malformed payload', () => okJson({ prefs: 'nope' })],
      ['a payload with no prefs', () => okJson({})],
    ])('degrades to 0 restored on %s', async (_label, impl) => {
      mockFetch(impl)
      expect(await hydrateUiPrefs()).toBe(0)
    })

    it('degrades to 0 restored when the gateway is unreachable', async () => {
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      expect(await hydrateUiPrefs()).toBe(0)
    })
  })

  describe('flushUiPrefs', () => {
    it('sends nothing when there is nothing to send', async () => {
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('uploads a change whose value collides with the prior one on a single 32-bit FNV-1a lane', async () => {
      // These two strings hash identically under plain 32-bit FNV-1a. With only
      // that lane, the second value read as "already synced": never uploaded, and
      // an origin reset would restore the stale first value over it.
      localStorage.setItem('mc-cloud-profile', 'pref-ijfpMFqy')
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      localStorage.setItem('mc-cloud-profile', 'pref-F51SMhug')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-cloud-profile': 'pref-F51SMhug' })
    })

    it('sends the durable keys and nothing else', async () => {
      localStorage.setItem('mc-font-family', '1.25')
      localStorage.setItem('vc_heights_abc', '{"0":10}')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-font-family': '1.25' })
    })

    it('sends only what changed since the last successful flush', async () => {
      localStorage.setItem('mc-font-family', '1')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      localStorage.setItem('mc-dev-mode', 'true')
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-dev-mode': 'true' })
    })

    it('sends null for a key the user cleared', async () => {
      localStorage.setItem('mc-dev-mode', 'true')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      localStorage.removeItem('mc-dev-mode')
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-dev-mode': null })
    })

    it('retries the same patch after a failed flush', async () => {
      localStorage.setItem('mc-zoom-unused', 'x') // non-durable, ignored
      localStorage.setItem('mc-dev-mode', 'true')
      const failing = mockFetch(() => ({ ok: false, status: 503, json: () => Promise.resolve({}) }))
      await flushUiPrefs()
      expect(lastPatch(failing)).toEqual({ 'mc-dev-mode': 'true' })
      // The baseline must NOT have advanced, or the value would be dropped.
      const retry = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(retry)).toEqual({ 'mc-dev-mode': 'true' })
    })

    it('a refused patch is retried per key so valid changes still land', async () => {
      localStorage.setItem('mc-dev-mode', 'true')
      localStorage.setItem('kc:file-explorer:state:v2', 'too-big')
      // Whole-patch PUT is refused; per-key retry accepts everything except the
      // offending value.
      const spy = mockFetch((_url, init) => {
        const body = JSON.parse((init as RequestInit).body as string)
        const keys = Object.keys(body.prefs)
        if (keys.length > 1) return { ok: false, status: 400, json: () => Promise.resolve({}) }
        if (keys[0] === 'kc:file-explorer:state:v2') {
          return { ok: false, status: 400, json: () => Promise.resolve({}) }
        }
        return okJson({ prefs: {} })
      })
      await flushUiPrefs()

      const sent = spy.mock.calls
        .filter((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
        .map((c) => Object.keys(JSON.parse((c[1] as RequestInit).body as string).prefs))
      expect(sent).toContainEqual(['mc-dev-mode'])
      expect(sent).toContainEqual(['kc:file-explorer:state:v2'])

      // The accepted key is now baselined (not resent), the refused one is not.
      const after = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(after)).toEqual({ 'kc:file-explorer:state:v2': 'too-big' })
    })

    it('after a reload it re-sends nothing when nothing changed', async () => {
      // A stale origin re-PUTting every value would overwrite the newer
      // preferences another origin already backed up.
      localStorage.setItem('mc-dev-mode', 'true')
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      __resetUiPrefsSyncForTests() // page reload: in-memory baseline gone

      const after = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(after).not.toHaveBeenCalled()
    })

    it('after a reload it sends only the value this profile changed', async () => {
      localStorage.setItem('mc-dev-mode', 'true')
      localStorage.setItem('mc-crews-view', 'list')
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      __resetUiPrefsSyncForTests()
      localStorage.setItem('mc-crews-view', 'grid')

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-crews-view': 'grid' })
    })

    it('a per-key retry after a reload keeps the untouched fingerprints', async () => {
      localStorage.setItem('mc-dev-mode', 'true')
      localStorage.setItem('mc-crews-view', 'list')
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      __resetUiPrefsSyncForTests() // reload: in-memory baseline gone

      // One key changes and the host refuses the whole patch, forcing per-key
      // retry with an EMPTY in-memory baseline.
      localStorage.setItem('mc-crews-view', 'grid')
      mockFetch((_url, init) => {
        const keys = Object.keys(JSON.parse((init as RequestInit).body as string).prefs)
        return keys.length > 1
          ? { ok: false, status: 400, json: () => Promise.resolve({}) }
          : okJson({ prefs: {} })
      })
      await flushUiPrefs()

      // mc-dev-mode was never in this round, so its fingerprint must survive —
      // otherwise the next poll re-uploads its stale value.
      const prints = JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!)
      expect(Object.keys(prints).sort()).toEqual(['mc-crews-view', 'mc-dev-mode'])

      __resetUiPrefsSyncForTests()
      const after = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(after).not.toHaveBeenCalled()
    })

    it('does not delete a key a newer build synced (downgrade safety)', async () => {
      // The marker still lists a key this build's allowlist does not contain. It
      // can never appear in `current`, so treating it as previously-synced would
      // emit null and delete a preference the newer build owns.
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({ 'mc-from-a-newer-build': 'abc', 'mc-dev-mode': 'zzz' }),
      )
      localStorage.setItem('mc-dev-mode', 'true')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      const patch = lastPatch(spy)
      expect(patch['mc-from-a-newer-build']).toBeUndefined()
      expect(patch['mc-dev-mode']).toBe('true')
    })

    it('reports a deletion made before a reload', async () => {
      // A previous session synced the key...
      localStorage.setItem('mc-dev-mode', 'true')
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      // ...then the page reloads (in-memory baseline gone) and the user clears it.
      __resetUiPrefsSyncForTests()
      localStorage.removeItem('mc-dev-mode')

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-dev-mode': null })
    })

    it('never nulls a key this profile has not synced', async () => {
      // A second browser holds a key on the host that this profile never had.
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({}))
      localStorage.setItem('mc-dev-mode', 'true')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-dev-mode': 'true' })
    })

    it('a change during an in-flight PUT is not swallowed', async () => {
      localStorage.setItem('mc-dev-mode', 'true')
      let release: (() => void) | undefined
      const gate = new Promise<void>((r) => {
        release = r
      })
      let calls = 0
      const spy = vi.fn((_url: string, _init?: RequestInit) => {
        calls += 1
        // Hold the FIRST request open so the second flush lands mid-flight.
        return calls === 1
          ? gate.then(() => okJson({ prefs: {} }))
          : Promise.resolve(okJson({ prefs: {} }))
      })
      vi.stubGlobal('fetch', spy as unknown as typeof fetch)

      const first = flushUiPrefs()
      localStorage.setItem('mc-crews-view', 'grid')
      await flushUiPrefs() // in-flight: marks dirty, returns immediately
      release!()
      await first

      const puts = spy.mock.calls.map((c) =>
        Object.keys(JSON.parse((c[1] as RequestInit).body as string).prefs),
      )
      expect(puts.length).toBe(2)
      expect(puts[1]).toEqual(['mc-crews-view'])
    })

    it('does not overlap two in-flight flushes', async () => {
      localStorage.setItem('mc-font-family', '1')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await Promise.all([flushUiPrefs(), flushUiPrefs()])
      expect(spy.mock.calls.length).toBe(1)
    })

    it('keeps syncing through a 403, so a routine cookie lapse does not stop backups for the session', async () => {
      // The access cookie lapses mid-session (the app's own refresh scheduler
      // repairs it). The refused patch stays pending and goes up once auth is
      // back -- with the change made during the lapse.
      localStorage.setItem('mc-font-family', '1')
      mockFetch(() => ({ ok: false, status: 403, json: () => Promise.resolve({}) }))
      startUiPrefsSync()
      await vi.advanceTimersByTimeAsync(2000)

      const after = mockFetch(() => okJson({ prefs: {} }))
      localStorage.setItem('mc-dev-mode', 'true')
      window.dispatchEvent(new Event('mc-config-changed'))
      await vi.advanceTimersByTimeAsync(2000)
      expect(lastPatch(after)).toEqual({ 'mc-font-family': '1', 'mc-dev-mode': 'true' })
    })
  })

  describe('startUiPrefsSync', () => {
    it('flushes on pagehide with keepalive, so a change made just before closing survives teardown', async () => {
      const spy = mockFetch(() => okJson({ prefs: {} }))
      startUiPrefsSync()
      await vi.advanceTimersByTimeAsync(2000)
      spy.mockClear()

      localStorage.setItem('mc-dev-mode', 'true') // inside the debounce window...
      window.dispatchEvent(new Event('mc-config-changed'))
      window.dispatchEvent(new Event('pagehide')) // ...and the tab closes
      await vi.advanceTimersByTimeAsync(0)
      expect(spy).toHaveBeenCalledTimes(1)
      const init = spy.mock.calls[0][1] as RequestInit
      expect(init.keepalive).toBe(true)
      expect(JSON.parse(init.body as string)).toEqual({ prefs: { 'mc-dev-mode': 'true' } })
    })

    it('ordinary flushes do not use keepalive (its 64 KiB body cap would reject large patches)', async () => {
      localStorage.setItem('mc-font-family', '1.5')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      startUiPrefsSync()
      await vi.advanceTimersByTimeAsync(2000)
      expect((spy.mock.calls[0][1] as RequestInit).keepalive).toBe(false)
    })

    it('uploads the current profile on start, so an upgrading user gets a backup', async () => {
      localStorage.setItem('mc-font-family', '1.5')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      startUiPrefsSync()
      await vi.advanceTimersByTimeAsync(2000)
      expect(lastPatch(spy)).toEqual({ 'mc-font-family': '1.5' })
    })

    it('debounces a burst of changes into one PUT', async () => {
      const spy = mockFetch(() => okJson({ prefs: {} }))
      startUiPrefsSync()
      for (let i = 0; i < 5; i++) {
        localStorage.setItem('mc-font-family', String(i))
        window.dispatchEvent(new Event('mc-config-changed'))
      }
      await vi.advanceTimersByTimeAsync(2000)
      const puts = spy.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
      expect(puts.length).toBe(1)
      expect(lastPatch(spy)).toEqual({ 'mc-font-family': '4' })
    })

    it('is idempotent', async () => {
      localStorage.setItem('mc-font-family', '1')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      startUiPrefsSync()
      startUiPrefsSync()
      await vi.advanceTimersByTimeAsync(2000)
      const puts = spy.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
      expect(puts.length).toBe(1)
    })

    it('backs up a change another tab made', async () => {
      const spy = mockFetch(() => okJson({ prefs: {} }))
      startUiPrefsSync()
      await vi.advanceTimersByTimeAsync(2000)
      localStorage.setItem('mc-dev-mode', 'true')
      window.dispatchEvent(new Event('storage'))
      await vi.advanceTimersByTimeAsync(2000)
      expect(lastPatch(spy)).toEqual({ 'mc-dev-mode': 'true' })
    })
  })
})
