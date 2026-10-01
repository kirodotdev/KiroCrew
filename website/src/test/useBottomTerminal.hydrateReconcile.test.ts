/**
 * Hydrate-time reconciliation of persisted dock terminal tabs (#10977).
 *
 * `loadPersisted` restores tab ids without asking the backend which PTYs still
 * exist, so a tab leaked before its dispatch's rollback could run — or one whose
 * shell the orphan reaper has killed — came back on every reload and kept
 * consuming the tab cap (#10822: five restored tabs, three already dead).
 *
 * Pins the store half of the two-look protocol: the restored set is flagged
 * pending at module init; `reconcileRestoredTabs` names the restored tabs the
 * first `GET /api/terminal/sessions` body omits or reports dead as SUSPECTS
 * without dropping them (the route skips a session another window is still
 * opening, so absent-once is not gone); `confirmRestoredTabs` drops the suspects
 * a second, uncached answer still misses and keeps the ones it now lists live.
 * An answer that does not rule keeps every tab, and tabs minted after boot are
 * never candidates. Each case boots the module fresh from seeded storage,
 * because the restored set is what module init computes.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

const STORAGE_KEY = 'mc-bottom-terminal'

type Store = typeof import('../hooks/useBottomTerminal')

let current: Store | null = null

/** Persist `tabs`, then boot the store module fresh so module init restores them. */
async function bootWith(tabs: { id: string; cwd?: string }[], extra: Record<string, unknown> = {}): Promise<Store> {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ open: true, tabs, activeId: tabs[0]?.id ?? null, ...extra }))
  vi.resetModules()
  current = await import('../hooks/useBottomTerminal')
  return current
}

const persistedTabIds = () => {
  const raw = localStorage.getItem(STORAGE_KEY)
  return raw ? (JSON.parse(raw) as { tabs: { id: string }[] }).tabs.map(t => t.id) : []
}

const listing = (...entries: [string, boolean][]) => ({
  enabled: true,
  sessions: entries.map(([session_id, alive]) => ({ session_id, alive })),
})

afterEach(() => {
  current?.__resetBottomTerminal()
  current = null
  localStorage.clear()
})

