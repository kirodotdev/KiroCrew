/**
 * Tests for usePanelTabs — the tabbed side panel state model
 * (introduced with the tabbed side panel revamp). Pins the tab-strip
 * contracts: singleton view tabs, document dedupe/focus, replace-in-place
 * opens, patch-without-focus, neighbor refocus on close, and reordering.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

// The panel-tab store is now module-level + localStorage-persisted (so the
// strip survives ChatPage route unmounts and reloads). Reset it before each
// test so state doesn't leak across the renderHook calls in this suite.
beforeEach(() => { __resetPanelTabs() })

describe('usePanelTabs', () => {
  it('starts empty with no active tab', () => {
    const { result } = renderHook(() => usePanelTabs())
    expect(result.current.tabs).toEqual([])
    expect(result.current.activeId).toBeNull()
    expect(result.current.activeTab).toBeNull()
    expect(result.current.hasTabs).toBe(false)
  })

  it('openView creates a singleton tab and focuses it; reopening focuses instead of duplicating', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('files'))
    act(() => result.current.openView('logs'))
    expect(result.current.tabs.map(t => t.id)).toEqual(['files', 'logs'])
    expect(result.current.activeId).toBe('logs')

    // Reopen files: no duplicate, focus moves back.
    act(() => result.current.openView('files'))
    expect(result.current.tabs.map(t => t.id)).toEqual(['files', 'logs'])
    expect(result.current.activeId).toBe('files')
    expect(result.current.activeTab?.title).toBe('Files')
  })

  it('opens Changes as a singleton source view', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('changes'))
    act(() => result.current.openView('changes'))
    expect(result.current.tabs).toHaveLength(1)
    expect(result.current.activeTab).toMatchObject({
      id: 'changes', kind: 'changes', title: 'Changes',
    })
  })

  it('opens Web Preview as a singleton, closable view tab', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('browser'))
    act(() => result.current.openView('browser'))
    expect(result.current.tabs).toHaveLength(1)
    expect(result.current.activeTab).toMatchObject({
      id: 'browser', kind: 'browser', title: 'Web Preview',
    })
    // Not a pinned view — closes like any dynamic tab.
    act(() => result.current.closeTab('browser'))
    expect(result.current.tabs).toHaveLength(0)
    expect(result.current.activeId).toBeNull()
  })

  it('openFile dedupes on path, titles by basename, and carries the origin slot', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openFile('/src/pages/ChatPage.tsx', 'body-1', 'slot-a'))
    expect(result.current.tabs).toHaveLength(1)
    expect(result.current.activeTab).toMatchObject({
      id: 'file:/src/pages/ChatPage.tsx', kind: 'file', title: 'ChatPage.tsx', slot: 'slot-a', content: 'body-1',
    })

    // Same path again: merges (fresh content), still one tab.
    act(() => result.current.openFile('/src/pages/ChatPage.tsx', 'body-2', 'slot-a'))
    expect(result.current.tabs).toHaveLength(1)
    expect(result.current.activeTab?.content).toBe('body-2')
  })

  it('openFile with replaceId swaps the new tab into the replaced tab\'s strip position', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('files'))
    act(() => result.current.openView('logs'))
    // A file opened FROM the Files view replaces the Files tab in-place.
    act(() => result.current.openFile('/a.ts', 'x', null, { replaceId: 'files' }))
    expect(result.current.tabs.map(t => t.id)).toEqual(['file:/a.ts', 'logs'])
    expect(result.current.activeId).toBe('file:/a.ts')
  })

  it('openFile with replaceId closes the replaced tab when the file is already open elsewhere', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openFile('/a.ts', 'x'))
    act(() => result.current.openView('files'))
    expect(result.current.tabs.map(t => t.id)).toEqual(['file:/a.ts', 'files'])
    // Opening /a.ts from the Files tab: existing tab wins, Files tab closes.
    act(() => result.current.openFile('/a.ts', 'y', null, { replaceId: 'files' }))
    expect(result.current.tabs.map(t => t.id)).toEqual(['file:/a.ts'])
    expect(result.current.activeId).toBe('file:/a.ts')
  })

  it('openDiff titles as "name - Diff" and dedupes per path', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openDiff('/src/App.tsx', 'mod', 'orig'))
    expect(result.current.activeTab).toMatchObject({
      id: 'diff:/src/App.tsx', kind: 'diff', title: 'App.tsx - Diff', modified: 'mod', original: 'orig',
    })
    act(() => result.current.openDiff('/src/App.tsx', 'mod-2'))
    expect(result.current.tabs).toHaveLength(1)
    expect(result.current.activeTab?.modified).toBe('mod-2')
  })

  it('patchTab updates fields WITHOUT stealing focus', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openFile('/a.ts', 'x'))
    act(() => result.current.openView('logs'))
    expect(result.current.activeId).toBe('logs')
    act(() => result.current.patchTab('file:/a.ts', { content: 'edited' }))
    expect(result.current.activeId).toBe('logs') // focus unchanged
    expect(result.current.tabs.find(t => t.id === 'file:/a.ts')?.content).toBe('edited')
    // Patching a missing id is a no-op.
    act(() => result.current.patchTab('nope', { content: 'z' }))
    expect(result.current.tabs).toHaveLength(2)
  })

  it('closeTab refocuses the left neighbor (then right, then null)', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('files'))
    act(() => result.current.openView('logs'))
    act(() => result.current.openView('side'))
    expect(result.current.activeId).toBe('side')

    // Close active (rightmost) -> left neighbor takes focus.
    act(() => result.current.closeTab('side'))
    expect(result.current.activeId).toBe('logs')

    // Close a NON-active tab -> focus untouched.
    act(() => result.current.closeTab('files'))
    expect(result.current.activeId).toBe('logs')

    // Close the last tab -> nothing focused.
    act(() => result.current.closeTab('logs'))
    expect(result.current.tabs).toEqual([])
    expect(result.current.activeId).toBeNull()
  })

  it('closeTab on the active leftmost tab focuses the (new) first tab', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('files'))
    act(() => result.current.openView('logs'))
    act(() => result.current.setActive('files'))
    act(() => result.current.closeTab('files'))
    expect(result.current.activeId).toBe('logs')
  })

  it('closeAll clears tabs and focus; setOrder replaces the strip order wholesale', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('files'))
    act(() => result.current.openView('logs'))
    const reversed = [...result.current.tabs].reverse()
    act(() => result.current.setOrder(reversed))
    expect(result.current.tabs.map(t => t.id)).toEqual(['logs', 'files'])
    act(() => result.current.closeAll())
    expect(result.current.tabs).toEqual([])
    expect(result.current.activeId).toBeNull()
    expect(result.current.hasTabs).toBe(false)
  })
})

describe('usePanelTabs — per-slot isolation', () => {
  it('each chat slot gets its own strip; switching slots swaps and restores it', () => {
    const { result, rerender } = renderHook(({ slot }: { slot: string | null }) => usePanelTabs(slot), {
      initialProps: { slot: 'chat-a' as string | null },
    })
    act(() => result.current.openFile('/a.ts', 'body-a'))
    act(() => result.current.openView('logs'))
    expect(result.current.tabs.map(t => t.id)).toEqual(['file:/a.ts', 'logs'])

    // Switch to chat B: fresh empty strip.
    rerender({ slot: 'chat-b' })
    expect(result.current.tabs).toEqual([])
    expect(result.current.activeId).toBeNull()
    expect(result.current.hasTabs).toBe(false)

    // B builds its own strip; A's is untouched.
    act(() => result.current.openDiff('/b.ts', 'mod'))
    expect(result.current.tabs.map(t => t.id)).toEqual(['diff:/b.ts'])

    // Back to A: strip and focus restored exactly.
    rerender({ slot: 'chat-a' })
    expect(result.current.tabs.map(t => t.id)).toEqual(['file:/a.ts', 'logs'])
    expect(result.current.activeId).toBe('logs')

    // And B's again.
    rerender({ slot: 'chat-b' })
    expect(result.current.tabs.map(t => t.id)).toEqual(['diff:/b.ts'])
    expect(result.current.activeId).toBe('diff:/b.ts')
  })

  it('restores a file tab\'s selected diff view after leaving and returning to a chat', () => {
    const { result, rerender } = renderHook(({ slot }: { slot: string | null }) => usePanelTabs(slot), {
      initialProps: { slot: 'chat-a' as string | null },
    })
    act(() => result.current.openFile('/README.md', '# current'))
    act(() => result.current.patchTab('file:/README.md', { diffMode: false }))

    rerender({ slot: 'chat-b' })
    expect(result.current.tabs).toEqual([])
    rerender({ slot: 'chat-a' })

    expect(result.current.activeTab).toMatchObject({
      id: 'file:/README.md', diffMode: false,
    })
  })

  it('operations only touch the active slot\'s bucket (closeAll in B leaves A intact)', () => {
    const { result, rerender } = renderHook(({ slot }: { slot: string | null }) => usePanelTabs(slot), {
      initialProps: { slot: 'chat-a' as string | null },
    })
    act(() => result.current.openView('files'))
    rerender({ slot: 'chat-b' })
    act(() => result.current.openView('subagents'))
    act(() => result.current.closeAll())
    expect(result.current.tabs).toEqual([])
    rerender({ slot: 'chat-a' })
    expect(result.current.tabs.map(t => t.id)).toEqual(['files'])
  })

  it('a null slot uses a stable fallback bucket', () => {
    const { result, rerender } = renderHook(({ slot }: { slot: string | null }) => usePanelTabs(slot), {
      initialProps: { slot: null as string | null },
    })
    let sid = ''
    act(() => { sid = result.current.openTerminal() })
    rerender({ slot: 'chat-a' })
    expect(result.current.tabs).toEqual([])
    rerender({ slot: null })
    expect(result.current.tabs.map(t => t.id)).toEqual([`terminal:${sid}`])
    expect(result.current.tabs[0].kind).toBe('terminal')
  })

  it('syncPinned adds content-gated views at the front, in PINNED_VIEWS order', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('logs'))
    act(() => result.current.syncPinned(['files', 'changes']))
    // Pinned views are ordered per PINNED_VIEWS (changes, files, artifacts),
    // always ahead of dynamic tabs.
    expect(result.current.tabs.map(t => t.id)).toEqual(['changes', 'files', 'logs'])
  })

  it('syncPinned removes a pinned view when its content goes away, refocusing if needed', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.syncPinned(['files', 'artifacts']))
    act(() => result.current.setActive('artifacts'))
    expect(result.current.activeId).toBe('artifacts')
    // Artifacts empties out: its tab is dropped and focus falls back.
    act(() => result.current.syncPinned(['files']))
    expect(result.current.tabs.map(t => t.id)).toEqual(['files'])
    expect(result.current.activeId).toBe('files')
  })

  it('syncPinned preserves dynamic tabs and their order after the pinned block', () => {
    const { result } = renderHook(() => usePanelTabs())
    act(() => result.current.openView('logs'))
    act(() => result.current.openView('side'))
    act(() => result.current.syncPinned(['changes']))
    expect(result.current.tabs.map(t => t.id)).toEqual(['changes', 'logs', 'side'])
    // Emptying content removes only the pinned view; dynamic tabs untouched.
    act(() => result.current.syncPinned([]))
    expect(result.current.tabs.map(t => t.id)).toEqual(['logs', 'side'])
  })
})
