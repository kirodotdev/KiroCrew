import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  DURABLE_PREF_KEYS,
  hydrateUiPrefs,
  needsHydrate,
  flushUiPrefs,
  startUiPrefsSync,
  hasUnreconciledKeys,
  reconcileNewDurableKeys,
  pauseUiPrefsSync,
  resumeUiPrefsSync,
  adoptHostUiPrefsOnNextLoad,
  markCompositeFieldsDirty,
  __resetUiPrefsSyncForTests,
} from '../lib/uiPrefs'
import { saveChatConfig, loadChatConfig } from '../pages/chat/ChatSettings'
import { loadBoardFolderCollapse, persistBoardOverride } from '../utils/boardFolderCollapse'

const SYNCED_KEYS_KEY = 'mc-ui-prefs-synced'
const ROSTER_ENTRY = 'mc:ui-prefs:roster'

/** The reconciled roster stored inside the synced document, or null. */
function storedRoster(): string[] | null {
  const raw = localStorage.getItem(SYNCED_KEYS_KEY)
  if (raw === null) return null
  const entry = (JSON.parse(raw) as Record<string, string>)[ROSTER_ENTRY]
  return typeof entry === 'string' ? (JSON.parse(entry) as string[]) : null
}

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

    it('excludes the Apps Library view toggle, which is origin-local by decision', () => {
      // The growth-gap mechanism (reconcileNewDurableKeys, issue 9491) now
      // hydrates a newly added key before its first flush, so re-adding this
      // key is mechanically safe -- but whether the Library show-all toggle
      // SHOULD follow the user across origins is a product decision that has
      // not been made. A re-add without that decision fails here.
      expect(DURABLE_PREF_KEYS).not.toContain('mc-apps-library-show-all')
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
      expect(DURABLE_PREF_KEYS).not.toContain(ROSTER_ENTRY)
    })

    it('holds the three per-surface prefs that used to reset across origins', () => {
      // Issue 9875: sound, interface mode, and reading width were localStorage
      // only, so they silently reverted to defaults on every fresh origin.
      for (const key of ['mc-notification-sound', 'mc-ui', 'mc-reading-width']) {
        expect(DURABLE_PREF_KEYS).toContain(key)
      }
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
      mockFetch(() => okJson({ prefs: { 'mc-font-family': 'serif' } }))
      expect(await hydrateUiPrefs()).toBe(1)
      expect(localStorage.getItem('mc-font-family')).toBe('serif')
    })

    it('never overwrites a value this profile already has', async () => {
      localStorage.setItem('mc-font-family', 'local-wins')
      mockFetch(() => okJson({ prefs: { 'mc-font-family': 'server-copy' } }))
      expect(await hydrateUiPrefs()).toBe(0)
      expect(localStorage.getItem('mc-font-family')).toBe('local-wins')
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
      const printKeys = Object.keys(JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!)).filter(
        (k) => k !== ROSTER_ENTRY, // reconcile bookkeeping, not a fingerprint
      )
      expect(printKeys).toEqual(['mc-crews-view'])

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

    it('sends the sessions sidebar width keys as stored and the board collapse family as one projected key', async () => {
      localStorage.setItem('mc-sidebar-width', '920')
      localStorage.setItem('mc-sidebar-width-pre-board', '260')
      localStorage.setItem('kc-board-folder-collapsed:col-b:f1', '0')
      localStorage.setItem('kc-board-folder-collapsed:col-a:f1', '1')
      // A key added to the allowlist is withheld until a reconcile has read
      // the host's copy. The host holds none, so the local values seed it.
      mockFetch(() => okJson({ prefs: {} }))
      expect(await reconcileNewDurableKeys()).toBe(0)
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({
        'mc-sidebar-width': '920',
        'mc-sidebar-width-pre-board': '260',
        'kc-board-folder-collapsed': '{"col-a:f1":true,"col-b:f1":false}',
      })
      // The projection exists only on the wire. Nothing is stored under it.
      expect(localStorage.getItem('kc-board-folder-collapsed')).toBeNull()
    })

    it('deletes the host copy of the board collapse family once the last override is gone', async () => {
      localStorage.setItem('kc-board-folder-collapsed:col-a:f1', '1')
      mockFetch(() => okJson({ prefs: {} }))
      expect(await reconcileNewDurableKeys()).toBe(0)
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      localStorage.removeItem('kc-board-folder-collapsed:col-a:f1')
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'kc-board-folder-collapsed': null })
    })

    it('a cold restore writes the board collapse family as per-key entries', async () => {
      mockFetch(() => okJson({ prefs: { 'kc-board-folder-collapsed': '{"col-a:f1":true}' } }))
      expect(await hydrateUiPrefs()).toBe(1)
      expect(localStorage.getItem('kc-board-folder-collapsed:col-a:f1')).toBe('1')
      expect(localStorage.getItem('kc-board-folder-collapsed')).toBeNull()
      expect(loadBoardFolderCollapse().get('col-a:f1')).toBe(true)
      // Hydrate baselines the adopted family as this profile's local value.
      // The next flush therefore has nothing to send.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('a toggle after a cold restore uploads the toggled family and survives the next load', async () => {
      // The restored family is adopted whole. One toggle changes one pair. The
      // flush uploads the whole family with that pair changed. A later load
      // reads the toggled value. No restored copy remains to re-adopt over it.
      mockFetch(() => okJson({ prefs: { 'kc-board-folder-collapsed': '{"col-a:f1":true,"col-b:f2":false}' } }))
      expect(await hydrateUiPrefs()).toBe(1)
      persistBoardOverride('col-a', 'f1', false)
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'kc-board-folder-collapsed': '{"col-a:f1":false,"col-b:f2":false}' })
      __resetUiPrefsSyncForTests()
      expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-a:f1': false, 'col-b:f2': false })
      expect(localStorage.getItem('kc-board-folder-collapsed')).toBeNull()
      const after = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(after).not.toHaveBeenCalled()
    })

    it('a cold restore whose board collapse write is refused counts nothing and keeps the host copy', async () => {
      const realSet = Storage.prototype.setItem
      const setSpy = vi
        .spyOn(Storage.prototype, 'setItem')
        .mockImplementation(function (this: Storage, k: string, v: string) {
          if (k.startsWith('kc-board-folder-collapsed:')) throw new DOMException('full', 'QuotaExceededError')
          return realSet.call(this, k, v)
        })
      try {
        mockFetch(() => okJson({ prefs: { 'kc-board-folder-collapsed': '{"col-a:f1":true}' } }))
        expect(await hydrateUiPrefs()).toBe(0)
      } finally {
        setSpy.mockRestore()
      }
      expect(localStorage.getItem('kc-board-folder-collapsed')).toBeNull()
      expect(loadBoardFolderCollapse().size).toBe(0)
      // The dropped key has no baseline and no local value. The first flush
      // neither re-sends it nor deletes the host's copy.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('a cold restore never overrides a board collapse family this profile already holds', async () => {
      localStorage.setItem('kc-board-folder-collapsed:col-a:f1', '0')
      mockFetch(() => okJson({ prefs: { 'kc-board-folder-collapsed': '{"col-a:f1":true}' } }))
      expect(await hydrateUiPrefs()).toBe(0)
      expect(localStorage.getItem('kc-board-folder-collapsed')).toBeNull()
      expect(loadBoardFolderCollapse().get('col-a:f1')).toBe(false)
    })

    it('a settings import replaces the board collapse family with the imported one', async () => {
      // Scenario: a Replace import rewrote the host copy of a synced profile
      // that holds its own overrides. The imported family must win whole.
      localStorage.setItem('kc-board-folder-collapsed:col-a:f1', '0')
      localStorage.setItem('kc-board-folder-collapsed:col-c:f3', '1')
      mockFetch(() => okJson({ prefs: {} }))
      await hydrateUiPrefs()
      expect(await adoptHostUiPrefsOnNextLoad()).toBe(true)

      __resetUiPrefsSyncForTests()
      mockFetch(() => okJson({ prefs: { 'kc-board-folder-collapsed': '{"col-a:f1":true,"col-b:f2":true}' } }))
      expect(await hydrateUiPrefs()).toBe(1)
      expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-a:f1': true, 'col-b:f2': true })
      expect(localStorage.getItem('kc-board-folder-collapsed:col-c:f3')).toBeNull()
      expect(localStorage.getItem('kc-board-folder-collapsed')).toBeNull()
      // The adopted family equals the baseline, so nothing re-uploads over the
      // imported copy.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('a warm profile adopts a board collapse family another origin already backed up', async () => {
      // Scenario: this profile synced before the key joined the allowlist and
      // holds no override. Another origin on the same host uploaded its family.
      localStorage.setItem('mc-crews-view', 'list')
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      __resetUiPrefsSyncForTests()
      mockFetch(() => okJson({ prefs: { 'kc-board-folder-collapsed': '{"col-a:f1":true}' } }))
      expect(await reconcileNewDurableKeys()).toBe(1)
      expect(localStorage.getItem('kc-board-folder-collapsed:col-a:f1')).toBe('1')
      expect(localStorage.getItem('kc-board-folder-collapsed')).toBeNull()
      expect(loadBoardFolderCollapse().get('col-a:f1')).toBe(true)
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
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

  describe('reconcileNewDurableKeys (growth-gap, issue 9491)', () => {
    /** Build the state of a profile from BEFORE a key joined the allowlist:
     *  warm (has synced fingerprints for an old key), no reconciled roster. */
    async function warmLegacyProfile() {
      localStorage.setItem('mc-crews-view', 'list')
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      __resetUiPrefsSyncForTests() // reload into the upgraded build
      expect(storedRoster()).toBeNull()
    }

    it('a cold origin restores the three keys through the ordinary hydrate', async () => {
      mockFetch(() =>
        okJson({
          prefs: {
            'mc-notification-sound': '{"enabled":false,"volume":0.2,"perCategory":{"all":"ding"}}',
            'mc-ui': 'cli',
            'mc-reading-width': 'full',
          },
        }),
      )
      expect(await hydrateUiPrefs()).toBe(3)
      expect(localStorage.getItem('mc-ui')).toBe('cli')
      expect(localStorage.getItem('mc-reading-width')).toBe('full')
      expect(hasUnreconciledKeys()).toBe(false) // hydrate also records the roster
    })

    it('a warm origin does NOT flush a mount-written default over the host value', async () => {
      await warmLegacyProfile()
      // UIModeProvider persists the current mode on mount, so by the time the
      // upgraded build reconciles, the key exists locally holding the default.
      localStorage.setItem('mc-ui', 'chat')
      mockFetch(() => okJson({ prefs: { 'mc-ui': 'cli' } }))
      expect(await reconcileNewDurableKeys()).toBe(0)

      // Local stays in use here (the backup is not a live sync channel)...
      expect(localStorage.getItem('mc-ui')).toBe('chat')
      // ...and the first flush does not upload it over the host's copy.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()

      // The moment the user changes it HERE, it is theirs and goes up.
      localStorage.setItem('mc-ui', 'cli')
      const spy2 = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy2)).toEqual({ 'mc-ui': 'cli' })
    })

    it('a warm origin adopts the host value for a new key it never held', async () => {
      await warmLegacyProfile()
      mockFetch(() => okJson({ prefs: { 'mc-reading-width': 'full' } }))
      expect(await reconcileNewDurableKeys()).toBe(1) // caller reloads on > 0
      expect(localStorage.getItem('mc-reading-width')).toBe('full')

      // Adopted, so baselined: nothing to flush.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('a warm origin seeds the backup for a new key the host does not hold', async () => {
      await warmLegacyProfile()
      localStorage.setItem('mc-notification-sound', '{"enabled":false}')
      mockFetch(() => okJson({ prefs: {} }))
      expect(await reconcileNewDurableKeys()).toBe(0)

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-notification-sound': '{"enabled":false}' })
    })

    it('records the roster, so the pass runs once per allowlist growth, not per boot', async () => {
      await warmLegacyProfile()
      expect(hasUnreconciledKeys()).toBe(true)
      mockFetch(() => okJson({ prefs: {} }))
      await reconcileNewDurableKeys()
      expect(hasUnreconciledKeys()).toBe(false)
      expect(storedRoster()).toEqual([...DURABLE_PREF_KEYS])
    })

    it('does NOT bulk-import old keys a legacy profile merely never held', async () => {
      // Only keys added AFTER the profile's baseline are reconciled. mc-nav is
      // a pre-mechanism key with no fingerprint here (never held); adopting it
      // would be the cross-origin sync design decision 1 forbids.
      await warmLegacyProfile()
      mockFetch(() => okJson({ prefs: { 'mc-nav': 'ORIGIN-A-LAYOUT' } }))
      expect(await reconcileNewDurableKeys()).toBe(0)
      expect(localStorage.getItem('mc-nav')).toBeNull()

      // And it is not baselined, so a local deletion can never null the host copy.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('a downgrade sheds the roster, so a re-upgrade reconciles again', async () => {
      await warmLegacyProfile()
      mockFetch(() => okJson({ prefs: {} }))
      await reconcileNewDurableKeys()
      expect(hasUnreconciledKeys()).toBe(false)

      // A pre-roster build rewrites the synced document from its own prints
      // and drops both the roster entry and the new keys' fingerprints.
      const doc = JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!) as Record<string, string>
      delete doc[ROSTER_ENTRY]
      for (const k of ['mc-notification-sound', 'mc-ui', 'mc-reading-width']) delete doc[k]
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify(doc))

      // Back on this build: the keys must count as new again -- trusting the
      // stale roster would upload a stale local value over the host backup.
      expect(hasUnreconciledKeys()).toBe(true)
    })

    it('withholds an unreconciled key from every flush, not only at boot', async () => {
      // Mixed-version multi-tab race: this (new-build) tab reconciled at boot,
      // then an OLD tab's baseline rewrite sheds the roster and the new keys'
      // fingerprints mid-session. This tab's next debounced flush must NOT
      // read the new keys as changed and upload their local values over the
      // host backup -- they stop flushing until a boot reconciles them again.
      await warmLegacyProfile()
      localStorage.setItem('mc-ui', 'chat')
      mockFetch(() => okJson({ prefs: { 'mc-ui': 'cli' } }))
      await reconcileNewDurableKeys()

      // The old tab's commitSent, as a pre-roster build performs it.
      const doc = JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!) as Record<string, string>
      delete doc[ROSTER_ENTRY]
      for (const k of ['mc-notification-sound', 'mc-ui', 'mc-reading-width']) delete doc[k]
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify(doc))

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled() // mc-ui withheld, nothing else changed

      // An old (reconciled-baseline) key still flushes normally.
      localStorage.setItem('mc-crews-view', 'grid')
      const spy2 = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy2)).toEqual({ 'mc-crews-view': 'grid' })
    })

    it('the roster survives an ordinary flush baseline rewrite', async () => {
      await warmLegacyProfile()
      mockFetch(() => okJson({ prefs: {} }))
      await reconcileNewDurableKeys()

      localStorage.setItem('mc-dev-mode', 'true')
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs() // commitSent rewrites the whole document
      expect(storedRoster()).toEqual([...DURABLE_PREF_KEYS])
    })

    it('reports failure on a non-2xx response too, and writes no roster', async () => {
      await warmLegacyProfile()
      mockFetch(() => ({ ok: false, status: 503, json: () => Promise.resolve({}) }))
      expect(await reconcileNewDurableKeys()).toBe(-1)
      expect(storedRoster()).toBeNull()
      expect(hasUnreconciledKeys()).toBe(true)
    })

    it('treats a 200 with a malformed body as a failure, not as an empty backup', async () => {
      // Recording completion here would start the sync and flush local
      // defaults over whatever the host really holds.
      await warmLegacyProfile()
      localStorage.setItem('mc-ui', 'chat')
      mockFetch(() => okJson({ prefs: 'nope' }))
      expect(await reconcileNewDurableKeys()).toBe(-1)
      expect(storedRoster()).toBeNull()
      expect(hasUnreconciledKeys()).toBe(true)
    })

    it('a malformed-body failure still records the ownership snapshot for the retry', async () => {
      // Without the snapshot, ownedAtFailure() is null on retry, keepLocal is
      // true for the default a hook mounted AFTER the failure, and the host
      // value would be silently lost forever.
      await warmLegacyProfile()
      mockFetch(() => okJson({ prefs: 'nope' }))
      expect(await reconcileNewDurableKeys()).toBe(-1)
      localStorage.setItem('mc-ui', 'chat') // mounted default, written post-failure

      mockFetch(() => okJson({ prefs: { 'mc-ui': 'cli' } }))
      expect(await reconcileNewDurableKeys()).toBe(1)
      expect(localStorage.getItem('mc-ui')).toBe('cli') // host wins
    })

    it('a dropped-commit failure records the snapshot; restored keys stay, later defaults lose', async () => {
      await warmLegacyProfile()
      const realSet = Storage.prototype.setItem
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
        this: Storage,
        k: string,
        v: string,
      ) {
        if (k === SYNCED_KEYS_KEY) throw new DOMException('full', 'QuotaExceededError')
        realSet.call(this, k, v)
      })
      mockFetch(() => okJson({ prefs: { 'mc-reading-width': 'full' } }))
      expect(await reconcileNewDurableKeys()).toBe(-1)
      vi.restoreAllMocks()
      // The adoption landed before the commit failed; it holds the HOST value,
      // so the snapshot treats it as owned and the retry keeps it.
      expect(localStorage.getItem('mc-reading-width')).toBe('full')
      localStorage.setItem('mc-ui', 'chat') // mounted default, written post-failure

      mockFetch(() => okJson({ prefs: { 'mc-reading-width': 'full', 'mc-ui': 'cli' } }))
      expect(await reconcileNewDurableKeys()).toBe(1)
      expect(localStorage.getItem('mc-reading-width')).toBe('full') // kept
      expect(localStorage.getItem('mc-ui')).toBe('cli') // host wins

      // Both baselined: nothing to flush.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('does not baseline an adopted value the quota-safe writer had to drop', async () => {
      await warmLegacyProfile()
      const realSet = Storage.prototype.setItem
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
        this: Storage,
        k: string,
        v: string,
      ) {
        if (k === 'mc-reading-width') throw new DOMException('full', 'QuotaExceededError')
        realSet.call(this, k, v)
      })
      mockFetch(() => okJson({ prefs: { 'mc-reading-width': 'full' } }))
      expect(await reconcileNewDurableKeys()).toBe(0)
      vi.restoreAllMocks()

      // Not stored locally, and NOT baselined: a baseline for a missing key
      // would make the first flush null out the good host backup.
      expect(localStorage.getItem('mc-reading-width')).toBeNull()
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('a refused host-wins adoption on the retry baselines the toggled pair instead of uploading it', async () => {
      // The reconcile failed while the profile held no override. The family is
      // therefore not owned. One pair was toggled after the failure. On the
      // retry the host wins. Quota refuses the adoption and it rolls back. The
      // pair stays in use. The first flush must not replace the host's family.
      await warmLegacyProfile()
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      expect(await reconcileNewDurableKeys()).toBe(-1)
      persistBoardOverride('col-a', 'f1', false)
      const refused = 'kc-board-folder-collapsed:col-b:f2'
      const realSet = Storage.prototype.setItem
      const setSpy = vi
        .spyOn(Storage.prototype, 'setItem')
        .mockImplementation(function (this: Storage, k: string, v: string) {
          if (k === refused) throw new DOMException('full', 'QuotaExceededError')
          realSet.call(this, k, v)
        })
      try {
        mockFetch(() => okJson({ prefs: { 'kc-board-folder-collapsed': '{"col-a:f1":true,"col-b:f2":true,"col-c:f3":true}' } }))
        expect(await reconcileNewDurableKeys()).toBe(0)
      } finally {
        setSpy.mockRestore()
      }
      expect(Object.fromEntries(loadBoardFolderCollapse())).toEqual({ 'col-a:f1': false })
      expect(localStorage.getItem('mc-ui-prefs-hydrate-pending')).toBeNull()
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
      // A kept local value goes up on the next change the user makes to it.
      persistBoardOverride('col-a', 'f1', true)
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'kc-board-folder-collapsed': '{"col-a:f1":true}' })
    })

    it('fails the whole reconcile when the baseline+roster commit write is dropped', async () => {
      // A roster recorded without its baselines would start the sync and let
      // the first flush upload unreconciled local values over the host backup.
      // The commit is one write, and a dropped write is a failed reconcile.
      await warmLegacyProfile()
      localStorage.setItem('mc-ui', 'chat')
      const realSet = Storage.prototype.setItem
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
        this: Storage,
        k: string,
        v: string,
      ) {
        if (k === SYNCED_KEYS_KEY) throw new DOMException('full', 'QuotaExceededError')
        realSet.call(this, k, v)
      })
      mockFetch(() => okJson({ prefs: { 'mc-ui': 'cli' } }))
      expect(await reconcileNewDurableKeys()).toBe(-1)
      vi.restoreAllMocks()
      expect(storedRoster()).toBeNull()
      expect(hasUnreconciledKeys()).toBe(true) // retried next boot
    })

    it('a hydrate whose marker write is dropped leaves the profile cold, never roster-only', async () => {
      // Baselines and roster land in ONE write: a profile must never look warm
      // and reconciled while holding no fingerprints.
      const realSet = Storage.prototype.setItem
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
        this: Storage,
        k: string,
        v: string,
      ) {
        if (k === SYNCED_KEYS_KEY) throw new DOMException('full', 'QuotaExceededError')
        realSet.call(this, k, v)
      })
      mockFetch(() => okJson({ prefs: { 'mc-ui': 'cli' } }))
      await hydrateUiPrefs()
      vi.restoreAllMocks()
      expect(needsHydrate()).toBe(true) // next boot hydrates again
      expect(storedRoster()).toBeNull()
    })

    it('reports failure so the caller can decline to start the sync, and the next boot retries', async () => {
      await warmLegacyProfile()
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      expect(await reconcileNewDurableKeys()).toBe(-1)
      expect(hasUnreconciledKeys()).toBe(true) // roster not written: retried next boot
    })

    it('after a FAILED reconcile, the host wins for a new key written by a settings-less render', async () => {
      await warmLegacyProfile()
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      await reconcileNewDurableKeys()
      // The page rendered without its settings; the mount hook wrote a default.
      localStorage.setItem('mc-ui', 'chat')

      mockFetch(() => okJson({ prefs: { 'mc-ui': 'cli' } }))
      expect(await reconcileNewDurableKeys()).toBe(1)
      expect(localStorage.getItem('mc-ui')).toBe('cli')
      expect(localStorage.getItem('mc-ui-prefs-hydrate-pending')).toBeNull()
    })

    it('a reconciled key still propagates a later local deletion', async () => {
      await warmLegacyProfile()
      localStorage.setItem('mc-reading-width', 'full')
      mockFetch(() => okJson({ prefs: { 'mc-reading-width': 'full' } }))
      await reconcileNewDurableKeys()

      localStorage.removeItem('mc-reading-width')
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ 'mc-reading-width': null })
    })

    it('leaves every previously synced fingerprint intact', async () => {
      await warmLegacyProfile()
      mockFetch(() => okJson({ prefs: { 'mc-ui': 'cli' } }))
      await reconcileNewDurableKeys()
      // The old key's fingerprint survived the merge: nothing is re-uploaded.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
      expect(localStorage.getItem('mc-crews-view')).toBe('list')
    })
  })

  describe('mc-chat-config per-field merge (issue #15236)', () => {
    const CFG = 'mc-chat-config'
    // Mirror the implementation's wire name: `mc-chat-config.<hex(field)>`. The
    // hex encoding keeps a field like `showContextTokens` from putting the
    // substring `token` into the wire key, which the server's credential
    // denylist would reject.
    const hex = (field: string) =>
      [...field].map((c) => c.charCodeAt(0).toString(16).padStart(4, '0')).join('')
    const child = (field: string) => `${CFG}.${hex(field)}`

    it('flushes each chat-config field under its own wire key, never the whole blob', async () => {
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: false }))
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({
        [child('pinLastPrompt')]: 'true',
        [child('hideEmptyFolderBody')]: 'false',
      })
      // The opaque whole-blob key is never on the wire.
      expect(lastPatch(spy)[CFG]).toBeUndefined()
    })

    it('after a reload, uploads ONLY the field this profile changed', async () => {
      // The core guarantee: touching one chat setting sends only that field, so
      // a profile never writes a field it did not change and a second origin's
      // untouched Pin/Compact values on the host are left intact.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: false }))
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      __resetUiPrefsSyncForTests() // page reload: in-memory baseline gone

      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: true }),
      )
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ [child('hideEmptyFolderBody')]: 'true' })
    })

    it('reassembles the host fields into the local blob, filling only missing fields', async () => {
      // Cold phone over the tunnel: storage was dropped, so the blob is absent.
      mockFetch(() =>
        okJson({
          prefs: {
            [child('pinLastPrompt')]: 'true',
            [child('hideEmptyFolderBody')]: 'true',
            [child('sendOnEnter')]: '"ctrl-enter"',
          },
        }),
      )
      expect(await hydrateUiPrefs()).toBe(3)
      expect(JSON.parse(localStorage.getItem(CFG)!)).toEqual({
        pinLastPrompt: true,
        hideEmptyFolderBody: true,
        sendOnEnter: 'ctrl-enter',
      })
    })

    it('keeps a field this profile already set and fills only the ones it is missing', async () => {
      // Local-wins is now PER FIELD: the profile keeps its own Pin while the
      // host supplies the Compact value it never set here.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: false }))
      mockFetch(() =>
        okJson({
          prefs: {
            [child('pinLastPrompt')]: 'true', // host differs -> local keeps its own
            [child('hideEmptyFolderBody')]: 'true', // local absent -> host fills it
          },
        }),
      )
      expect(await hydrateUiPrefs()).toBe(1)
      expect(JSON.parse(localStorage.getItem(CFG)!)).toEqual({
        pinLastPrompt: false,
        hideEmptyFolderBody: true,
      })
    })

    it('does not re-upload a reassembled field on the first flush after hydrate', async () => {
      // Each restored field is baselined, so the next origin reset cannot make
      // this profile echo the host's own value back as if it were a change.
      mockFetch(() =>
        okJson({
          prefs: { [child('pinLastPrompt')]: 'true', [child('hideEmptyFolderBody')]: 'true' },
        }),
      )
      await hydrateUiPrefs()
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('migrates a legacy whole-blob host backup into per-field restore', async () => {
      // An existing user upgrades: the host still holds the pre-split blob under
      // the parent key. Its fields must still reach the cold phone, or the
      // upgrade itself would wipe the settings this feature exists to protect.
      mockFetch(() =>
        okJson({
          prefs: { [CFG]: JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: true }) },
        }),
      )
      expect(await hydrateUiPrefs()).toBe(2)
      expect(JSON.parse(localStorage.getItem(CFG)!)).toEqual({
        pinLastPrompt: true,
        hideEmptyFolderBody: true,
      })
    })

    it('prefers a per-field host value over the legacy whole blob for the same field', async () => {
      mockFetch(() =>
        okJson({
          prefs: {
            [CFG]: JSON.stringify({ pinLastPrompt: false }),
            [child('pinLastPrompt')]: 'true', // the newer per-field write wins
          },
        }),
      )
      await hydrateUiPrefs()
      expect(JSON.parse(localStorage.getItem(CFG)!)).toEqual({ pinLastPrompt: true })
    })

    it('never nulls the legacy whole-blob host key, and withholds children until reconciled', async () => {
      // A profile synced by the whole-blob build persisted a fingerprint for the
      // whole `mc-chat-config` key. On this build that key is the composite
      // parent and never travels whole, so its stale fingerprint is ignored
      // rather than read as "a key the profile holds no more" (which would emit
      // `mc-chat-config: null` and wipe the legacy backup). Its children are
      // ALSO withheld from this first flush -- a legacy upgrade must reconcile
      // them against the host before uploading, or it re-clobbers newer per-field
      // values another origin wrote (finding F2).
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp.0' }))
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      // Nothing goes up: the parent is never nulled and the child is withheld
      // pending reconcile.
      expect(spy).not.toHaveBeenCalled()
    })

    it('a child wire key never carries a credential substring the server would reject (F1)', async () => {
      // `showContextTokens` contains `token`, which the server's DENY_SUBSTRINGS
      // rejects in a key NAME, 400-ing the whole patch. The wire name is hex, so
      // it carries no such substring, and the field still round-trips.
      localStorage.setItem(CFG, JSON.stringify({ showContextTokens: true }))
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      const patch = lastPatch(spy)
      const wireKey = Object.keys(patch)[0]
      expect(wireKey).toBe(child('showContextTokens'))
      expect(wireKey.toLowerCase()).not.toContain('token')
      expect(patch[wireKey]).toBe('true')

      // And it reassembles back to the real field name on restore.
      localStorage.clear()
      __resetUiPrefsSyncForTests()
      mockFetch(() => okJson({ prefs: { [child('showContextTokens')]: 'true' } }))
      await hydrateUiPrefs()
      expect(JSON.parse(localStorage.getItem(CFG)!)).toEqual({ showContextTokens: true })
    })

    it('a warm legacy upgrade baselines children from the host instead of uploading its stale blob (F2)', async () => {
      // Origin B synced the whole blob under the old build (fingerprint for the
      // parent key, no child fingerprints). It upgrades. The host already holds
      // NEWER per-field values that origin A (a fixed build) wrote. B's first
      // contact must adopt-or-baseline per field, never upload its stale fields
      // over A's.
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp.0' }))
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: false }))
      expect(hasUnreconciledKeys()).toBe(true) // the children need reconciling

      mockFetch(() =>
        okJson({
          prefs: {
            [child('pinLastPrompt')]: 'false', // A's newer value differs from B's
            [child('hideEmptyFolderBody')]: 'false',
          },
        }),
      )
      await reconcileNewDurableKeys()

      // B keeps its own fields in use locally (the backup is not a live channel)...
      expect(JSON.parse(localStorage.getItem(CFG)!)).toEqual({
        pinLastPrompt: true,
        hideEmptyFolderBody: false,
      })
      // ...but the first flush uploads NOTHING: every field is baselined, so B
      // cannot overwrite A's host values.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()

      // The moment B changes a field here, that ONE field goes up.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: false, hideEmptyFolderBody: false }))
      const spy2 = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy2)).toEqual({ [child('pinLastPrompt')]: 'false' })
    })

    it('propagates a field the user cleared from the blob as a null for that child key', async () => {
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true, showTimestamps: true }))
      mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      // The user drops one field from the blob.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)).toEqual({ [child('showTimestamps')]: null })
    })

    it('a failed-restore owned parent protects every child field on hydrate (Opus F1)', async () => {
      // A pre-split build recorded a failed restore owning the PARENT key
      // `mc-chat-config` (its markHydrateFailed filtered DURABLE_PREF_KEYS). On
      // the upgraded build needsHydrate() is still true, so hydrate runs with
      // owned = {mc-chat-config}. The user's chosen fields must win over the
      // host's copy -- the parent-ownership must flow to each child.
      localStorage.setItem('mc-ui-prefs-hydrate-pending', JSON.stringify([CFG]))
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true, showTimestamps: false }))

      mockFetch(() =>
        okJson({
          prefs: {
            [child('pinLastPrompt')]: 'false', // host disagrees with the user's choice
            [child('showTimestamps')]: 'true',
          },
        }),
      )
      await hydrateUiPrefs()

      // Owned parent => local wins per field; the host does NOT overwrite them.
      expect(JSON.parse(localStorage.getItem(CFG)!)).toEqual({
        pinLastPrompt: true,
        showTimestamps: false,
      })
    })

    it('a legacy blob expansion is baselined so the first flush cannot clobber (Opus F2)', async () => {
      // A warm-storage profile cold-hydrates: the host holds only the LEGACY
      // whole blob (no child keys). The expansion must be visible to the
      // baseline loop, so every expanded child lands in the synced doc and the
      // first flush uploads nothing over the (newer) host backup.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: false }))
      mockFetch(() =>
        okJson({
          prefs: {
            [CFG]: JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: false }),
          },
        }),
      )
      await hydrateUiPrefs()

      // The children are baselined from the expanded legacy blob -> no flush.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('reconcile legacy-expands a whole-blob host so a warm upgrade cannot clobber it (c375 F1)', async () => {
      // B synced the whole blob under the old build (parent fingerprint, no
      // child fingerprints) and the host still holds ONLY the legacy whole blob
      // -- no fixed build has written child keys yet. Reconcile must expand that
      // legacy blob so each child has a host value to baseline against;
      // otherwise every child reads `undefined`, baselines nothing, and B's
      // first flush uploads its stale fields as the authoritative per-field
      // backup (the #15236 clobber, reinstated for the state every user upgrades
      // through).
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp.0' }))
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: false }))
      expect(hasUnreconciledKeys()).toBe(true)

      // Host holds the LEGACY WHOLE BLOB only -- no child keys.
      mockFetch(() =>
        okJson({
          prefs: {
            [CFG]: JSON.stringify({ pinLastPrompt: true, hideEmptyFolderBody: false }),
          },
        }),
      )
      await reconcileNewDurableKeys()

      // Every child is baselined from the expanded legacy blob, so the first
      // flush uploads NOTHING -- B cannot overwrite the host backup.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('fails the reconcile when a child restore is quota-refused, so the next flush cannot overwrite the host (c609 GPT 6.1 F2)', async () => {
      // Mixed-version profile: it already synced the composite as a whole blob
      // (legacy parent fingerprint) and holds an UNOWNED local pinLastPrompt=true
      // that no fixed build wrote as a child. The host now holds the child
      // pinLastPrompt=false (a newer per-field value). Quota rejects writing the
      // restored child into the blob while the smaller metadata rewrite would
      // succeed. The reconcile must FAIL (keep the pending marker + migration
      // state) rather than mark the child reconciled -- otherwise the first
      // flush uploads the stale local `true` over the host's newer `false`.
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp.0' }))
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      // A prior restore failed, recording an owned-at-failure snapshot that does
      // NOT include pinLastPrompt -- so the host value WINS for it (the local is
      // not kept), and the loop attempts to restore host `false` into the blob.
      localStorage.setItem('mc-ui-prefs-hydrate-pending', JSON.stringify(['mc-crews-view']))
      expect(hasUnreconciledKeys()).toBe(true)

      mockFetch(() => okJson({ prefs: { [child('pinLastPrompt')]: 'false' } }))
      // Refuse ONLY the blob write (the child restore); every other key (the
      // smaller synced-doc metadata) still lands.
      const realSet = Storage.prototype.setItem
      const setSpy = vi
        .spyOn(Storage.prototype, 'setItem')
        .mockImplementation(function (this: Storage, k: string, v: string) {
          if (k === CFG) throw new DOMException('full', 'QuotaExceededError')
          return realSet.call(this, k, v)
        })
      let result: number
      try {
        result = await reconcileNewDurableKeys()
      } finally {
        setSpy.mockRestore()
      }

      // The reconcile reports failure and the pending marker is retained for the
      // next boot's retry; the child was NOT marked reconciled.
      expect(result).toBe(-1)
      expect(localStorage.getItem('mc-ui-prefs-hydrate-pending')).not.toBeNull()

      // The next flush does NOT upload the stale local pinLastPrompt over the
      // host's newer value: the child is still unreconciled (withheld), not
      // baselined-and-uploadable.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      const uploadedChild = spy.mock.calls.length > 0 ? lastPatch(spy)[child('pinLastPrompt')] : undefined
      expect(uploadedChild).toBeUndefined()
    })

    it('skips a child whose host value is not valid JSON rather than nulling a valid field (c375 F2)', async () => {
      // A failed restore left pinLastPrompt owned-by-this-profile locally; the
      // host then serves a malformed (non-JSON) child value. A naive parse would
      // turn it into JSON null and overwrite the valid local field, defaulting
      // it on reload. The malformed child must be skipped, leaving the field.
      vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('ECONNREFUSED'))))
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      await hydrateUiPrefs() // records nothing is owned, local wins thereafter

      mockFetch(() =>
        okJson({
          prefs: {
            [child('pinLastPrompt')]: 'not-json{', // malformed on the host
          },
        }),
      )
      await hydrateUiPrefs()

      // The valid local field survives -- it is NOT replaced by null.
      expect(JSON.parse(localStorage.getItem(CFG)!)).toEqual({ pinLastPrompt: true })
    })

    it('reconciles a field the host holds but the local blob lacks (c380 F1)', async () => {
      // Warm legacy profile: synced the whole blob under the old build (parent
      // fingerprint, no child fingerprints). The host backup holds a field this
      // origin never set locally -- the ordinary state after any ChatConfig
      // field addition, or a partial legacy blob. Reconcile must iterate the
      // UNION of local and host children: a child present only on the host has
      // no local entry, so without adopting+baselining it here the next
      // whole-blob saveChatConfig persists its DEFAULT with no fingerprint and
      // the first flush uploads that default, silently and permanently shadowing
      // the host's newer value (GPT/Opus F1, the exact clobber #15236 fixes).
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp.0' }))
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true })) // local lacks showTimestamps
      expect(hasUnreconciledKeys()).toBe(true)

      // Host backup carries BOTH the local field and a host-only field, as
      // per-field child keys written by another (fixed-build) origin.
      mockFetch(() =>
        okJson({
          prefs: {
            [child('pinLastPrompt')]: 'true',
            [child('showTimestamps')]: 'true', // host-only: never set on this origin
          },
        }),
      )
      await reconcileNewDurableKeys()

      // The host-only field is adopted into the local blob...
      expect(JSON.parse(localStorage.getItem(CFG)!)).toMatchObject({
        pinLastPrompt: true,
        showTimestamps: true,
      })
      // ...and baselined, so the first flush uploads NOTHING -- the default
      // cannot overwrite the host's value for showTimestamps.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('runs the reconcile pass even when the local blob holds no fields (c380 F1 trigger)', async () => {
      // Legacy fingerprint present but the local blob is empty/absent: zero
      // local children. hasUnreconciledKeys must still fire (via the parent
      // trigger marker) so the host-only fields get baselined; otherwise the
      // pass is skipped and the first flush later clobbers them.
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp.0' }))
      localStorage.removeItem(CFG) // no local blob at all -> zero local children
      expect(hasUnreconciledKeys()).toBe(true)

      mockFetch(() => okJson({ prefs: { [child('showTimestamps')]: 'false' } }))
      await reconcileNewDurableKeys()

      expect(JSON.parse(localStorage.getItem(CFG)!)).toMatchObject({ showTimestamps: false })
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(spy).not.toHaveBeenCalled()
    })

    it('records the composite PARENT key in the failed-restore marker, so a downgrade keeps local (c387 F1)', async () => {
      // A legacy warm profile holds a chat-config blob and a reconcile fails
      // (host unreachable). The ownership marker must list the parent
      // `mc-chat-config`, not only hex child keys: a pre-split build reads
      // ownership by parent key and would otherwise treat the parent as unowned
      // and let the stale host blob overwrite the user's local chat settings.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp.0' }))
      mockFetch(() => Promise.reject(new Error('offline')))
      expect(await reconcileNewDurableKeys()).toBe(-1)

      const marker = JSON.parse(localStorage.getItem('mc-ui-prefs-hydrate-pending')!) as string[]
      expect(marker).toContain(CFG) // the parent entry the old reader consults
      expect(marker.some((k) => k.startsWith(`${CFG}.`))).toBe(true) // children still recorded
    })

    it('does not upload a default-filled new field over a newer host value after migration (c387 F2)', async () => {
      // Migration already finished: child fingerprints exist, no legacy parent
      // fingerprint remains. A later release adds `showTimestamps`; `loadChatConfig`
      // fills it with its DEFAULT, which this origin never chose. Another origin
      // backed up a newer value for it. The reconcile must baseline the local
      // default against the host (keep-local-but-baseline) so the first flush
      // never uploads the default and reverts the host's value.
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({ [child('pinLastPrompt')]: 'fp.pin' }), // child print only -> migration done
      )
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: true }), // showTimestamps = default fill
      )
      expect(hasUnreconciledKeys()).toBe(true) // child-print path triggers the pass

      // Host holds a NEWER value for the new field from another origin.
      mockFetch(() => okJson({ prefs: { [child('showTimestamps')]: 'false' } }))
      await reconcileNewDurableKeys()

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      // The default-filled field is NOT uploaded (it would revert the host's
      // 'false'); only an actually-changed field would appear here.
      const put = lastPatch(spy)
      expect(put[child('showTimestamps')]).toBeUndefined()
    })

    it('a post-failure default on a child the profile did NOT hold loses to the host (c404 F1)', async () => {
      // A failed restore recorded ownership for the ONE field the profile held
      // then (`pinLastPrompt`), plus the parent compat key. Afterwards a full
      // `saveChatConfig` fills ~20 DEFAULTS the user never chose. The parent
      // entry must NOT grant ownership to those defaults: a child the user truly
      // held is kept, but a default-filled child the host has a real value for
      // must lose to the host. (Earlier the parent OR-grant kept every default.)
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp' }))
      mockFetch(() => Promise.reject(new Error('offline')))
      expect(await reconcileNewDurableKeys()).toBe(-1) // records the failure marker

      // The marker carries child entries now, so the parent is NOT a blanket grant.
      // A settings-less render then fills the whole blob with defaults.
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: true }), // showTimestamps = default fill
      )
      // Host holds the user's REAL (newer) value for the default-filled field.
      mockFetch(() => okJson({ prefs: { [child('showTimestamps')]: 'false' } }))
      await hydrateUiPrefs()

      const blob = JSON.parse(localStorage.getItem(CFG)!) as Record<string, unknown>
      expect(blob.pinLastPrompt).toBe(true) // the field the profile genuinely held is kept
      expect(blob.showTimestamps).toBe(false) // the post-failure default loses to the host
    })

    it('a fully reconciled profile reports nothing unreconciled and pays no per-boot reconcile GET (c404 F2)', async () => {
      // Once the composite's children are reconciled they carry roster entries,
      // so the trigger (which earlier keyed on any child fingerprint existing --
      // a condition nothing cleared) must now report an EMPTY set. A permanent
      // trigger made every boot run a reconcile GET whose transient miss cost
      // the whole session's backup.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp' }))
      expect(hasUnreconciledKeys()).toBe(true) // legacy blob present -> reconcile needed

      mockFetch(() => okJson({ prefs: { [child('pinLastPrompt')]: 'true' } }))
      await reconcileNewDurableKeys()

      // After reconcile the child is both fingerprinted and in the roster; the
      // legacy parent fingerprint is gone. The pass is DONE -> nothing left.
      expect(hasUnreconciledKeys()).toBe(false)
      expect(storedRoster()!.some((k) => k.startsWith(`${CFG}.`))).toBe(true)
    })

    it('baselines a child the host does NOT hold via the roster so it is never silently dropped (c404 Opus)', async () => {
      // The host backup predates `showTimestamps` (it has only pinLastPrompt as a
      // child), so there is nothing to fingerprint for showTimestamps. It must
      // still be recorded in the roster as reconciled, or it would read as
      // unreconciled on every boot -- withheld from flush forever while a later
      // flush fingerprints it from a never-sent value and drops the user's choice.
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: true }),
      )
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp' }))
      mockFetch(() => okJson({ prefs: { [child('pinLastPrompt')]: 'true' } })) // host lacks showTimestamps
      await reconcileNewDurableKeys()

      // showTimestamps got no fingerprint (host had nothing) but IS in the roster.
      const roster = storedRoster()!
      expect(roster).toContain(child('showTimestamps'))
      expect(hasUnreconciledKeys()).toBe(false) // cleared via the roster, not a fingerprint

      // Now the user's choice flushes normally -- it was NOT silently dropped.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy)[child('showTimestamps')]).toBe('true')
    })

    it('an unrelated plain-key reconcile does not bulk-import host chat-config children (c404 Opus)', async () => {
      // A warm profile that has NEVER touched the composite reconciles one new
      // PLAIN key. The host holds chat-config children from another origin. The
      // reconcile must NOT reach in and adopt them -- that is the cross-origin
      // bulk import design decision 1 rules out, and it would inject children
      // into the agent-writable blob the profile never asked for.
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({ [ROSTER_ENTRY]: JSON.stringify(['mc-nav']) }), // roster lacks a newer plain key
      )
      // 'mc-busy-send-mode' is in DURABLE_PREF_KEYS but not this roster -> fresh plain key.
      expect(hasUnreconciledKeys()).toBe(true)
      expect(localStorage.getItem(CFG)).toBeNull() // composite genuinely untouched

      mockFetch(() =>
        okJson({ prefs: { [child('pinLastPrompt')]: 'true', [child('showTimestamps')]: 'false' } }),
      )
      await reconcileNewDurableKeys()

      // The host's chat-config children were NOT imported into the local blob.
      expect(localStorage.getItem(CFG)).toBeNull()
    })

    it('a successful flush does NOT baseline a withheld child from a value never sent (c412 GPT/Opus)', async () => {
      // A later release added `showTimestamps`; the profile already synced
      // `pinLastPrompt` (it has a fingerprint and a roster entry) but
      // showTimestamps has neither, so it is unreconciled -- withheld from the
      // PUT. The user changes pinLastPrompt, triggering a flush. The successful
      // flush must baseline ONLY pinLastPrompt (the sent key), never fingerprint
      // the withheld showTimestamps from its local value -- or it would read as
      // "synced" forever, no boot reconcile would revisit it, and the user's
      // chosen value would silently never reach the host.
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: true }),
      )
      // Only pinLastPrompt is baselined + in the roster; showTimestamps is new.
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([...DURABLE_PREF_KEYS, child('pinLastPrompt')]),
          [child('pinLastPrompt')]: 'oldfp', // differs, so pinLastPrompt flushes
        }),
      )
      expect(hasUnreconciledKeys()).toBe(true) // showTimestamps is unreconciled

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      // The PUT carried pinLastPrompt but NOT the withheld showTimestamps.
      expect(lastPatch(spy)[child('pinLastPrompt')]).toBe('true')
      expect(lastPatch(spy)[child('showTimestamps')]).toBeUndefined()

      // showTimestamps was NOT baselined by that flush: no fingerprint landed
      // for it, so it is still unreconciled and a boot reconcile will revisit it.
      const prints = JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!) as Record<string, string>
      expect(prints[child('showTimestamps')]).toBeUndefined()
      expect(hasUnreconciledKeys()).toBe(true)
    })

    it('re-upgrade after a downgrade re-baselines a child whose fingerprint was shed (c412 Opus)', async () => {
      // In-place downgrade to a pre-split build sheds the hex child FINGERPRINTS
      // (readSyncedPrints drops unknown keys) but carries the child ROSTER
      // entries through and writes a fresh parent fingerprint. On re-upgrade the
      // reconcile must still re-baseline the host children -- selecting by a
      // missing fingerprint ALONE, not also "not in the roster" -- or the first
      // flush uploads this origin's stale fields over another origin's newer
      // per-field backup (the #15236 clobber).
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      // Downgrade-shed state: roster still lists the child, parent fingerprint
      // present (legacy signal), but the child's OWN fingerprint is gone.
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([...DURABLE_PREF_KEYS, child('pinLastPrompt')]),
          [CFG]: 'legacy.parent.fp',
        }),
      )
      // Host holds a NEWER value another origin backed up.
      mockFetch(() => okJson({ prefs: { [child('pinLastPrompt')]: 'false' } }))
      await reconcileNewDurableKeys()

      // The child was re-baselined from the host (fingerprint now present), so
      // the next flush will not clobber the host's newer value.
      const prints = JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!) as Record<string, string>
      expect(prints[child('pinLastPrompt')]).toBeDefined()
      expect(hasUnreconciledKeys()).toBe(false)
    })

    it('cold hydrate records composite children in the roster (c412 Opus)', async () => {
      // A cold hydrate reconciles every key. A child the LOCAL blob holds but
      // the host lacks gets no fingerprint, so without a roster entry it would
      // read as unreconciled on the next boot -- forcing a reconcile GET whose
      // transient failure costs the whole session's backup. The hydrate roster
      // must list the composite children (host-expanded AND local-blob).
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true, localOnlyField: 'keep' }))
      expect(needsHydrate()).toBe(true) // no synced marker yet
      mockFetch(() => okJson({ prefs: { [child('pinLastPrompt')]: 'true' } })) // host lacks localOnlyField
      await hydrateUiPrefs()

      const roster = storedRoster()!
      expect(roster).toContain(child('pinLastPrompt')) // host child
      expect(roster).toContain(child('localOnlyField')) // local-only child, no fingerprint
      // The local-only child is reconciled (via the roster), so no boot GET.
      expect(hasUnreconciledKeys()).toBe(false)
    })

    it('withholds a legacy-migration child whose fingerprint a pre-split tab shed while the roster survived (c416 Opus)', async () => {
      // The flush-path twin of the reconcile re-baseline fix: while a legacy
      // whole-blob fingerprint is still present (upgrade unfinished), a child's
      // roster entry CANNOT be trusted to mean "reconciled". A pre-split tab
      // left open across the upgrade flushes -> its writeSyncedPrints rebuilds
      // the synced doc from its own `current` (parent blob, no hex children),
      // shedding every child fingerprint, while this tab's reconcile already
      // wrote the roster. unreconciledKeys must mark such children unresolved by
      // a missing fingerprint ALONE here, so buildPatch WITHHOLDS them instead
      // of re-uploading this origin's whole stale blob over another origin's
      // per-field backup (the #15236 clobber this change removes).
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: false }),
      )
      // Shed state: legacy parent fingerprint present, roster lists both
      // children, but neither child has its own fingerprint.
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([
            ...DURABLE_PREF_KEYS,
            child('pinLastPrompt'),
            child('showTimestamps'),
          ]),
          [CFG]: 'legacy.parent.fp',
        }),
      )

      // Both children are unresolved despite being in the roster, because the
      // legacy fingerprint is still present (migration unfinished).
      expect(hasUnreconciledKeys()).toBe(true)

      // A flush now must NOT upload the children (they are withheld); the only
      // reconcile GET baselines them from the host instead of clobbering it.
      const spy = mockFetch(() =>
        okJson({ prefs: { [child('pinLastPrompt')]: 'true', [child('showTimestamps')]: 'true' } }),
      )
      await flushUiPrefs()
      const puts = spy.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'PUT',
      )
      for (const put of puts) {
        const prefs = JSON.parse((put[1] as RequestInit).body as string).prefs as Record<
          string,
          string | null
        >
        // Neither shed child is pushed with this origin's stale local value.
        expect(prefs[child('pinLastPrompt')]).toBeUndefined()
        expect(prefs[child('showTimestamps')]).toBeUndefined()
      }
    })

    it('uploads a composite field the user edited to its DEFAULT value, even unreconciled (c421 GPT F1)', async () => {
      // F1: a withheld child (unreconciled: no fingerprint, no roster entry)
      // drops a value the user DELIBERATELY set, because "equals the default"
      // and "a hook mounted the default" are indistinguishable from the blob
      // alone. The saveChatConfig seam records the explicitly-edited field in
      // the dirty set, and a dirty child uploads even when it equals the known
      // default -- so the user's choice is backed up instead of lost to a later
      // storage reset.
      //
      // Set up the F1 window: a legacy-migrating profile (parent fingerprint
      // present) whose child `showTimestamps` is unreconciled and would be
      // withheld by every flush.
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: false }),
      )
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([...DURABLE_PREF_KEYS, child('showTimestamps')]),
          [CFG]: 'legacy.parent.fp',
        }),
      )
      expect(hasUnreconciledKeys()).toBe(true) // showTimestamps is withheld by default

      // The user explicitly edits showTimestamps -- to its default (false) --
      // which the saveChatConfig seam records as dirty.
      markCompositeFieldsDirty(['showTimestamps'])

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      // The deliberately-edited default value IS uploaded despite being unreconciled.
      expect(lastPatch(spy)[child('showTimestamps')]).toBe('false')

      // The dirty marker cleared once the field landed (its fingerprint now
      // records the real value), so a second flush does not re-upload it --
      // in fact it sends no PUT at all, because nothing changed.
      expect(localStorage.getItem('mc-chat-config-dirty')).toBeNull()
      const spy2 = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      const puts2 = spy2.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'PUT',
      )
      for (const put of puts2) {
        const prefs = JSON.parse((put[1] as RequestInit).body as string).prefs as Record<
          string,
          string | null
        >
        expect(prefs[child('showTimestamps')]).toBeUndefined()
      }
    })

    it('keeps the dirty marker when the fingerprint-doc write fails after a landed PUT (c613 GPT 6.1 F1)', async () => {
      // GPT 6.1 F1 (:1408): commitSent clears a child's dirty marker on the
      // strength of the fingerprint doc it just merged. But if that merge's
      // safeSetItem fails (storage still full after its free-and-retry) while
      // the HOST PUT already landed, the baseline did NOT advance -- the field
      // has no stored fingerprint. Clearing its marker anyway would leave it
      // with neither a fingerprint nor a marker: buildPatch withholds it on
      // every later flush, so a cold restore reinstates the stale host value
      // with no recovery path. The marker MUST survive so the next flush forces
      // the field up again.
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([...DURABLE_PREF_KEYS, child('showTimestamps')]),
          [CFG]: 'legacy.parent.fp',
        }),
      )
      // The user edits showTimestamps; the saveChatConfig seam records it dirty.
      localStorage.setItem(CFG, JSON.stringify({ showTimestamps: true }))
      markCompositeFieldsDirty(['showTimestamps'])
      expect(
        JSON.parse(localStorage.getItem('mc-chat-config-dirty') || '[]') as string[],
      ).toContain('showTimestamps')

      // The PUT lands (host accepts), but the follow-up fingerprint-doc write
      // (SYNCED_KEYS_KEY) is quota-refused: storage is still full.
      const realSetItem = Storage.prototype.setItem
      const quotaErr = new DOMException('quota', 'QuotaExceededError')
      const setSpy = vi
        .spyOn(Storage.prototype, 'setItem')
        .mockImplementation(function (this: Storage, k: string, v: string) {
          if (k === SYNCED_KEYS_KEY) throw quotaErr
          return realSetItem.call(this, k, v)
        })
      const spy = mockFetch(() => okJson({ prefs: {} }))
      try {
        await flushUiPrefs()
      } finally {
        setSpy.mockRestore()
      }
      // The field DID go up (the PUT landed)...
      expect(lastPatch(spy)[child('showTimestamps')]).toBe('true')
      // ...but because the fingerprint doc did not persist, the dirty marker is
      // RETAINED (not cleared) -- the field has no fingerprint, so the marker is
      // the only thing that will force it up again.
      expect(
        JSON.parse(localStorage.getItem('mc-chat-config-dirty') || '[]') as string[],
      ).toContain('showTimestamps')

      // Proof of recovery: a later flush (storage now has room) re-forces the
      // field up, so the user's edit is not lost.
      const spy2 = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect(lastPatch(spy2)[child('showTimestamps')]).toBe('true')
    })

    it('does NOT mark default-filled fields dirty on an unrelated edit of a partial blob (c428 GPT/Opus F1)', () => {
      // F1: the stored blob is routinely PARTIAL -- restore writers merge only
      // host-held fields, and a build upgrade adds fields the old blob lacks.
      // saveChatConfig must compare against the DEFAULT-FILLED prior, not the
      // raw blob: otherwise every field absent from the blob reads as changed
      // on any unrelated edit and force-uploads this origin's defaults over
      // another origin's host values.
      //
      // Partial legacy blob: only pinLastPrompt stored; every other field is
      // absent (and would be undefined in the raw blob).
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      // The user edits ONE unrelated field (pinLastPrompt -> false). Every
      // other field is written at its own default by saveChatConfig (the cfg
      // is default-filled in memory by loadChatConfig).
      const cfg = loadChatConfig()
      cfg.pinLastPrompt = false
      saveChatConfig(cfg)
      // Only the field the user actually changed is dirty -- not the ~20
      // default-filled fields that merely were absent from the partial blob.
      const dirty = JSON.parse(localStorage.getItem('mc-chat-config-dirty') || '[]') as string[]
      expect(dirty).toEqual(['pinLastPrompt'])
    })

    it('does NOT leave a dirty marker when the config write fails (c428 GPT/Opus F2)', () => {
      // F2: the dirty marker must only be recorded after the blob write
      // succeeds. If safeSetItem fails (quota exhausted after reclaim), a
      // marker written ahead of the failed write would force the UN-updated
      // prior value past buildPatch's withholding and overwrite the host
      // backup -- a value that was never even stored locally.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      // Make the blob write fail with a quota error (every setItem of the CFG
      // key throws), while leaving reads intact so loadChatConfig works.
      const realSetItem = Storage.prototype.setItem
      const quotaErr = new DOMException('quota', 'QuotaExceededError')
      const setSpy = vi
        .spyOn(Storage.prototype, 'setItem')
        .mockImplementation(function (this: Storage, k: string, v: string) {
          if (k === CFG) throw quotaErr
          return realSetItem.call(this, k, v)
        })
      try {
        const cfg = loadChatConfig()
        cfg.pinLastPrompt = false
        saveChatConfig(cfg)
      } finally {
        setSpy.mockRestore()
      }
      // The blob write failed, so NO dirty marker was recorded -- nothing will
      // force the stale prior value onto the host.
      expect(localStorage.getItem('mc-chat-config-dirty')).toBeNull()
    })

    it('keeps a dirty (un-uploaded, user-edited) child over the stale host value on hydrate (c463 GPT F1)', async () => {
      // F1: a reconcile GET fails (records the owned-at-failure marker), that
      // session never flushes, and the user then edits a field the stored blob
      // PREDATES. The dirty marker records the edit, but the field is absent
      // from the owned marker (the blob predated it) and `compositeChildOwned`
      // then refuses the parent grant -- so without treating a dirty child as
      // owned, hydrate restores the stale host value AND baselines it, losing
      // the user's edit silently on both sides.
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      localStorage.setItem(SYNCED_KEYS_KEY, JSON.stringify({ [CFG]: 'legacy.fp' }))
      mockFetch(() => Promise.reject(new Error('offline')))
      expect(await reconcileNewDurableKeys()).toBe(-1) // records the failure marker

      // The user edits a blob-predating field (showTimestamps) to a NEW value;
      // the saveChatConfig seam records it dirty.
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: true }),
      )
      markCompositeFieldsDirty(['showTimestamps'])

      // Host holds the STALE value for that field. Hydrate must NOT clobber the
      // user's un-uploaded edit with it, because the dirty marker makes it owned.
      mockFetch(() => okJson({ prefs: { [child('showTimestamps')]: 'false' } }))
      await hydrateUiPrefs()

      const blob = JSON.parse(localStorage.getItem(CFG)!) as Record<string, unknown>
      expect(blob.showTimestamps).toBe(true) // the user's un-uploaded edit survived
    })

    it('keeps the legacy parent fingerprint alive across a flush that withheld the children (c463 Opus BLOCKING)', async () => {
      // The legacy parent fingerprint (mc-chat-config) is the SOLE input to
      // legacyCompositeSynced(), but readSyncedPrints drops it (the parent is
      // not a wire key), so mergeSyncedPrints would rebuild the doc without it.
      // A mid-migration flush that withholds the children must re-plant it, or
      // the next poll reads legacyCompositeSynced() false while the roster still
      // marks the children reconciled -> nothing withheld -> the whole local
      // blob uploads over another origin's per-field host values (#15236).
      //
      // Legacy-migrating profile: parent fingerprint present, children in the
      // roster but NOT fingerprinted (withheld), plus an unrelated plain key
      // the flush will actually send.
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: false }),
      )
      localStorage.setItem('mc-diff-plain', 'true') // an unrelated durable plain key
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([
            ...DURABLE_PREF_KEYS,
            child('pinLastPrompt'),
            child('showTimestamps'),
          ]),
          [CFG]: 'legacy.parent.fp',
        }),
      )
      expect(hasUnreconciledKeys()).toBe(true) // children withheld, migration unfinished

      // Flush an unrelated key; the composite children are withheld this round.
      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      expect((spy.mock.calls.length > 0)).toBe(true)

      // The parent fingerprint must still be in the stored doc, so the next
      // poll still sees legacyCompositeSynced() == true and keeps withholding.
      const doc = JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!) as Record<string, string>
      expect(doc[CFG]).toBe('legacy.parent.fp')
      expect(hasUnreconciledKeys()).toBe(true) // still unfinished -> boot reconcile can still heal
    })

    it('does NOT mark a legacy un-normalized stored value dirty on an unrelated toggle (c463 Opus FINDING)', () => {
      // The stored blob can hold un-migrated legacy values; `cfg` always comes
      // through loadChatConfig()'s normalizing read. The dirty diff must compare
      // against the NORMALIZED prior (loadChatConfig), not the raw blob -- else a
      // legacy value (fileChipStyle:"pebble" -> "expanded") reads as changed on
      // an unrelated toggle and force-uploads that untouched field over the host.
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, fileChipStyle: 'pebble' }),
      )
      // The user edits ONE unrelated field.
      const cfg = loadChatConfig() // fileChipStyle is now normalized to 'expanded'
      cfg.pinLastPrompt = false
      saveChatConfig(cfg)
      // Only the genuinely-edited field is dirty -- the legacy-normalized
      // fileChipStyle must NOT be, even though its raw stored value ("pebble")
      // differs from its normalized value ("expanded").
      const dirty = JSON.parse(localStorage.getItem('mc-chat-config-dirty') || '[]') as string[]
      expect(dirty).toEqual(['pinLastPrompt'])
    })

    it('keeps the legacy parent fingerprint alive across a PER-KEY RETRY that withheld the children (c501 GPT F1 / Opus BLOCKING)', async () => {
      // The c463 test proved commitSent re-plants the parent print on a
      // successful whole-patch flush. This is the OTHER flush path: a whole-patch
      // 4xx (an over-MAX_VALUE_BYTES durable value) routes flushUiPrefs into
      // retryPerKey, which rebuilds the stored doc via mergeSyncedPrints. Before
      // the fix only commitSent captured/re-planted the parent print, so the
      // per-key retry still erased it -- self-destructing the migration guard
      // with no healing path (unreconciledKeys empty, hasUnreconciledKeys false).
      // The fix moves the preservation INTO mergeSyncedPrints, so BOTH paths keep
      // it. (uiPrefs.ts:1070/:1320.)
      localStorage.setItem(
        CFG,
        JSON.stringify({ pinLastPrompt: true, showTimestamps: false }),
      )
      localStorage.setItem('mc-dev-mode', 'true') // an unrelated durable plain key
      localStorage.setItem('kc:file-explorer:state:v2', 'too-big') // the oversized offender
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([
            ...DURABLE_PREF_KEYS,
            child('pinLastPrompt'),
            child('showTimestamps'),
          ]),
          [CFG]: 'legacy.parent.fp',
        }),
      )
      expect(hasUnreconciledKeys()).toBe(true) // children withheld, migration unfinished

      // Whole-patch PUT is refused -> per-key retry; one value is still refused
      // so the retry path genuinely runs mergeSyncedPrints with a partial set.
      mockFetch((_url, init) => {
        const body = JSON.parse((init as RequestInit).body as string)
        const keys = Object.keys(body.prefs)
        if (keys.length > 1) return { ok: false, status: 400, json: () => Promise.resolve({}) }
        if (keys[0] === 'kc:file-explorer:state:v2') {
          return { ok: false, status: 400, json: () => Promise.resolve({}) }
        }
        return okJson({ prefs: {} })
      })
      await flushUiPrefs()

      // The parent fingerprint must survive the per-key retry's merge, so the
      // next poll still sees legacyCompositeSynced() == true and keeps
      // withholding -- the guard, and the boot-reconcile heal path, are intact.
      const doc = JSON.parse(localStorage.getItem(SYNCED_KEYS_KEY)!) as Record<string, string>
      expect(doc[CFG]).toBe('legacy.parent.fp')
      expect(hasUnreconciledKeys()).toBe(true)
    })

    it('rolls the blob back when the config write succeeds but the dirty-marker write fails (c501 GPT F2)', () => {
      // F2 (second half): the marker is its own ~40-byte key, so the blob write
      // can land while the marker write fails under near-full storage. An edited
      // field recorded in the blob but NOT the marker is withheld as an unproven
      // default and baselined without upload -> a cold restore reinstates the
      // stale host value, losing the edit silently. The save must therefore roll
      // the blob back when the marker cannot be persisted, so nothing
      // stored-but-unmarked survives. (ChatSettings.tsx:190.)
      const prior = JSON.stringify({ pinLastPrompt: true, showTimestamps: false })
      localStorage.setItem(CFG, prior)
      // Blob write SUCCEEDS; only the dirty-marker key write fails with quota.
      const realSetItem = Storage.prototype.setItem
      const quotaErr = new DOMException('quota', 'QuotaExceededError')
      const setSpy = vi
        .spyOn(Storage.prototype, 'setItem')
        .mockImplementation(function (this: Storage, k: string, v: string) {
          if (k === 'mc-chat-config-dirty') throw quotaErr
          return realSetItem.call(this, k, v)
        })
      try {
        const cfg = loadChatConfig()
        cfg.showTimestamps = true // edit a composite field -> would be marked dirty
        saveChatConfig(cfg)
      } finally {
        setSpy.mockRestore()
      }
      // No marker (its write failed) AND the blob was rolled back to the prior,
      // so there is no stored-but-unmarked edit the sync could mishandle.
      expect(localStorage.getItem('mc-chat-config-dirty')).toBeNull()
      expect(localStorage.getItem(CFG)).toBe(prior)
    })

    it('rolls the stored blob back AND returns false when the dirty-marker write fails, so the caller can surface the failure (c561 GPT 6.1 F1 / errors-use-error-notice)', () => {
      // The marker rollback keeps the stored blob and its dirty markers
      // consistent: when the blob write lands but the ~40-byte marker write
      // fails under near-full storage, an edited field recorded in the blob but
      // NOT the marker would be withheld as an unproven default and baselined
      // without uploading, losing the edit on both sides. The remedy rolls the
      // blob back to the prior raw value so nothing is left stored that the sync
      // would mishandle, AND returns false so `ChatPanel.setChat` can render the
      // failure through ErrorNotice instead of showing an un-persisted value as
      // if it saved (GPT 6.1 F1). A successful save returns true.
      localStorage.setItem(CFG, JSON.stringify({ showTimestamps: false }))

      // Success path: the edit is stored AND the save reports success.
      const okCfg = loadChatConfig()
      okCfg.showTimestamps = true
      expect(saveChatConfig(okCfg)).toBe(true)
      expect(JSON.parse(localStorage.getItem(CFG)!).showTimestamps).toBe(true)

      // Marker-write failure -> blob rolled back AND the save reports failure.
      const priorRaw = localStorage.getItem(CFG)
      const realSetItem = Storage.prototype.setItem
      const quotaErr = new DOMException('quota', 'QuotaExceededError')
      const setSpy = vi
        .spyOn(Storage.prototype, 'setItem')
        .mockImplementation(function (this: Storage, k: string, v: string) {
          if (k === 'mc-chat-config-dirty') throw quotaErr
          return realSetItem.call(this, k, v)
        })
      try {
        const cfg = loadChatConfig()
        cfg.showTimestamps = false // a real edit -> would be marked dirty
        expect(saveChatConfig(cfg)).toBe(false)
        // Blob rolled back: nothing stored-but-unmarked for the sync to mishandle.
        expect(localStorage.getItem(CFG)).toBe(priorRaw)
      } finally {
        setSpy.mockRestore()
      }
    })

    it('editing one chat setting does NOT upload the other default fields over the host (c504 GPT 6.1)', async () => {
      // A warm synced profile that has synced OTHER keys but never stored a
      // chat-config child (any second origin/device whose first chat edit comes
      // after the upgrade) counts the composite as untouched, so nothing is
      // withheld. If saveChatConfig persisted the whole loadChatConfig()
      // default-filled cfg, all ~21 children would materialize with no
      // fingerprints and buildPatch would upload every default -- overwriting a
      // newer host value (e.g. showTimestamps) with the local default, no
      // recovery. The fix persists ONLY the changed field merged into the prior
      // raw blob, so the unedited fields stay ABSENT, stay unreconciled, and are
      // NOT sent. (ChatSettings.tsx:202.)
      //
      // Warm synced profile: a plain key is synced + rostered, but the stored
      // chat-config blob has only pinLastPrompt (no showTimestamps child); the
      // roster does NOT list any chat child (never stored one).
      localStorage.setItem('mc-dev-mode', 'true')
      localStorage.setItem(CFG, JSON.stringify({ pinLastPrompt: true }))
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([...DURABLE_PREF_KEYS]),
          'mc-dev-mode': 'devmode.fp',
        }),
      )
      __resetUiPrefsSyncForTests() // reload: in-memory baseline gone

      // The user toggles ONE field (pinLastPrompt). The host holds a NEWER value
      // for an UNRELATED field (showTimestamps) that this profile never stored.
      const cfg = loadChatConfig()
      cfg.pinLastPrompt = false
      saveChatConfig(cfg)

      const spy = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs()
      const patch = lastPatch(spy)
      // Only the edited field's child is uploaded; the untouched default fields
      // (e.g. showTimestamps) must be ABSENT from the patch, so the host keeps
      // whatever newer value another origin wrote.
      expect(patch[child('pinLastPrompt')]).toBe(JSON.stringify(false))
      expect(patch[child('showTimestamps')]).toBeUndefined()
      // The stored blob must stay partial (only the edited field + the prior
      // field), never the full default-filled set.
      const blob = JSON.parse(localStorage.getItem(CFG)!) as Record<string, unknown>
      expect(Object.keys(blob).sort()).toEqual(['pinLastPrompt'])
    })

    it('a legacy field too deeply nested to serialize does NOT crash boot; valid siblings survive (c516 GPT 6.1 F1)', () => {
      // A legacy backup can hold a value nested thousands of arrays deep -- it
      // fits the store's size cap but overflows the JS call stack when
      // JSON.stringify recurses. A bare stringify of that field in
      // expandComposite() raised an uncaught RangeError inside
      // hasUnreconciledKeys() -- before React mounts -- crashing the dashboard
      // on boot with no recovery, and the raw-blob merge in saveChatConfig()
      // threw the same way. The fix catches per-field and drops only the
      // unserializable field, keeping every valid sibling. (uiPrefs.ts:162.)
      // At this depth JSON.parse of the stored blob SUCCEEDS (so the composite
      // parses and the field materializes), but JSON.stringify of the parsed
      // field OVERFLOWS the stack -- exactly GPT's scenario: the value fits the
      // store yet re-serializing it crashes. Build the JSON by hand so the TEST
      // setup never stringifies the deep value itself.
      const depth = 8000
      const deepJson = '['.repeat(depth) + '0' + ']'.repeat(depth)
      localStorage.setItem(CFG, `{"showTimestamps":true,"pinLastPrompt":${deepJson}}`)
      __resetUiPrefsSyncForTests()

      // The boot path must NOT throw: expandComposite drops the bad field and
      // the valid sibling still registers as an unreconciled child.
      expect(() => hasUnreconciledKeys()).not.toThrow()

      // A later edit must NOT throw either: saveChatConfig's raw-blob merge
      // carries the bad field forward from priorBlob, and the serialize guard
      // excludes it while persisting the edited + valid fields.
      const cfg = loadChatConfig()
      cfg.showTimestamps = false
      expect(() => saveChatConfig(cfg)).not.toThrow()
      const blob = JSON.parse(localStorage.getItem(CFG)!) as Record<string, unknown>
      // The unserializable field is gone; the valid edited field is kept.
      expect(blob.pinLastPrompt).toBeUndefined()
      expect(blob.showTimestamps).toBe(false)
    })

    it('an unserializable prior value in the DIRTY COMPARISON does NOT crash the save; the serializable replacement persists (c558 GPT 6.1 F1)', () => {
      // Distinct from the expandComposite / nextBlob guard above: this is the
      // dirty-comparison loop in saveChatConfig. `base` comes from
      // loadChatConfig(), which does NOT type-check collapseAllSteps, so a
      // hand-tampered / host-backup value nested thousands of arrays deep gets
      // through. The compare line `JSON.stringify(base[field]) !==
      // JSON.stringify(value)` then threw an uncaught RangeError on
      // JSON.stringify(base[field]) -- BEFORE the nextBlob serialize guard is
      // ever reached and before the replacement boolean is persisted, so the
      // "Show thinking inline" toggle (and the setChat updater calling it) died
      // with no recovery. The fix guards the compare serialization and treats
      // an unserializable prior value as changed, so the serializable
      // replacement is written -- exactly the repair the user is performing.
      // (ChatSettings.tsx:189.)
      //
      // At depth 8000, JSON.parse of the stored blob SUCCEEDS (collapseAllSteps
      // materializes as a deep array) but JSON.stringify of it OVERFLOWS the
      // stack. Build the blob JSON by hand so the TEST setup never stringifies
      // the deep value itself.
      const depth = 8000
      const deepJson = '['.repeat(depth) + '0' + ']'.repeat(depth)
      localStorage.setItem(CFG, `{"collapseAllSteps":${deepJson},"showTimestamps":true}`)
      __resetUiPrefsSyncForTests()

      // loadChatConfig keeps the deep array (collapseAllSteps isn't type-checked
      // there), so the compare loop meets it. The save must NOT throw.
      const cfg = loadChatConfig()
      cfg.collapseAllSteps = false // the user's "Show thinking inline" toggle
      expect(() => saveChatConfig(cfg)).not.toThrow()

      // The serializable replacement boolean persisted; the deep array is gone.
      const blob = JSON.parse(localStorage.getItem(CFG)!) as Record<string, unknown>
      expect(blob.collapseAllSteps).toBe(false)
    })

    it('an unrelated PUT ack does NOT discard a chat edit made while it was in flight (c549 GPT 6.1 F1)', async () => {
      // The hazard: commitSent cleared the dirty marker of EVERY non-withheld
      // child in the live snapshot `current`, not just the children actually in
      // the acknowledged patch. So a user editing one chat field while another
      // field's PUT was in flight had that edit's marker cleared on the
      // unrelated ack -> the next flush skipped it (equals default/baseline, not
      // dirty) and a cold restore lost it. The fix passes the acknowledged patch
      // keys into commitSent and clears markers only for children in that
      // request. (uiPrefs.ts:1297.)
      //
      // Start from a profile whose chat composite is already fully reconciled
      // (a roster listing its child) so editing it does not pull in migration
      // or withholding behaviour -- the only thing under test is marker survival
      // across an unrelated ack. Establish the real sync baseline with one
      // successful flush, so the chat child is genuinely "unchanged" (its stored
      // fingerprint matches) before the in-flight scenario begins.
      localStorage.setItem('mc-dev-mode', 'true') // an unrelated durable plain key
      localStorage.setItem(CFG, JSON.stringify({ showTimestamps: false }))
      localStorage.setItem(
        SYNCED_KEYS_KEY,
        JSON.stringify({
          [ROSTER_ENTRY]: JSON.stringify([
            ...DURABLE_PREF_KEYS,
            child('showTimestamps'),
          ]),
        }),
      )
      __resetUiPrefsSyncForTests() // reload: in-memory baseline gone
      const baseline = mockFetch(() => okJson({ prefs: {} }))
      await flushUiPrefs() // baselines mc-dev-mode + the chat child at their stored values
      baseline.mockReset()

      // Now the user changes the UNRELATED plain key; hold its PUT open so a
      // chat edit can land mid-flight.
      localStorage.setItem('mc-dev-mode', 'false')
      let release: (() => void) | undefined
      const gate = new Promise<void>((r) => { release = r })
      let calls = 0
      const spy = vi.fn((_url: string, _init?: RequestInit) => {
        calls += 1
        return calls === 1
          ? gate.then(() => okJson({ prefs: {} }))
          : Promise.resolve(okJson({ prefs: {} }))
      })
      vi.stubGlobal('fetch', spy as unknown as typeof fetch)

      const first = flushUiPrefs() // snapshot: only mc-dev-mode changed
      // Mid-flight: the user edits the chat field. saveChatConfig marks it dirty.
      const cfg = loadChatConfig()
      cfg.showTimestamps = true
      saveChatConfig(cfg)
      await flushUiPrefs() // in-flight: sets dirtyDuringFlush, returns at once
      release!()
      await first

      // The first PUT acknowledged ONLY mc-dev-mode. commitSent must not have
      // cleared the chat child's dirty marker, so the chained flush still forces
      // it up.
      const puts = spy.mock.calls.map((c) =>
        JSON.parse((c[1] as RequestInit).body as string).prefs as Record<string, unknown>,
      )
      // Round 1: just the plain key -- the chat child was edited AFTER this
      // snapshot and must not ride along, nor be dropped by its ack.
      expect(Object.keys(puts[0])).toEqual(['mc-dev-mode'])
      // A later round uploads the chat child because its dirty marker survived
      // the unrelated ack.
      const sawChild = puts
        .slice(1)
        .some((p) => p[child('showTimestamps')] === JSON.stringify(true))
      expect(sawChild).toBe(true)
    })
  })
})

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
