/**
 * Tests for the `app` panel tab kind — the MCP App host.
 *
 * The load-bearing invariant these pin: an MCP App's iframe is null-origin
 * (`sandbox="allow-scripts allow-forms"`, no `allow-same-origin`) with no
 * storage, so unmounting it reloads the app and destroys whatever the user has
 * drawn. See `src/kiro_crew/docs/dashboard-iframe-hosts.md`.
 *
 * Two ways that could happen, both covered here:
 *   1. registering `app` as a ViewKind — SidePanel unmounts category views on
 *      tab switch (`if (!isActive) return null`);
 *   2. the `activityOpen` gate in ChatPage unmounting the whole SidePanel
 *      subtree when the panel is closed.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePanelTabs, __resetPanelTabs, PINNED_VIEWS, MAX_APP_TABS_PER_CHAT, claimAppAutoOpen, useAllAppTabs, useAnyLiveAppTab } from '../hooks/usePanelTabs'

beforeEach(() => { __resetPanelTabs() })

describe('app panel tab kind', () => {
  it('openApp creates an app tab keyed by tool-call id and focuses it', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })

    const tab = result.current.tabs.find(t => t.kind === 'app')
    expect(tab).toBeDefined()
    expect(tab!.id).toBe('app:call-abc')
    expect(tab!.appToolCallId).toBe('call-abc')
    expect(tab!.slot).toBe('slot-1')
    expect(result.current.activeId).toBe('app:call-abc')
  })

  it('re-rendering the same app refocuses its tab instead of stacking duplicates', () => {
    // A streaming render fires repeatedly for one tool call; each must not add a tab.
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    act(() => { result.current.openView('files') })
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })

    expect(result.current.tabs.filter(t => t.kind === 'app')).toHaveLength(1)
    expect(result.current.activeId).toBe('app:call-abc')
  })

  it('keeps separate tabs for separate app renders', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-1', 'MCP App', 'slot-1') })
    act(() => { result.current.openApp('call-2', 'MCP App', 'slot-1') })
    expect(result.current.tabs.filter(t => t.kind === 'app')).toHaveLength(2)
  })

  it('is NOT a pinned view — syncPinned must not drop or reorder it', () => {
    // syncPinned rebuilds the strip from PINNED_VIEWS; an app tab is dynamic and
    // must survive that rebuild untouched.
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    act(() => { result.current.syncPinned(['changes', 'files']) })

    expect(result.current.tabs.some(t => t.id === 'app:call-abc')).toBe(true)
    expect(PINNED_VIEWS as readonly string[]).not.toContain('app')
  })

  it('closing the app tab removes it', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    act(() => { result.current.closeTab('app:call-abc') })
    expect(result.current.tabs.some(t => t.kind === 'app')).toBe(false)
  })

  it('closeTab then openApp restores the tab — the hook behaviour the bubble control relies on', () => {
    // NOT the gap-2 guard (that lives in ToolCallLine.test.tsx): the hook already
    // upserted before the fix. This pins the contract the bubble control depends
    // on, so a future create-only refactor of openApp would be caught here.
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    act(() => { result.current.closeTab('app:call-abc') })
    expect(result.current.tabs.find(t => t.id === 'app:call-abc')).toBeUndefined()

    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    const tab = result.current.tabs.find(t => t.id === 'app:call-abc')
    expect(tab).toBeDefined()
    expect(tab!.appToolCallId).toBe('call-abc')
    expect(result.current.activeId).toBe('app:call-abc')
  })

  it('re-opening an already-open app tab focuses it without duplicating (pre-existing upsert)', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    act(() => { result.current.openView('files') })
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })

    expect(result.current.tabs.filter(t => t.id === 'app:call-abc')).toHaveLength(1)
    expect(result.current.activeId).toBe('app:call-abc')
  })
})

describe('app tabs are never persisted', () => {
  // A render payload arrives ONLY on a live `mcp_app_render` event and is never
  // written to storage, so a rehydrated app tab could never show anything —
  // it would just be an empty tab the user has to close after every reload.
  // `serializeBucket` drops them for the same reason it drops diff tabs.
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  const flushPersist = () => { act(() => { vi.advanceTimersByTime(400) }) }
  const readBucket = () => {
    const raw = localStorage.getItem('mc-panel-tabs:slot-1')
    expect(raw).toBeTruthy()
    return JSON.parse(raw!) as { tabs: { id: string; kind: string }[]; activeId: string | null }
  }

  it('keeps other tabs but omits the app tab from storage', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openView('files') })
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    flushPersist()

    const saved = readBucket()
    expect(saved.tabs.some(t => t.kind === 'files')).toBe(true)
    expect(saved.tabs.some(t => t.kind === 'app')).toBe(false)
    expect(saved.tabs.some(t => t.id === 'app:call-abc')).toBe(false)
  })

  it('never persists the app tab as the focused tab', () => {
    // openApp focuses the tab, so naive persistence would save an activeId
    // pointing at a tab that was just dropped — a strip focused on nothing.
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openView('files') })
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    expect(result.current.activeId).toBe('app:call-abc')
    flushPersist()

    const saved = readBucket()
    expect(saved.activeId).not.toBe('app:call-abc')
    // Whatever it points at must actually exist in the persisted strip.
    expect(saved.tabs.some(t => t.id === saved.activeId)).toBe(true)
  })

  it('persists an empty strip when the app tab was the only tab', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    flushPersist()

    const saved = readBucket()
    expect(saved.tabs).toHaveLength(0)
    expect(saved.activeId).toBeNull()
  })

  it('leaves the app tab live IN MEMORY — only storage drops it (persistence never mutated the strip)', () => {
    // Persistence must not mutate the in-memory strip: the iframe has to survive
    // in-app navigation, which is the whole point of the panel host.
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openApp('call-abc', 'MCP App', 'slot-1') })
    flushPersist()

    expect(result.current.tabs.some(t => t.id === 'app:call-abc')).toBe(true)
    expect(result.current.activeId).toBe('app:call-abc')
  })
})

describe('warm-set cap on mounted app frames', () => {
  // Every app tab keeps a LIVE multi-MB iframe mounted (SidePanel display-toggles
  // app bodies rather than unmounting them), so the set must be bounded.
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  /** Open an app tab, then advance the clock so focus stamps are strictly ordered. */
  const open = (r: { current: ReturnType<typeof usePanelTabs> }, id: string) => {
    act(() => { r.current.openApp(id, 'MCP App', 'slot-1') })
    act(() => { vi.advanceTimersByTime(1000) })
  }
  const appIds = (r: { current: ReturnType<typeof usePanelTabs> }) =>
    r.current.tabs.filter(t => t.kind === 'app').map(t => t.appToolCallId)

  it('never exceeds the cap however many renders arrive', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    for (let i = 0; i < MAX_APP_TABS_PER_CHAT + 5; i++) open(result, `call-${i}`)
    expect(appIds(result)).toHaveLength(MAX_APP_TABS_PER_CHAT)
  })

  it('evicts the least-recently-USED frame, not the oldest-opened one', () => {
    // The case FIFO gets wrong: the user goes back to the first diagram and keeps
    // working in it while new renders stream in. FIFO would destroy exactly the
    // frame they are looking at.
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    open(result, 'a'); open(result, 'b'); open(result, 'c')

    act(() => { result.current.setActive('app:a') })      // revisit the oldest
    act(() => { vi.advanceTimersByTime(1000) })
    act(() => { result.current.setActive('app:c') })      // move focus off `a`
    act(() => { vi.advanceTimersByTime(1000) })

    open(result, 'd')
    const ids = appIds(result)
    expect(ids).toContain('a')      // recently used — survives
    expect(ids).toContain('d')
    expect(ids).not.toContain('b')  // least-recently used — evicted
  })

  it('never evicts the tab the user is currently looking at — boundary, vacuous before the cap existed', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    open(result, 'a'); open(result, 'b'); open(result, 'c')
    act(() => { result.current.setActive('app:a') })
    // No advance: `a` keeps an old-ish stamp but IS the active tab.
    open(result, 'd')
    expect(appIds(result)).toContain('a')
  })

  it('holds the cap when several renders open in the SAME tick', () => {
    // The auto-open effect loops over every pending render, so these land back to
    // back with no re-render between them. Reading a stale `tabs` closure would
    // let each call conclude there was room.
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => {
      for (let i = 0; i < MAX_APP_TABS_PER_CHAT + 3; i++) {
        result.current.openApp(`same-tick-${i}`, 'MCP App', 'slot-1')
      }
    })
    expect(appIds(result)).toHaveLength(MAX_APP_TABS_PER_CHAT)
  })

  it('re-opening an existing app tab does not evict anything — boundary, vacuous before the cap existed', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    open(result, 'a'); open(result, 'b'); open(result, 'c')
    open(result, 'a')   // the bubble control re-focusing a live tab
    expect(appIds(result)).toHaveLength(MAX_APP_TABS_PER_CHAT)
    expect(appIds(result)).toEqual(expect.arrayContaining(['a', 'b', 'c']))
  })

  it('does not evict non-app tabs to make room — boundary, would also hold with no cap', () => {
    const { result } = renderHook(() => usePanelTabs('slot-1'))
    act(() => { result.current.openView('files') })
    for (let i = 0; i < MAX_APP_TABS_PER_CHAT + 2; i++) open(result, `call-${i}`)
    expect(result.current.tabs.some(t => t.kind === 'files')).toBe(true)
  })
})

