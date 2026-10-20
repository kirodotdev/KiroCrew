/**
 * The page's view components (pages/chat/page/*.tsx), rendered directly. Each
 * is a pure function of the props ChatPage hands it, so the branches the page
 * suites rarely reach (a crew window over an embedded pane, the recents flyout,
 * the header's reveal and rename actions, a popped-out or split-anchored
 * session, the pinned prompt) are driven here with the children stubbed and
 * their props captured.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { forwardRef, type ReactNode } from 'react'

const seen = vi.hoisted(() => ({
  crewWindow: null as null | { onClose?: () => void; target: unknown },
  crewGuard: null as null | { onClose?: () => void },
  flyout: null as null | { onExpand: () => void; onDismiss: () => void },
  headerMenu: null as null | { onReveal?: () => void; onRename?: () => void },
  pinnedPrompt: null as null | { onToggleExpanded: () => void; onJump: () => void },
  toolGroups: [] as Array<{ onApprove: (a: string) => Promise<void>; pendingPermCount: number; count: number }>,
}))

vi.mock('./chat/crew-window/CrewChatWindow', () => ({
  default: (p: { onClose?: () => void; target: unknown }) => { seen.crewWindow = p; return <div data-testid="crew-window" /> },
}))
vi.mock('./chat/crew-window/CrewWindowGuard', () => ({ default: (p: { onClose?: () => void }) => { seen.crewGuard = p; return null } }))
vi.mock('../components/SnipOverlay', () => ({ default: () => null }))
vi.mock('./chat/page/ChatPaneNotices', () => ({ default: () => null }))
vi.mock('../components/SessionTabStrip', () => ({ default: () => null }))
vi.mock('./chat/SessionFlyout', () => ({
  default: forwardRef((p: { onExpand: () => void; onDismiss: () => void }, _ref) => { seen.flyout = p; return <div data-testid="flyout" /> }),
  TOGGLE_RECT: { x: 8, y: 9, size: 28 },
}))
vi.mock('./ChatSidebar', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../components/SlotTagPopover', () => ({ default: () => null }))
vi.mock('./chat/page/MobileTopBar', () => ({ default: () => null }))
vi.mock('../guide/GuideRevealScope', () => ({ GuideRevealScope: ({ children }: { children: ReactNode }) => <>{children}</> }))
vi.mock('./chat/ChatHeaderMenu', () => ({
  ChatHeaderMenu: (p: { onReveal?: () => void; onRename?: () => void }) => { seen.headerMenu = p; return null },
}))
vi.mock('./chat/SessionTitleControl', () => ({ default: () => null }))
vi.mock('../components/InboundLinkChip', () => ({ default: () => null }))
vi.mock('../components/ChatHeaderChips', () => ({ default: () => null }))
vi.mock('./chat', async (orig) => ({
  ...(await orig<typeof import('./chat')>()),
  PinnedPrompt: (p: { onToggleExpanded: () => void; onJump: () => void }) => { seen.pinnedPrompt = p; return null },
  ChatFooter: () => null,
}))
vi.mock('./chat/TranscriptScrollShell', () => ({ default: ({ children }: { children: ReactNode }) => <div data-testid="shell">{children}</div> }))
vi.mock('./chat/CollapsibleToolGroup', () => ({
  default: (p: { onApprove: (a: string) => Promise<void>; pendingPermCount: number; count: number; children: ReactNode }) => {
    seen.toolGroups.push(p); return <div data-testid="tool-group">{p.children}</div>
  },
}))
vi.mock('./chat/TurnBlock', () => ({ default: () => null }))
vi.mock('./chat/TurnNavigationMinimap', () => ({ default: () => null }))
vi.mock('./chat/EarlierMessagesBar', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/SessionPulseSurveyCard', () => ({ default: () => null }))
vi.mock('../components/ChatTranscriptSkeleton', () => ({ ChatTranscriptSkeleton: () => null }))
vi.mock('../hooks/virtualizer/MeasureFarm', () => ({ MeasureFarm: () => null }))
vi.mock('../api/client', () => ({ api: { resolveApproval: vi.fn(async () => undefined) } }))

import ChatPaneChrome from './chat/page/ChatPaneChrome'
import SessionsSidebarChrome from './chat/page/SessionsSidebarChrome'
import ChatTitleOverlay from './chat/page/ChatTitleOverlay'
import ChatTranscriptView from './chat/page/ChatTranscriptView'
import { api } from '../api/client'
import { requestSlotReveal } from '../store/chatSlice'

/** A view's props: the fields a case sets, the rest left undefined (each view reads only what its branch needs). */
const props = <T,>(over: Partial<T>) => over as T
/** How long a test waits for ChatPaneChrome's lazy CrewChatWindow chunk to resolve and render. */
const CREW_WINDOW_CHUNK_TIMEOUT_MS = 5000
type PaneProps = Parameters<typeof ChatPaneChrome>[0]
type SidebarProps = Parameters<typeof SessionsSidebarChrome>[0]
type TitleProps = Parameters<typeof ChatTitleOverlay>[0]
type TranscriptProps = Parameters<typeof ChatTranscriptView>[0]

