/**
 * appNavHidden — the pin-persistence contract for the sidebar Apps group
 * (PR3 Library launchpad).
 *
 * The module owns the `mc-app-nav-hidden` localStorage key (a JSON string
 * array of HIDDEN app nav ids) and the same-tab sync event
 * `mc:app-nav-hidden-changed`. Both the LibraryPage tiles and the App.tsx
 * sidebar filter read/write through it, so the contract pinned here is what
 * keeps the two surfaces agreeing:
 *
 *  - an id ABSENT from storage is visible (pinned) — new installs need no
 *    migration;
 *  - malformed or tampered storage degrades to "everything visible", never
 *    a throw;
 *  - every persisted change dispatches the sync event (same-tab
 *    localStorage writes do not fire `storage`).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  APP_NAV_HIDDEN_CHANGED_EVENT,
  APP_NAV_HIDDEN_KEY,
  buildReorderBaseline,
  readAppNavOrder,
  mergeVisibleReorder,
  readAppNavHidden,
  subscribeAppNavHidden,
  toggleAppNavHidden,
  useAppNavHidden,
} from './appNavHidden'

beforeEach(() => {
  localStorage.clear()
})

describe('readAppNavHidden — defaults and malformed storage', () => {
  it('returns the empty set when the key is absent (everything pinned)', () => {
    expect(readAppNavHidden().size).toBe(0)
    expect(readAppNavHidden().has('app-secretary')).toBe(false)
  })

  it('treats malformed JSON as the empty set instead of throwing', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '{not json[')
    expect(readAppNavHidden().size).toBe(0)
    expect(readAppNavHidden().has('app-secretary')).toBe(false)
  })

  it('treats a non-array JSON value as the empty set', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '{"app-secretary": true}')
    expect(readAppNavHidden().size).toBe(0)
  })

  it('drops non-string entries from a tampered array, keeping the strings', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '["app-secretary", 7, null, {"x":1}]')
    expect([...readAppNavHidden()]).toEqual(['app-secretary'])
  })
})

describe('writes — persistence and the same-tab sync event', () => {
  it('toggleAppNavHidden persists the id and dispatches the change event', () => {
    const listener = vi.fn()
    window.addEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, listener)
    try {
      toggleAppNavHidden('app-secretary')
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
      expect(listener).toHaveBeenCalledTimes(1)

      toggleAppNavHidden('app-secretary')
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual([])
      expect(listener).toHaveBeenCalledTimes(2)
    } finally {
      window.removeEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, listener)
    }
  })

  it('toggleAppNavHidden round-trips and reports the NEW hidden state', () => {
    expect(toggleAppNavHidden('app-secretary')).toBe(true)
    expect(readAppNavHidden().has('app-secretary')).toBe(true)
    expect(toggleAppNavHidden('app-secretary')).toBe(false)
    expect(readAppNavHidden().has('app-secretary')).toBe(false)
  })

  it('stores a stable sorted array so repeated writes are byte-identical', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify([...readAppNavHidden(), 'zeta'].sort()))
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify([...readAppNavHidden(), 'alpha'].sort()))
    expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['alpha', 'zeta'])
  })

  it('recovers a malformed stored value on the next write', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, 'garbage')
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify([...readAppNavHidden(), 'app-secretary'].sort()))
    expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
  })
})

describe('subscribeAppNavHidden', () => {
  it('notifies on writes and stops after unsubscribe', () => {
    const listener = vi.fn()
    const unsubscribe = subscribeAppNavHidden(listener)
    toggleAppNavHidden('app-secretary')
    expect(listener).toHaveBeenCalledTimes(1)

    unsubscribe()
    toggleAppNavHidden('app-secretary')
    expect(listener).toHaveBeenCalledTimes(1)
  })
})

describe('useAppNavHidden — the live React view', () => {
  it('initialises from storage', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['app-secretary']))
    const { result } = renderHook(() => useAppNavHidden())
    expect(result.current.has('app-secretary')).toBe(true)
    expect(result.current.has('app-radar')).toBe(false)
  })

  it('re-reads on the module’s own change event (same-tab writes)', () => {
    const { result } = renderHook(() => useAppNavHidden())
    expect(result.current.size).toBe(0)
    act(() => { toggleAppNavHidden('app-secretary') })
    expect(result.current.has('app-secretary')).toBe(true)
    act(() => { toggleAppNavHidden('app-secretary') })
    expect(result.current.has('app-secretary')).toBe(false)
  })

  it('re-reads on a native storage event for the key (another-tab writes)', () => {
    const { result } = renderHook(() => useAppNavHidden())
    // A cross-tab write lands in localStorage WITHOUT the module's event —
    // only the native `storage` event announces it here.
    act(() => {
      localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['app-secretary']))
      window.dispatchEvent(new StorageEvent('storage', { key: APP_NAV_HIDDEN_KEY }))
    })
    expect(result.current.has('app-secretary')).toBe(true)
  })

  it('ignores storage events for other keys', () => {
    const { result } = renderHook(() => useAppNavHidden())
    act(() => {
      localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['app-secretary']))
      window.dispatchEvent(new StorageEvent('storage', { key: 'some-other-key' }))
    })
    // The write is invisible until an event for OUR key arrives.
    expect(result.current.has('app-secretary')).toBe(false)
  })

  it('stops listening after unmount', () => {
    const { result, unmount } = renderHook(() => useAppNavHidden())
    unmount()
    // Neither path may throw or update after cleanup.
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify([...readAppNavHidden(), 'app-secretary'].sort()))
    window.dispatchEvent(new StorageEvent('storage', { key: APP_NAV_HIDDEN_KEY }))
    expect(result.current.size).toBe(0)
  })
})

describe('readAppNavOrder', () => {
  // Writers must merge into the FRESH stored order (another tab may have
  // reordered since this tab's React copy was set) — this is the read they
  // use, so its degrade behavior is what stands between a malformed value
  // and a crash inside a drag handler.
  it('reads the stored order and degrades malformed data to the empty array', () => {
    expect(readAppNavOrder()).toEqual([])
    localStorage.setItem('mc-app-nav-order', JSON.stringify(['a', 'b']))
    expect(readAppNavOrder()).toEqual(['a', 'b'])
    localStorage.setItem('mc-app-nav-order', 'not json')
    expect(readAppNavOrder()).toEqual([])
    localStorage.setItem('mc-app-nav-order', JSON.stringify({ nope: 1 }))
    expect(readAppNavOrder()).toEqual([])
    localStorage.setItem('mc-app-nav-order', JSON.stringify(['ok', 42, null]))
    expect(readAppNavOrder()).toEqual(['ok'])
  })
})

describe('mergeVisibleReorder', () => {
  // The GPT-review repro this function exists for: B is hidden with a saved
  // middle slot; the user reorders visible A/C; B's slot must survive so
  // re-pinning B restores it between them, never at the end.
  it('keeps a hidden id in its saved slot across a visible reorder', () => {
    const prev = ['a', 'b', 'c']          // b is hidden
    const visible = ['a', 'c']
    const moved = ['c', 'a']              // user dragged C before A
    expect(mergeVisibleReorder(prev, visible, moved)).toEqual(['c', 'b', 'a'])
  })

  it('appends visible ids the previous order never saw, in dragged order', () => {
    const prev = ['a']                    // d and e are new apps, never ordered
    const visible = ['a', 'd', 'e']
    const moved = ['d', 'a', 'e']
    expect(mergeVisibleReorder(prev, visible, moved)).toEqual(['d', 'a', 'e'])
  })

  it('reduces to the dragged order when the full order has no other ids', () => {
    expect(mergeVisibleReorder(['a', 'b'], ['a', 'b'], ['b', 'a'])).toEqual(['b', 'a'])
  })

  // The round-2 GPT repro: mc-app-nav-order is EMPTY, so a hidden app's slot
  // exists only implicitly in the natural order. The caller must seed the
  // merge with the effective full order (hidden included) — given that seed,
  // the hidden id keeps its implicit middle slot on the first-ever reorder.
  it('keeps an implicitly-positioned hidden id when seeded with the natural order', () => {
    const naturalFullOrder = ['a', 'b', 'c']  // b hidden, never persisted
    const visible = ['a', 'c']
    const moved = ['c', 'a']
    expect(mergeVisibleReorder(naturalFullOrder, visible, moved)).toEqual(['c', 'b', 'a'])
  })

  // The round-3 GPT repro: a hidden app that is currently DISABLED has no
  // nav row, so the effective order alone omits it — the baseline must keep
  // every persisted id even when nothing currently renders it.
  it('baseline keeps a persisted id that has no current nav row', () => {
    expect(buildReorderBaseline(['a', 'gone', 'c'], ['a', 'c', 'new']))
      .toEqual(['a', 'gone', 'c', 'new'])
    // ...and the merge then carries it through an unrelated drag untouched.
    const baseline = buildReorderBaseline(['a', 'gone', 'c'], ['a', 'c'])
    expect(mergeVisibleReorder(baseline, ['a', 'c'], ['c', 'a']))
      .toEqual(['c', 'gone', 'a'])
  })

  it('baseline falls back to the effective order when nothing was persisted', () => {
    expect(buildReorderBaseline([], ['a', 'b', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('preserves multiple hidden slots interleaved with visible ones', () => {
    const prev = ['h1', 'a', 'h2', 'b', 'c']  // h1/h2 hidden
    const visible = ['a', 'b', 'c']
    const moved = ['b', 'c', 'a']
    expect(mergeVisibleReorder(prev, visible, moved)).toEqual(['h1', 'b', 'h2', 'c', 'a'])
  })
})