describe('auto-open claim survives a ChatPage remount', () => {
  it('grants the claim exactly once per (slot, tool call)', () => {
    expect(claimAppAutoOpen('slot-1', 'call-a')).toBe(true)
    // A second ask models the effect re-running after ChatPage remounts. A
    // per-mount ref granted it again and re-opened a tab the user had closed.
    expect(claimAppAutoOpen('slot-1', 'call-a')).toBe(false)
    expect(claimAppAutoOpen('slot-1', 'call-a')).toBe(false)
  })

  it('tracks slots independently', () => {
    expect(claimAppAutoOpen('slot-1', 'call-a')).toBe(true)
    expect(claimAppAutoOpen('slot-2', 'call-a')).toBe(true)
    expect(claimAppAutoOpen('slot-1', 'call-a')).toBe(false)
  })

  it('is cleared by the test reset so suites stay isolated', () => {
    expect(claimAppAutoOpen('slot-1', 'call-a')).toBe(true)
    __resetPanelTabs()
    expect(claimAppAutoOpen('slot-1', 'call-a')).toBe(true)
  })
})

describe('app frames from every slot stay hosted under one stable key', () => {
  it('lists app tabs from ALL slots, including the active one', () => {
    // One list is the point: an earlier split (active slot vs background) changed a
    // tab's React key when the active chat changed, remounting the iframe it was
    // supposed to preserve.
    const a = renderHook(() => usePanelTabs('slot-1'))
    act(() => { a.result.current.openApp('call-a', 'MCP App', 'slot-1') })
    const b = renderHook(() => usePanelTabs('slot-2'))
    act(() => { b.result.current.openApp('call-b', 'MCP App', 'slot-2') })

    const all = renderHook(() => useAllAppTabs())
    expect(all.result.current.map(t => t.appToolCallId).sort()).toEqual(['call-a', 'call-b'])
  })

  it('keeps each app tab id stable regardless of which slot is active', () => {
    const a = renderHook(() => usePanelTabs('slot-1'))
    act(() => { a.result.current.openApp('call-a', 'MCP App', 'slot-1') })
    const seenFromA = renderHook(() => useAllAppTabs()).result.current.find(t => t.appToolCallId === 'call-a')!.id
    const b = renderHook(() => usePanelTabs('slot-2'))
    act(() => { b.result.current.openApp('call-b', 'MCP App', 'slot-2') })
    const seenFromB = renderHook(() => useAllAppTabs()).result.current.find(t => t.appToolCallId === 'call-a')!.id
    // Same tab id AND same owning slot => the composite render key SidePanel builds
    // (`slot\u001F id`) is unchanged => React does not remount the iframe. The id
    // alone is only unique WITHIN a slot, which is why the key carries the slot too.
    expect(seenFromB).toBe(seenFromA)
    expect(seenFromB).toBe('app:call-a')
  })

  it('never includes non-app tabs', () => {
    const a = renderHook(() => usePanelTabs('slot-1'))
    act(() => { a.result.current.openView('files') })
    act(() => { a.result.current.openApp('call-a', 'MCP App', 'slot-1') })
    const all = renderHook(() => useAllAppTabs())
    expect(all.result.current.every(t => t.kind === 'app')).toBe(true)
    expect(all.result.current).toHaveLength(1)
  })

  it('reports a live app tab even while a DIFFERENT, app-free slot is active', () => {
    // The mount-guard regression: render an app in chat A, switch to app-free chat B,
    // close the panel -> an active-slot-only guard unmounts the subtree and kills A.
    const a = renderHook(() => usePanelTabs('slot-1'))
    act(() => { a.result.current.openApp('call-a', 'MCP App', 'slot-1') })
    renderHook(() => usePanelTabs('slot-2'))   // slot-2 has no app tabs

    expect(renderHook(() => useAnyLiveAppTab()).result.current).toBe(true)
  })

  it('reports no live app tab once every slot has closed theirs', () => {
    const a = renderHook(() => usePanelTabs('slot-1'))
    act(() => { a.result.current.openApp('call-a', 'MCP App', 'slot-1') })
    act(() => { a.result.current.closeTab('app:call-a') })
    expect(renderHook(() => useAnyLiveAppTab()).result.current).toBe(false)
  })
})

describe('the same tool-call id in two slots stays distinguishable', () => {
  it('yields one entry per slot, each carrying its own slot', () => {
    // A tool-call id is unique within a session, not globally — `chat.mcpApps` keys
    // by session + tool-call id for that reason. Two slots holding the same id must
    // therefore remain separable, or SidePanel would emit duplicate React keys and
    // overlay one session's frame with another's.
    const a = renderHook(() => usePanelTabs('slot-1'))
    act(() => { a.result.current.openApp('same-call', 'MCP App', 'slot-1') })
    const b = renderHook(() => usePanelTabs('slot-2'))
    act(() => { b.result.current.openApp('same-call', 'MCP App', 'slot-2') })

    const all = renderHook(() => useAllAppTabs()).result.current
    const collided = all.filter(t => t.appToolCallId === 'same-call')
    expect(collided).toHaveLength(2)
    expect(collided.map(t => t.slot).sort()).toEqual(['slot-1', 'slot-2'])
    // Same tab id in both — so the slot is the ONLY thing that separates them.
    expect(new Set(collided.map(t => t.id)).size).toBe(1)
  })
})