beforeEach(() => {
  seen.crewWindow = null
  seen.crewGuard = null
  seen.flyout = null
  seen.headerMenu = null
  seen.pinnedPrompt = null
  seen.toolGroups = []
  vi.mocked(api.resolveApproval).mockClear()
})

describe('ChatPaneChrome', () => {
  const base = (over: Partial<PaneProps>): PaneProps => props<PaneProps>({
    messages: [], sessionTabs: { tabs: [], cue: null } as never, isMobile: false, ...over,
  })

  it('covers the pane with the crew window, closing back to the sessions list when embedded', async () => {
    // A fresh module, so its lazy CrewChatWindow chunk starts unloaded whichever test ran first, and
    // the Suspense fallback (the guard) is what renders before the chunk resolves.
    vi.resetModules()
    const { default: FreshPaneChrome } = await import('./chat/page/ChatPaneChrome')
    const navigate = vi.fn()
    render(<FreshPaneChrome {...base({ crewWindow: { instanceId: 'i1', key: 'k1' }, embedded: true, navigate })} />)
    // While the window's chunk loads, its guard already offers the same way out.
    seen.crewGuard!.onClose!()
    expect(navigate).toHaveBeenCalledWith('/embed/sessions')
    navigate.mockClear()
    expect(await screen.findByTestId('crew-window', {}, { timeout: CREW_WINDOW_CHUNK_TIMEOUT_MS })).toBeInTheDocument()
    expect(seen.crewWindow!.target).toEqual({ instanceId: 'i1', key: 'k1' })
    seen.crewWindow!.onClose!()
    expect(navigate).toHaveBeenCalledWith('/embed/sessions')
  })

  it('gives the crew window no close action on the full page', async () => {
    render(<ChatPaneChrome {...base({ crewWindow: { instanceId: 'i1', key: 'k1' }, embedded: false, navigate: vi.fn() })} />)
    await screen.findByTestId('crew-window', {}, { timeout: CREW_WINDOW_CHUNK_TIMEOUT_MS })
    expect(seen.crewWindow!.onClose).toBeUndefined()
  })
})

describe('SessionsSidebarChrome', () => {
  it('expands the sidebar from the recents flyout, and returns focus to the toggle when it is dismissed', () => {
    const expandSidebar = vi.fn()
    const close = vi.fn()
    const focus = vi.fn()
    render(<SessionsSidebarChrome {...props<SidebarProps>({
      embedMode: 'chat', isMobile: false, filteredSlots: [], effectiveSidebarWidth: 280, containerH: 600,
      flyoutEligible: true, expandSidebar, flyoutTriggerRef: { current: { focus } as unknown as HTMLButtonElement },
      flyoutSurfaceRef: { current: null },
      flyout: { open: true, openedBy: 'pointer', close, surfaceProps: {} } as never,
    })} />)
    expect(screen.getByTestId('flyout')).toBeInTheDocument()
    act(() => { seen.flyout!.onExpand() })
    expect(expandSidebar).toHaveBeenCalledWith(true)
    act(() => { seen.flyout!.onDismiss() })
    expect(close).toHaveBeenCalled()
    expect(focus).toHaveBeenCalled()
  })
})

