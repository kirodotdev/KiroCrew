/**
 * SessionTabStrip + useSessionTabs (#4477).
 *
 * The load-bearing assertion in this file is the FIRST one: below two tabs the
 * strip renders nothing at all. That is the compatibility contract for the chat
 * surface — every user who does not use tabs must get the surface unchanged, no
 * new bar and no reclaimed transcript height — and it is a one-line early return
 * that a refactor can delete without any other test noticing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, renderHook, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import SessionTabStrip from '../components/SessionTabStrip'
import { useSessionTabs } from '../hooks/useSessionTabs'

type Slots = RootState['dashboard']['slots']

const SLOTS = [
  { key: 'chat-1', title: 'Design review' },
  { key: 'chat-2', title: 'Debugging the reaper' },
  { key: 'chat-3', title: 'Release notes' },
] as unknown as Slots

function makeStore(opts: { slots?: Slots; unread?: string[] } = {}) {
  const defaults = createTestStore().getState()
  return createTestStore({
    dashboard: { ...defaults.dashboard, slots: opts.slots ?? SLOTS, unreadSlots: opts.unread ?? [] },
  })
}

function renderStrip(props: {
  tabs: string[]
  activeKey?: string | null
  onSelect?: (k: string) => void
  onClose?: (k: string) => void
  slots?: Slots
  unread?: string[]
}) {
  const onSelect = props.onSelect ?? vi.fn()
  const onClose = props.onClose ?? vi.fn()
  const utils = render(
    <Provider store={makeStore({ slots: props.slots, unread: props.unread })}>
      <SessionTabStrip
        tabs={props.tabs}
        activeKey={props.activeKey ?? props.tabs[0] ?? null}
        onSelect={onSelect}
        onClose={onClose}
      />
    </Provider>,
  )
  return { ...utils, onSelect, onClose }
}

describe('SessionTabStrip visibility', () => {
  it('renders NOTHING below two tabs — the surface is unchanged for anyone not using tabs', () => {
    const { container } = renderStrip({ tabs: ['chat-1'] })
    expect(screen.queryByTestId('session-tab-strip')).toBeNull()
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for an empty set', () => {
    renderStrip({ tabs: [] })
    expect(screen.queryByTestId('session-tab-strip')).toBeNull()
  })

  it('appears at two tabs', () => {
    renderStrip({ tabs: ['chat-1', 'chat-2'] })
    expect(screen.getByTestId('session-tab-strip')).toBeTruthy()
    expect(screen.getAllByRole('tab')).toHaveLength(2)
  })
})

describe('SessionTabStrip rendering', () => {
  it('labels each tab with its session title and marks the active one selected', () => {
    renderStrip({ tabs: ['chat-1', 'chat-2'], activeKey: 'chat-2' })
    expect(screen.getByText('Design review')).toBeTruthy()
    expect(screen.getByTestId('session-tab-chat-2').getAttribute('aria-selected')).toBe('true')
    expect(screen.getByTestId('session-tab-chat-1').getAttribute('aria-selected')).toBe('false')
  })

  it('falls back to the session key when a session has no title yet', () => {
    const slots = [{ key: 'chat-1' }, { key: 'chat-2' }] as unknown as Slots
    renderStrip({ tabs: ['chat-1', 'chat-2'], slots })
    expect(screen.getByText('chat-1')).toBeTruthy()
  })

  it('ellipsizes a long title rather than widening the strip', () => {
    const slots = [{ key: 'chat-1', title: 'z'.repeat(50) }, { key: 'chat-2' }] as unknown as Slots
    renderStrip({ tabs: ['chat-1', 'chat-2'], slots })
    expect(screen.getByText('z'.repeat(24) + '…')).toBeTruthy()
    // The untruncated title stays reachable as the tab's tooltip.
    expect(screen.getByTestId('session-tab-chat-1').getAttribute('title')).toBe('z'.repeat(50))
  })

  it('keeps ONE tab stop for the whole strip (roving tabIndex)', () => {
    // Otherwise Tab walks every open session before reaching the transcript.
    renderStrip({ tabs: ['chat-1', 'chat-2', 'chat-3'], activeKey: 'chat-2' })
    expect(screen.getByTestId('session-tab-chat-2').getAttribute('tabindex')).toBe('0')
    expect(screen.getByTestId('session-tab-chat-1').getAttribute('tabindex')).toBe('-1')
    expect(screen.getByTestId('session-tab-chat-3').getAttribute('tabindex')).toBe('-1')
  })

  it('shows the status a session is actually in', () => {
    const slots = [
      { key: 'chat-1', running: true },
      { key: 'chat-2', pending_approval: true },
      { key: 'chat-3' },
    ] as unknown as Slots
    renderStrip({ tabs: ['chat-1', 'chat-2', 'chat-3'], slots, unread: ['chat-3'] })
    expect(screen.getByTestId('session-tab-status-running')).toBeTruthy()
    expect(screen.getByTestId('session-tab-status-permission')).toBeTruthy()
    expect(screen.getByTestId('session-tab-status-unread')).toBeTruthy()
  })

  it('puts the status in WORDS on the tab, not only in the dot colour', () => {
    // The dot is aria-hidden and colour is its only channel: without the word a
    // screen-reader user gets no status at all, and "needs you" vs "idle" is two
    // greys apart for a colourblind user — which would defeat ranking a blocked
    // session above a running one.
    const slots = [
      { key: 'chat-1', pending_approval: true, title: 'Design review' },
      { key: 'chat-2', running: true, title: 'Debugging the reaper' },
      { key: 'chat-3', title: 'Release notes' },
    ] as unknown as Slots
    renderStrip({ tabs: ['chat-1', 'chat-2', 'chat-3'], slots })
    expect(screen.getByTestId('session-tab-chat-1').getAttribute('aria-label')).toBe('Design review — needs permission')
    expect(screen.getByTestId('session-tab-chat-2').getAttribute('aria-label')).toBe('Debugging the reaper — working')
    // Idle contributes nothing: an ordinary tab is named by its session alone.
    expect(screen.getByTestId('session-tab-chat-3').getAttribute('aria-label')).toBe('Release notes')
    // The tooltip carries the same text, so a sighted mouse user gets it too.
    expect(screen.getByTestId('session-tab-chat-1').getAttribute('title')).toBe('Design review — needs permission')
  })

  it('names a waiting-on-you and an unread session in words too', () => {
    const slots = [
      { key: 'chat-1', needs_input: true, title: 'Design review' },
      { key: 'chat-2', title: 'Release notes' },
    ] as unknown as Slots
    renderStrip({ tabs: ['chat-1', 'chat-2'], slots, unread: ['chat-2'] })
    expect(screen.getByTestId('session-tab-chat-1').getAttribute('aria-label')).toBe('Design review — waiting on you')
    expect(screen.getByTestId('session-tab-chat-2').getAttribute('aria-label')).toBe('Release notes — unread')
  })
})

describe('SessionTabStrip interaction', () => {
  it('switches session on click', () => {
    const { onSelect } = renderStrip({ tabs: ['chat-1', 'chat-2'], activeKey: 'chat-1' })
    fireEvent.click(screen.getByTestId('session-tab-chat-2'))
    expect(onSelect).toHaveBeenCalledWith('chat-2')
  })

  it('closes a tab WITHOUT switching to it', () => {
    // The close button sits inside the tab, so a leaked click would both close
    // the tab and navigate to the session being closed.
    const { onSelect, onClose } = renderStrip({ tabs: ['chat-1', 'chat-2'], activeKey: 'chat-1' })
    fireEvent.click(screen.getByLabelText('Close tab Debugging the reaper'))
    expect(onClose).toHaveBeenCalledWith('chat-2')
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('names the session in the close label and says the session survives', () => {
    renderStrip({ tabs: ['chat-1', 'chat-2'] })
    const btn = screen.getByLabelText('Close tab Design review')
    expect(btn.getAttribute('title')).toBe('Close this tab. The session stays in the sidebar.')
  })

  it('moves with the arrows and jumps with Home/End', () => {
    const { onSelect } = renderStrip({ tabs: ['chat-1', 'chat-2', 'chat-3'], activeKey: 'chat-2' })
    const tab = screen.getByTestId('session-tab-chat-2')
    fireEvent.keyDown(tab, { key: 'ArrowRight' })
    expect(onSelect).toHaveBeenLastCalledWith('chat-3')
    fireEvent.keyDown(tab, { key: 'ArrowLeft' })
    expect(onSelect).toHaveBeenLastCalledWith('chat-1')
    fireEvent.keyDown(tab, { key: 'End' })
    expect(onSelect).toHaveBeenLastCalledWith('chat-3')
    fireEvent.keyDown(tab, { key: 'Home' })
    expect(onSelect).toHaveBeenLastCalledWith('chat-1')
  })

  it('stops at both ends instead of wrapping', () => {
    const { onSelect } = renderStrip({ tabs: ['chat-1', 'chat-2'], activeKey: 'chat-1' })
    fireEvent.keyDown(screen.getByTestId('session-tab-chat-1'), { key: 'ArrowLeft' })
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('closes the focused tab with Delete and Backspace', () => {
    // The close control is a nested button, which a screen-reader user reaches
    // only by stepping into the tab — this is the keyboard path that avoids it.
    const { onClose } = renderStrip({ tabs: ['chat-1', 'chat-2'], activeKey: 'chat-1' })
    fireEvent.keyDown(screen.getByTestId('session-tab-chat-1'), { key: 'Delete' })
    expect(onClose).toHaveBeenLastCalledWith('chat-1')
    fireEvent.keyDown(screen.getByTestId('session-tab-chat-2'), { key: 'Backspace' })
    expect(onClose).toHaveBeenLastCalledWith('chat-2')
  })

  it('hands focus to the neighbour BEFORE a keyboard close unmounts the tab', () => {
    // Otherwise the closed tab's node goes away with focus on it, focus falls to
    // document.body, and the keyboard user who just used the strip's own close
    // key has to Tab through the whole page to get back in.
    renderStrip({ tabs: ['chat-1', 'chat-2', 'chat-3'], activeKey: 'chat-1' })
    const tab = screen.getByTestId('session-tab-chat-2')
    tab.focus()
    fireEvent.keyDown(tab, { key: 'Delete' })
    expect(document.activeElement).toBe(screen.getByTestId('session-tab-chat-3'))
  })

  it('falls back to the left neighbour at the end of the strip', () => {
    renderStrip({ tabs: ['chat-1', 'chat-2', 'chat-3'], activeKey: 'chat-1' })
    const tab = screen.getByTestId('session-tab-chat-3')
    tab.focus()
    fireEvent.keyDown(tab, { key: 'Delete' })
    expect(document.activeElement).toBe(screen.getByTestId('session-tab-chat-2'))
  })

  it('does not chase a tab when the close will unmount the whole strip', () => {
    // Below two tabs the strip itself goes away, so there is no tab to land on;
    // moving focus to the surviving tab first would only make it vanish next.
    renderStrip({ tabs: ['chat-1', 'chat-2'], activeKey: 'chat-1' })
    const tab = screen.getByTestId('session-tab-chat-1')
    tab.focus()
    fireEvent.keyDown(tab, { key: 'Delete' })
    expect(document.activeElement).toBe(tab)
  })

  it('activates on Enter and Space', () => {
    const { onSelect } = renderStrip({ tabs: ['chat-1', 'chat-2'], activeKey: 'chat-1' })
    fireEvent.keyDown(screen.getByTestId('session-tab-chat-2'), { key: 'Enter' })
    expect(onSelect).toHaveBeenLastCalledWith('chat-2')
    fireEvent.keyDown(screen.getByTestId('session-tab-chat-2'), { key: ' ' })
    expect(onSelect).toHaveBeenLastCalledWith('chat-2')
  })

  it('ignores an unhandled key', () => {
    const { onSelect, onClose } = renderStrip({ tabs: ['chat-1', 'chat-2'] })
    fireEvent.keyDown(screen.getByTestId('session-tab-chat-1'), { key: 'x' })
    expect(onSelect).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes a tab on middle-click', () => {
    // The other half of the convention this PR imports for OPENING on a sidebar
    // row: a user who learned that gesture will try it on a tab.
    const { onClose, onSelect } = renderStrip({ tabs: ['chat-1', 'chat-2'] })
    fireEvent(screen.getByTestId('session-tab-chat-2'), new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }))
    expect(onClose).toHaveBeenCalledWith('chat-2')
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('ignores a right-button aux click on a tab', () => {
    const { onClose } = renderStrip({ tabs: ['chat-1', 'chat-2'] })
    fireEvent(screen.getByTestId('session-tab-chat-2'), new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 2 }))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('marks a tab whose session was swapped in place, then clears the mark', () => {
    // A plain sidebar click keeps the tab's POSITION and changes only its label,
    // so without a cue the tab the user deliberately opened is not perceived as
    // consumed — it is discovered missing later.
    vi.useFakeTimers()
    try {
      const { rerender } = render(
        <Provider store={makeStore()}>
          <SessionTabStrip tabs={['chat-1', 'chat-2']} activeKey="chat-1" cue={null} onSelect={vi.fn()} onClose={vi.fn()} />
        </Provider>,
      )
      expect(screen.getByTestId('session-tab-chat-1').getAttribute('data-cued')).toBeNull()
      rerender(
        <Provider store={makeStore()}>
          <SessionTabStrip tabs={['chat-1', 'chat-2']} activeKey="chat-1" cue={{ key: 'chat-1', at: 1 }} onSelect={vi.fn()} onClose={vi.fn()} />
        </Provider>,
      )
      expect(screen.getByTestId('session-tab-chat-1').getAttribute('data-cued')).toBe('true')
      // Only the swapped tab is marked.
      expect(screen.getByTestId('session-tab-chat-2').getAttribute('data-cued')).toBeNull()
      act(() => { vi.advanceTimersByTime(2000) })
      expect(screen.getByTestId('session-tab-chat-1').getAttribute('data-cued')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('marks tabs disabled and says why when the gateway is offline', () => {
    // An offline switch never resolves its fetch and switchSlot.rejected clears
    // the transcript, so activation is withheld — but a click that silently does
    // nothing is worse than one that refuses out loud.
    //
    // The assertions name the REASON rather than checking the title is truthy:
    // the session name always satisfies truthy, so a weak assertion passed while
    // an ordering bug (spreading offlineProps before `title`) clobbered the
    // explanation entirely.
    render(
      <Provider store={makeStore()}>
        <SessionTabStrip tabs={['chat-1', 'chat-2']} activeKey="chat-1" connected={false} onSelect={vi.fn()} onClose={vi.fn()} />
      </Provider>,
    )
    const tab = screen.getByTestId('session-tab-chat-2')
    expect(tab.getAttribute('aria-disabled')).toBe('true')
    expect(tab.getAttribute('title')).toBe('Gateway offline — reconnect to switch sessions')
    // Screen-reader users hear the reason too, not just a disabled state.
    expect(tab.getAttribute('aria-label')).toBe('Debugging the reaper disabled — gateway offline')
    expect(tab.className).toContain('cursor-not-allowed')
  })

  it('keeps the session name as the title while connected', () => {
    renderStrip({ tabs: ['chat-1', 'chat-2'] })
    const tab = screen.getByTestId('session-tab-chat-2')
    expect(tab.getAttribute('aria-disabled')).toBe('false')
    expect(tab.getAttribute('title')).toBe('Debugging the reaper')
    expect(tab.getAttribute('aria-label')).toBe('Debugging the reaper')
  })

  it('exposes the strip as a horizontal tablist', () => {
    renderStrip({ tabs: ['chat-1', 'chat-2'] })
    const list = screen.getByRole('tablist')
    expect(list.getAttribute('aria-orientation')).toBe('horizontal')
    expect(list.getAttribute('aria-label')).toBe('Open sessions')
  })
})

describe('useSessionTabs', () => {
  beforeEach(() => localStorage.clear())

  const LIVE = [{ key: 'chat-1' }, { key: 'chat-2' }, { key: 'chat-3' }]

  it('starts as a one-element set from the active session, so the strip is hidden', () => {
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, true))
    expect(result.current.tabs).toEqual(['chat-1'])
  })

  it('opens a tab beside the active one and persists the set', () => {
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, true))
    act(() => result.current.openInNewTab('chat-2'))
    expect(result.current.tabs).toEqual(['chat-1', 'chat-2'])
    expect(localStorage.getItem('mc-session-tabs-chat')).toBe('["chat-1","chat-2"]')
  })

  it('restores the persisted set on the next mount', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-2', 'chat-3']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-2', LIVE, true))
    expect(result.current.tabs).toEqual(['chat-2', 'chat-3'])
  })

  it('replaces the active tab in place when a session outside the set is activated', () => {
    // The sidebar-click path: the tab keeps its POSITION, which is what makes
    // the strip a stable working set instead of a most-recent list.
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result, rerender } = renderHook(
      ({ active }: { active: string }) => useSessionTabs('chat', active, LIVE, true),
      { initialProps: { active: 'chat-1' } },
    )
    rerender({ active: 'chat-3' })
    expect(result.current.tabs).toEqual(['chat-3', 'chat-2'])
  })

  it('leaves the set alone when the activated session is already a tab', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result, rerender } = renderHook(
      ({ active }: { active: string }) => useSessionTabs('chat', active, LIVE, true),
      { initialProps: { active: 'chat-1' } },
    )
    rerender({ active: 'chat-2' })
    expect(result.current.tabs).toEqual(['chat-1', 'chat-2'])
  })

  it('points at the tab that already holds a re-requested session', () => {
    // Otherwise the gesture is a no-op the user cannot tell from a misfire —
    // background mode does not even switch sessions.
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, true))
    act(() => result.current.openInNewTab('chat-2'))
    expect(result.current.cue?.key).toBe('chat-2')
    expect(result.current.tabs).toEqual(['chat-1', 'chat-2'])
  })

  it('reports the in-place swap so the strip can show it happened', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result, rerender } = renderHook(
      ({ active }: { active: string }) => useSessionTabs('chat', active, LIVE, true),
      { initialProps: { active: 'chat-1' } },
    )
    expect(result.current.cue).toBeNull()
    rerender({ active: 'chat-3' })
    expect(result.current.cue?.key).toBe('chat-3')
    expect(typeof result.current.cue?.at).toBe('number')
  })

  it('does NOT report a swap when nothing was evicted', () => {
    // Selecting a session that is already a tab takes nothing away, and neither
    // does the append path when the previous active session is not in the set.
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result, rerender } = renderHook(
      ({ active }: { active: string }) => useSessionTabs('chat', active, LIVE, true),
      { initialProps: { active: 'chat-1' } },
    )
    rerender({ active: 'chat-2' })
    expect(result.current.cue).toBeNull()
  })

  it('reports where a close lands, and only moves the user for the active tab', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2', 'chat-3']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, true))
    let landed: string | null = null
    act(() => { landed = result.current.closeTab('chat-2') })
    expect(landed).toBe('chat-1')
    expect(result.current.tabs).toEqual(['chat-1', 'chat-3'])
    act(() => { landed = result.current.closeTab('chat-1') })
    expect(landed).toBe('chat-3')
  })

  it('drops tabs whose session is gone from this surface', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', [{ key: 'chat-1' }], true))
    expect(result.current.tabs).toEqual(['chat-1'])
  })

  it('does not RESURRECT a pruned tab when the same pass also reconciles', () => {
    // Both effects run in one flush: prune queues a removal and reconciliation
    // writes the set. Computing that write from a pre-read snapshot instead of a
    // functional update put the deleted session back — and persisted it.
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['gone', 'chat-2']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-3', LIVE, true))
    expect(result.current.tabs).not.toContain('gone')
    expect(localStorage.getItem('mc-session-tabs-chat')).not.toContain('gone')
  })

  it('does NOT prune while the slot list is still empty', () => {
    // On a cold load the slots arrive after the first paint; pruning then would
    // discard the restored set before it could ever be shown.
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', [], true))
    expect(result.current.tabs).toEqual(['chat-1', 'chat-2'])
  })

  it('keys storage by surface mode', () => {
    const { result } = renderHook(() => useSessionTabs('orchestrator', 'chat-1', LIVE, true))
    act(() => result.current.openInNewTab('chat-2'))
    expect(localStorage.getItem('mc-session-tabs-orchestrator')).toBe('["chat-1","chat-2"]')
    expect(localStorage.getItem('mc-session-tabs-chat')).toBeNull()
  })

  it('seeds the set from the first activation when there was no active session', () => {
    const { result, rerender } = renderHook(
      ({ active }: { active: string | null }) => useSessionTabs('chat', active, LIVE, true),
      { initialProps: { active: null as string | null } },
    )
    expect(result.current.tabs).toEqual([])
    rerender({ active: 'chat-2' })
    expect(result.current.tabs).toEqual(['chat-2'])
  })
})

describe('useSessionTabs on a surface that does not own the set', () => {
  // Every embedded host — popped-out window, artifact companion panel, Papyrus
  // co-author panel, app-SDK chat panel — mounts ChatPage on the dashboard's own
  // origin, so it shares this localStorage key. Left live, each would reconcile
  // the key against ITS active session and overwrite the dashboard's working set
  // with a session the dashboard never opened.
  beforeEach(() => localStorage.clear())

  const LIVE = [{ key: 'chat-1' }, { key: 'chat-2' }, { key: 'chat-3' }]

  it('reports no tabs even when a set is persisted', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, false))
    expect(result.current.tabs).toEqual([])
  })

  it('does NOT overwrite the persisted set', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    renderHook(() => useSessionTabs('chat', 'chat-3', LIVE, false))
    expect(localStorage.getItem('mc-session-tabs-chat')).toBe('["chat-1","chat-2"]')
  })

  it('does not reconcile its own active session into the set', () => {
    // The corruption GPT 5.6 named: an embedded host's active slot replacing a
    // dashboard tab, or adding a key the dashboard's sidebar cannot show.
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { rerender } = renderHook(
      ({ active }: { active: string }) => useSessionTabs('chat', active, LIVE, false),
      { initialProps: { active: 'chat-1' } },
    )
    rerender({ active: 'app-owned-slot' })
    expect(localStorage.getItem('mc-session-tabs-chat')).toBe('["chat-1","chat-2"]')
  })

  it('does not prune the persisted set against its own slot list', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    renderHook(() => useSessionTabs('chat', 'chat-1', [{ key: 'chat-1' }], false))
    expect(localStorage.getItem('mc-session-tabs-chat')).toBe('["chat-1","chat-2"]')
  })

  it('makes both mutators inert', () => {
    localStorage.setItem('mc-session-tabs-chat', JSON.stringify(['chat-1', 'chat-2']))
    const { result } = renderHook(() => useSessionTabs('chat', 'chat-1', LIVE, false))
    act(() => result.current.openInNewTab('chat-3'))
    let landed: string | null = 'sentinel'
    act(() => { landed = result.current.closeTab('chat-1') })
    expect(landed).toBeNull()
    expect(result.current.tabs).toEqual([])
    expect(localStorage.getItem('mc-session-tabs-chat')).toBe('["chat-1","chat-2"]')
  })
})
