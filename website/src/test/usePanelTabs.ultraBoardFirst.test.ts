/**
 * An Ultra Mode chat keeps the Board (`workers`) in front: no automatic open
 * moves the focus. Only a tab the person clicks does, and that tab then stays
 * in front until the next click. A normal chat keeps today's behaviour, where
 * a newly found PR or issue link brings its tab to the front.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePanelTabs, offerPanelView, notePanelShown, __resetPanelTabs, PINNED_VIEWS } from '../hooks/usePanelTabs'

const CREW_FIRST = ['workers'] as const

beforeEach(() => { __resetPanelTabs(); localStorage.clear() })

/** A strip the way the chat page and side panel build it for a chat. */
function strip(slot: string, boardFirst: boolean) {
  const hook = renderHook(({ on }) => usePanelTabs(slot, [], { boardFirst: on }), { initialProps: { on: boardFirst } })
  if (boardFirst) {
    act(() => offerPanelView(slot, 'workers'))
    act(() => hook.result.current.syncPinned(PINNED_VIEWS, CREW_FIRST))
  } else {
    act(() => hook.result.current.syncPinned(PINNED_VIEWS))
  }
  return hook
}

describe('usePanelTabs — Ultra Mode keeps the Board in front', () => {
  it('a fresh Ultra Mode chat opens on the Board and a PR link found stays behind it', () => {
    const { result } = strip('crew', true)
    expect(result.current.activeId).toBe('workers')
    act(() => result.current.openView('changes', { automatic: true }))
    expect(result.current.activeId).toBe('workers')
  })

  it('PR and issue links in loaded history add their tabs without taking the focus', () => {
    const { result } = strip('crew', true)
    act(() => result.current.openView('changes', { automatic: true }))
    act(() => result.current.openView('issues', { automatic: true }))
    expect(result.current.tabs.map(t => t.id)).toContain('issues')
    expect(result.current.activeId).toBe('workers')
  })

  it('Pin Summary landing, an app render and the Git tab do not take the focus either', () => {
    const { result } = strip('crew', true)
    act(() => result.current.openView('summary', { automatic: true }))
    act(() => result.current.openView('git', { automatic: true }))
    act(() => result.current.openApp('call-1', 'Diagram', 'crew', { automatic: true }))
    expect(result.current.tabs.map(t => t.id)).toEqual(expect.arrayContaining(['summary', 'git', 'app:call-1']))
    expect(result.current.activeId).toBe('workers')
  })

  it('a tab the person clicks stays in front when a new PR link arrives', () => {
    const { result } = strip('crew', true)
    act(() => result.current.openView('changes', { automatic: true }))
    act(() => result.current.setActive('files'))
    act(() => result.current.openView('changes', { automatic: true }))
    act(() => result.current.openView('issues', { automatic: true }))
    expect(result.current.activeId).toBe('files')
    act(() => result.current.setActive('changes'))
    act(() => result.current.openView('issues', { automatic: true }))
    expect(result.current.activeId).toBe('changes')
  })

  it('clicking back to the Board keeps it there against later discovery', () => {
    const { result } = strip('crew', true)
    act(() => result.current.setActive('files'))
    act(() => result.current.setActive('workers'))
    act(() => result.current.openView('changes', { automatic: true }))
    expect(result.current.activeId).toBe('workers')
  })

  it('the Board survives leaving the chat and coming back (session switch, remount)', () => {
    const first = strip('crew', true)
    act(() => first.result.current.openView('changes', { automatic: true }))
    notePanelShown('crew')
    notePanelShown('other')
    first.unmount()
    notePanelShown('crew')
    const again = renderHook(() => usePanelTabs('crew', [], { boardFirst: true }))
    act(() => again.result.current.openView('changes', { automatic: true }))
    expect(again.result.current.activeId).toBe('workers')
  })

  it('a person still opens any tab directly', () => {
    const { result } = strip('crew', true)
    act(() => result.current.openView('changes'))
    expect(result.current.activeId).toBe('changes')
    act(() => result.current.openApp('call-2', 'Diagram', 'crew'))
    expect(result.current.activeId).toBe('app:call-2')
  })
})

describe('usePanelTabs — a normal chat is unchanged', () => {
  it('a found PR or issue link and an app render still come to the front', () => {
    const { result } = strip('plain', false)
    expect(result.current.activeId).toBe('changes')
    act(() => result.current.setActive('files'))
    act(() => result.current.openView('changes', { automatic: true }))
    expect(result.current.activeId).toBe('changes')
    act(() => result.current.openView('issues', { automatic: true }))
    expect(result.current.activeId).toBe('issues')
    act(() => result.current.openApp('call-3', 'Diagram', 'plain', { automatic: true }))
    expect(result.current.activeId).toBe('app:call-3')
  })

  it('switching from an Ultra Mode chat to a normal one keeps each chat on its own rule', () => {
    const crew = strip('crew', true)
    const plain = strip('plain', false)
    act(() => crew.result.current.openView('changes', { automatic: true }))
    act(() => plain.result.current.setActive('files'))
    act(() => plain.result.current.openView('changes', { automatic: true }))
    expect(crew.result.current.activeId).toBe('workers')
    expect(plain.result.current.activeId).toBe('changes')
  })
})