describe('ChatTitleOverlay', () => {
  const base = (over: Partial<TitleProps>): TitleProps => props<TitleProps>({
    isMobile: false, titleInTopbar: false, filteredSlots: [], activeSlot: 'slot-a', pinned: null,
    sidebarAutoHidden: { current: 'focus' as never }, dispatch: vi.fn() as never,
    popOutTip: { handlers: {}, bubble: null } as never, activityTip: { handlers: {}, bubble: null } as never, ...over,
  })

  it('reveals the session in the sidebar: pins the desktop sidebar, or opens the phone drawer', () => {
    const setSidebarPinned = vi.fn()
    const desktop = base({ sidebarPinned: false, setSidebarPinned, openSidebar: vi.fn() })
    render(<ChatTitleOverlay {...desktop} />)
    act(() => { seen.headerMenu!.onReveal!() })
    expect(desktop.sidebarAutoHidden.current).toBeNull()
    expect(setSidebarPinned).toHaveBeenCalledWith(true)
    expect(desktop.dispatch).toHaveBeenCalledWith(requestSlotReveal('slot-a'))

    const openSidebar = vi.fn()
    const phone = base({ isMobile: true, openSidebar, setSidebarPinned: vi.fn() })
    render(<ChatTitleOverlay {...phone} />)
    act(() => { seen.headerMenu!.onReveal!() })
    expect(openSidebar).toHaveBeenCalled()
    expect(phone.setSidebarPinned).not.toHaveBeenCalled()
  })

  it('marks a Slack code channel session in the header, and nothing else', () => {
    const codeChannel = {
      channel_name: 'fix-login', status: 'active' as const,
      web_url: 'https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1',
    }
    const { unmount } = render(<ChatTitleOverlay {...base({ currentSlot: { key: 'slot-a', messages: 0, running: false } as never })} />)
    expect(screen.queryByTestId('code-channel-badge-header')).toBeNull()
    unmount()
    render(<ChatTitleOverlay {...base({ currentSlot: { key: 'slot-a', messages: 0, running: false, code_channel: codeChannel } as never })} />)
    expect(screen.getByTestId('code-channel-badge-header')).toHaveAttribute('href', codeChannel.web_url)
  })

  it('offers no reveal on an embedded chat, and renames the active session', () => {
    const setEditingTitleSlot = vi.fn()
    render(<ChatTitleOverlay {...base({ embedMode: 'chat', setEditingTitleSlot })} />)
    expect(seen.headerMenu!.onReveal).toBeUndefined()
    act(() => { seen.headerMenu!.onRename!() })
    expect(setEditingTitleSlot).toHaveBeenCalledWith('slot-a')
  })

  it('marks a popped-out session and a session another pane anchors in split view, and acts on both', () => {
    const focusActivePopout = vi.fn()
    const enterSplit = vi.fn()
    render(<ChatTitleOverlay {...base({
      activePoppedOut: true, splitFeatureEnabled: true, splitAnchorForActive: 'slot-b', activeIsSplitAnchor: false,
      focusActivePopout, enterSplit,
    })} />)
    fireEvent.click(screen.getByText(/popped out/i))
    expect(focusActivePopout).toHaveBeenCalledWith('slot-a')
    fireEvent.click(screen.getByText(/in split/i))
    expect(enterSplit).toHaveBeenCalledWith('slot-b')
  })

  it('pops the session out to its own window', () => {
    const openActivePopout = vi.fn()
    render(<ChatTitleOverlay {...base({ activePoppedOut: false, openActivePopout, currentSlot: { key: 'slot-a', title: 'Alpha' } as never })} />)
    fireEvent.click(screen.getByRole('button', { name: /pop out/i }))
    expect(openActivePopout).toHaveBeenCalledWith('slot-a', 'Alpha')
  })

  it('toggles and jumps to the pinned prompt', () => {
    const setPinExpanded = vi.fn()
    const scrollToPinnedPrompt = vi.fn()
    render(<ChatTitleOverlay {...base({
      pinned: { idx: 7, bannerH: 32 } as never, pinExpanded: false, setPinExpanded, scrollToPinnedPrompt,
      pinCardRef: { current: null }, pinFoldRef: { current: null }, onPinCollapsedHeight: vi.fn(),
    })} />)
    act(() => { seen.pinnedPrompt!.onToggleExpanded() })
    const toggle = setPinExpanded.mock.calls[0][0] as (p: boolean) => boolean
    expect(toggle(false)).toBe(true)
    act(() => { seen.pinnedPrompt!.onJump() })
    expect(scrollToPinnedPrompt).toHaveBeenCalledWith(7)
  })
})

describe('ChatTranscriptView', () => {
  const msg = (role: string, ts: string, meta?: Record<string, unknown>) => ({ role, content: role, ts, cls: '', ...(meta ? { meta } : {}) }) as never
  const row = (data: unknown, index: number) => ({ mounted: true, data, index, key: `k${index}` })
  const base = (items: unknown[], over: Partial<TranscriptProps> = {}): TranscriptProps => props<TranscriptProps>({
    isWelcomeState: false, visibleDisplayItems: items.map(row) as never, renderedDisplayItems: items as never,
    virt: { measureRef: () => undefined, restoreGate: false } as never, chatConfig: {} as never, pinned: null, chatNav: { sections: [] } as never,
    renderMessage: (_i: number, m: { ts: string }) => <span>{m.ts}</span>, stableMsgKey: (m: { ts: string }) => m.ts,
    approve: vi.fn(async () => undefined), dismissApproval: vi.fn(), toggleAct: vi.fn(), activityOpen: false, slotRunning: false,
    ...over,
  } as never)

  it('draws nothing for a permissions-only tool group', () => {
    render(<ChatTranscriptView {...base([{ kind: 'group', startIdx: 0, msgs: [msg('permission', 'p1')] }])} />)
    expect(seen.toolGroups).toHaveLength(0)
  })

  it('routes a group approval to the newest unresolved permission, else to the page approve', async () => {
    const p = base([
      { kind: 'group', startIdx: 0, msgs: [msg('tool', 't1'), msg('permission', 'p1', { approval_id: 'ap-1' })] },
      { kind: 'group', startIdx: 2, msgs: [msg('tool', 't2')] },
    ])
    render(<ChatTranscriptView {...p} />)
    const [withId, noId] = seen.toolGroups
    expect(withId.pendingPermCount).toBe(1)
    expect(withId.count).toBe(1)
    await act(async () => { await withId.onApprove('approve') })
    expect(vi.mocked(api.resolveApproval).mock.calls[0][0]).toBe('ap-1')
    expect(p.dismissApproval).toHaveBeenCalledWith('ap-1')
    expect(noId.onApprove).toBe(p.approve)
  })
})