describe('useBottomTerminal — hydrate-time reconciliation', () => {
  it('flags the restored set as pending only when storage held tabs', async () => {
    const empty = await bootWith([])
    expect(empty.isTerminalHydratePending()).toBe(false)

    const restored = await bootWith([{ id: 'a' }])
    expect(restored.isTerminalHydratePending()).toBe(true)
  })

  it('first look names absent and dead restored tabs as suspects without dropping them', async () => {
    const store = await bootWith([{ id: 'live' }, { id: 'dead' }, { id: 'gone' }])

    const suspects = store.reconcileRestoredTabs(listing(['live', true], ['dead', false]))

    expect(suspects).toEqual(['dead', 'gone'])
    // Still every tab, still gated: absent once is not yet gone.
    expect(persistedTabIds()).toEqual(['live', 'dead', 'gone'])
    expect(store.hasTab('gone')).toBe(true)
    expect(store.isTerminalHydratePending()).toBe(true)
  })

  it('confirm look drops the suspects still missing, keeps the live one, refocuses and persists', async () => {
    const store = await bootWith([{ id: 'live' }, { id: 'dead' }, { id: 'gone' }], { activeId: 'gone' })
    store.reconcileRestoredTabs(listing(['live', true], ['dead', false]))

    const dropped = store.confirmRestoredTabs(listing(['live', true], ['dead', false]))

    expect(dropped).toEqual(['dead', 'gone'])
    expect(store.hasTab('live')).toBe(true)
    expect(store.hasTab('dead')).toBe(false)
    expect(store.hasTab('gone')).toBe(false)
    expect(store.isTerminalHydratePending()).toBe(false)
    // The dropped active tab hands focus to a survivor, and the trimmed list
    // is what the next reload (and the other window) reads back.
    expect(persistedTabIds()).toEqual(['live'])
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).activeId).toBe('live')
  })

  it('keeps a suspect the confirm look lists live — a shell another window was opening', async () => {
    const store = await bootWith([{ id: 'opening' }, { id: 'gone' }])
    expect(store.reconcileRestoredTabs(listing())).toEqual(['opening', 'gone'])

    const dropped = store.confirmRestoredTabs(listing(['opening', true]))

    expect(dropped).toEqual(['gone'])
    expect(store.hasTab('opening')).toBe(true)
    expect(store.hasTab('gone')).toBe(false)
    expect(store.isTerminalHydratePending()).toBe(false)
  })

  it('reuseCurrentTab refuses a still-pending restored tab, then reuses it once hydration settles', async () => {
    // #11641 F1: a restored tab is a hydration SUSPECT until a live probe
    // confirms it. Reusing it before then copies the command against a tab
    // reconciliation may drop, leaving no terminal AND no fresh-tab fallback.
    const store = await bootWith([{ id: 'restored' }])
    expect(store.isTerminalHydratePending()).toBe(true)

    let reused: string | null = 'x'
    act(() => { reused = store.reuseCurrentTab() })
    // Null while pending -> the caller mints a fresh tab it fully owns.
    expect(reused).toBeNull()

    // The probe confirms the shell is live; hydration settles.
    store.reconcileRestoredTabs(listing(['restored', true]))
    expect(store.isTerminalHydratePending()).toBe(false)

    act(() => { reused = store.reuseCurrentTab() })
    // Now it is safe to reuse the confirmed tab.
    expect(reused).toBe('restored')
  })

  it('settles at once, keeping every tab, when the first look finds no suspect', async () => {
    const store = await bootWith([{ id: 'a' }, { id: 'b' }])

    expect(store.reconcileRestoredTabs(listing(['a', true], ['b', true]))).toEqual([])

    expect(store.isTerminalHydratePending()).toBe(false)
    expect(persistedTabIds()).toEqual(['a', 'b'])
    // Nothing left to confirm: a stray confirm call changes nothing.
    expect(store.confirmRestoredTabs(listing())).toEqual([])
    expect(persistedTabIds()).toEqual(['a', 'b'])
  })

  it('hides the panel when every restored tab is confirmed gone', async () => {
    const store = await bootWith([{ id: 'a' }, { id: 'b' }])
    store.reconcileRestoredTabs(listing())

    store.confirmRestoredTabs(listing())

    expect(persistedTabIds()).toEqual([])
    expect(store.isBottomTerminalOpen()).toBe(false)
  })

  it('never drops a tab minted after boot, even when neither look lists it', async () => {
    const store = await bootWith([{ id: 'restored' }])
    const fresh = store.addTab()
    expect(fresh).not.toBeNull()

    expect(store.reconcileRestoredTabs(listing())).toEqual(['restored'])
    store.confirmRestoredTabs(listing())

    expect(store.hasTab('restored')).toBe(false)
    expect(store.hasTab(fresh!)).toBe(true)
  })

  it.each([
    ['a failed probe', null],
    ['the feature-disabled answer', { enabled: false, sessions: [] }],
    ['a body without a session list', { enabled: true }],
    ['a malformed session entry', { enabled: true, sessions: [{ session_id: 'a' }] }],
    ['a non-object body', 'nope'],
  ])('first look keeps every restored tab on %s, and settles', async (_label, payload) => {
    const store = await bootWith([{ id: 'a' }, { id: 'b' }])

    expect(store.reconcileRestoredTabs(payload)).toEqual([])

    expect(store.hasTab('a')).toBe(true)
    expect(store.hasTab('b')).toBe(true)
    expect(store.isTerminalHydratePending()).toBe(false)
  })

  it('confirm look that cannot rule keeps every suspect, and settles', async () => {
    const store = await bootWith([{ id: 'a' }, { id: 'b' }])
    expect(store.reconcileRestoredTabs(listing())).toEqual(['a', 'b'])

    expect(store.confirmRestoredTabs(null)).toEqual([])

    expect(store.hasTab('a')).toBe(true)
    expect(store.hasTab('b')).toBe(true)
    expect(store.isTerminalHydratePending()).toBe(false)
  })

  it('rules once per load: a later first look cannot reopen a settled set', async () => {
    const store = await bootWith([{ id: 'a' }])

    store.reconcileRestoredTabs(null)
    expect(store.reconcileRestoredTabs(listing())).toEqual([])
    expect(store.confirmRestoredTabs(listing())).toEqual([])

    expect(store.hasTab('a')).toBe(true)
  })

  it('a repeated first look while confirming is a no-op, so a query refetch cannot double-rule', async () => {
    const store = await bootWith([{ id: 'a' }])
    expect(store.reconcileRestoredTabs(listing())).toEqual(['a'])

    expect(store.reconcileRestoredTabs(listing(['a', true]))).toEqual([])
    expect(store.isTerminalHydratePending()).toBe(true)

    expect(store.confirmRestoredTabs(listing())).toEqual(['a'])
  })

  it('notifies subscribers at each phase change', async () => {
    const store = await bootWith([{ id: 'a' }, { id: 'b' }])
    const { result } = renderHook(() => store.useTerminalHydratePending())
    expect(result.current).toBe(true)

    act(() => { store.reconcileRestoredTabs(listing(['a', true])) })
    expect(result.current).toBe(true)

    act(() => { store.confirmRestoredTabs(listing(['a', true], ['b', true])) })
    expect(result.current).toBe(false)
    expect(store.hasTab('b')).toBe(true)
  })
})
