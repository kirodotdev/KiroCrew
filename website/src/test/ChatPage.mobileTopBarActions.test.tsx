/**
 * The phone top bar's ACTIONS (pages/chat/page/MobileTopBar.tsx).
 *
 * ChatPage.mobileSingleTopbar pins where the bar's controls land (the shell's
 * two portal targets) and what the menus list; this file presses them. The
 * session menu and the shared title editor are stubbed to buttons that call the
 * props the bar hands them, so each assertion is about what the BAR does:
 *
 *  - Reveal clears the sidebar's auto-hide memory, opens the drawer and asks
 *    the store to reveal the row; Rename opens the shared title editor;
 *  - Auto-title runs one generation at a time, writes the server's title
 *    through the store, and narrates a failure (titled) through the page;
 *  - the editor's open/close and attempt callbacks drive the page's state;
 *  - the trailing ⋯ menu pops out or focuses the window, opens the activity
 *    panel only while it is closed, and enters or returns to split view;
 *  - the title half stands down in split view, the sessions toggle in chat
 *    embeds, and the trailing menu in every embed.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, act, waitFor, cleanup } from '@testing-library/react'
import type { ComponentProps, ReactNode } from 'react'

vi.mock('../pages/chat/ChatPageMessageContent', () => ({
  ChatHeaderMenu: (p: { onReveal: () => void; onRename: () => void; onAutoTitle: () => void; triggerLabel: ReactNode }) => (
    <div data-testid="header-menu">
      <span data-testid="trigger-label">{p.triggerLabel}</span>
      <button type="button" onClick={p.onReveal}>reveal</button>
      <button type="button" onClick={p.onRename}>rename</button>
      <button type="button" onClick={p.onAutoTitle}>auto-title</button>
    </div>
  ),
}))
vi.mock('../pages/chat/SessionTitleControl', () => ({
  default: (p: { onEditingChange: (open: boolean) => void; onError: (m: string) => void; onAttempt: () => void }) => (
    <div data-testid="title-editor">
      <button type="button" onClick={() => p.onEditingChange(false)}>close-editor</button>
      <button type="button" onClick={() => p.onEditingChange(true)}>open-editor</button>
      <button type="button" onClick={p.onAttempt}>attempt</button>
    </div>
  ),
}))
vi.mock('../components/UpdatePill', () => ({ default: () => null }))
const apiMock = vi.hoisted(() => ({ generateTitle: vi.fn() }))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, ...apiMock } }
})

import MobileTopBar from '../pages/chat/page/MobileTopBar'
import { i18nT } from '../i18n/t'
import { requestSlotReveal } from '../store/chatSlice'
import { sseSlotTitle } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

type Props = ComponentProps<typeof MobileTopBar>

let slot: HTMLElement
let trail: HTMLElement

function props(over: Partial<Props> = {}): Props {
  return {
    topbarSlot: slot,
    topbarTrailSlot: trail,
    embedMode: undefined,
    topbarSessionsToggle: <button type="button" data-testid="toggle">toggle</button>,
    activeSlot: 'slot-a',
    splitMode: false,
    splitFeatureEnabled: true,
    title: 'Release notes',
    editingTitle: false,
    setEditingTitleSlot: vi.fn(),
    showActionError: vi.fn(),
    setActionError: vi.fn(),
    currentSlot: { key: 'slot-a', title: 'Release notes' } as ChatSlot,
    sidebarAutoHidden: { current: true },
    openSidebar: vi.fn(),
    menuAutoTitleInFlight: { current: false },
    effectiveMode: 'normal',
    sidebarOnScreen: false,
    activePoppedOut: false,
    focusActivePopout: vi.fn(),
    openActivePopout: vi.fn(),
    activityOpen: false,
    toggleAct: vi.fn(),
    splitAnchorForActive: null,
    activeIsSplitAnchor: false,
    enterSplit: vi.fn(),
    mobileNewSessionItem: undefined,
    dispatch: vi.fn() as never,
    ...over,
  }
}

async function openMore() {
  const more = await screen.findByTestId('mobile-topbar-more')
  fireEvent.pointerDown(more, { button: 0, ctrlKey: false })
  fireEvent.click(more)
}

beforeEach(() => {
  slot = document.createElement('div')
  trail = document.createElement('div')
  document.body.append(slot, trail)
  apiMock.generateTitle.mockReset()
})
afterEach(() => { cleanup(); slot.remove(); trail.remove() })

describe('leading cell', () => {
  it('renders the toggle then the title into the slot, marking an incognito session', () => {
    render(<MobileTopBar {...props({ currentSlot: { key: 'slot-a', memory_mode: 'incognito' } as ChatSlot })} />)
    expect(slot.querySelector('[data-testid="toggle"]')).not.toBeNull()
    expect(slot.querySelector('[data-testid="mobile-topbar-title"]')).not.toBeNull()
    expect(screen.getByLabelText(i18nT('pages.chatPage.incognito_memory_writes_disabled'))).toBeInTheDocument()
    expect(screen.getByTestId('trigger-label')).toHaveTextContent('Release notes')
  })

  it('marks a temporary session', () => {
    render(<MobileTopBar {...props({ currentSlot: { key: 'slot-a', memory_mode: 'temporary' } as ChatSlot })} />)
    expect(screen.getByLabelText(i18nT('pages.chatPage.temporary_no_memory_reads_or_writes'))).toBeInTheDocument()
  })

  it('stands the title down in split view and the toggle down in a chat embed', () => {
    const { unmount } = render(<MobileTopBar {...props({ splitMode: true })} />)
    expect(screen.getByTestId('toggle')).toBeInTheDocument()
    expect(screen.queryByTestId('mobile-topbar-title')).toBeNull()
    unmount()
    render(<MobileTopBar {...props({ embedMode: 'chat' })} />)
    expect(screen.queryByTestId('toggle')).toBeNull()
    expect(screen.getByTestId('mobile-topbar-title')).toBeInTheDocument()
    expect(screen.queryByTestId('mobile-topbar-more')).toBeNull()
  })

  it('keeps the toggle but draws no title with no session open, and renders nothing without slots', () => {
    const { unmount } = render(<MobileTopBar {...props({ activeSlot: null })} />)
    expect(screen.getByTestId('toggle')).toBeInTheDocument()
    expect(screen.queryByTestId('mobile-topbar-title')).toBeNull()
    expect(screen.queryByTestId('mobile-topbar-more')).toBeNull()
    unmount()
    const { container } = render(<MobileTopBar {...props({ topbarSlot: null, topbarTrailSlot: null })} />)
    expect(container).toBeEmptyDOMElement()
    expect(slot).toBeEmptyDOMElement()
  })
})

describe('session menu actions', () => {
  it('reveal clears the auto-hide memory, opens the drawer and requests the row reveal', () => {
    const p = props()
    render(<MobileTopBar {...p} />)
    fireEvent.click(screen.getByText('reveal'))
    expect(p.sidebarAutoHidden.current).toBeNull()
    expect(p.openSidebar).toHaveBeenCalledTimes(1)
    expect(p.dispatch).toHaveBeenCalledWith(requestSlotReveal('slot-a'))
  })

  it('rename opens the shared title editor for the active session', () => {
    const p = props()
    render(<MobileTopBar {...p} />)
    fireEvent.click(screen.getByText('rename'))
    expect(p.setEditingTitleSlot).toHaveBeenCalledWith('slot-a')
  })

  it('auto-title runs one generation at a time and writes the new title through the store', async () => {
    let resolve!: (v: { title: string }) => void
    apiMock.generateTitle.mockReturnValue(new Promise(r => { resolve = r }))
    const p = props()
    render(<MobileTopBar {...p} />)
    fireEvent.click(screen.getByText('auto-title'))
    fireEvent.click(screen.getByText('auto-title'))
    expect(apiMock.generateTitle).toHaveBeenCalledTimes(1)
    expect(apiMock.generateTitle).toHaveBeenCalledWith('slot-a')
    expect(p.setActionError).toHaveBeenCalledWith(null)
    await act(async () => { resolve({ title: 'Shipping plan' }) })
    expect(p.dispatch).toHaveBeenCalledWith(sseSlotTitle({ key: 'slot-a', title: 'Shipping plan' }))
    expect(p.menuAutoTitleInFlight.current).toBe(false)
  })

  it('auto-title narrates a failure through the page, titled', async () => {
    apiMock.generateTitle.mockRejectedValue(new Error('model offline'))
    const p = props()
    render(<MobileTopBar {...p} />)
    fireEvent.click(screen.getByText('auto-title'))
    await waitFor(() => expect(p.showActionError).toHaveBeenCalledWith('model offline', i18nT('pages.chatPage.could_not_generate_title')))
    expect(p.dispatch).not.toHaveBeenCalled()
    expect(p.menuAutoTitleInFlight.current).toBe(false)
  })

  it('the open title editor drives the page state', () => {
    const p = props({ editingTitle: true })
    render(<MobileTopBar {...p} />)
    expect(screen.queryByTestId('header-menu')).toBeNull()
    fireEvent.click(screen.getByText('close-editor'))
    fireEvent.click(screen.getByText('open-editor'))
    fireEvent.click(screen.getByText('attempt'))
    expect((p.setEditingTitleSlot as ReturnType<typeof vi.fn>).mock.calls).toEqual([[null], ['slot-a']])
    expect(p.setActionError).toHaveBeenCalledWith(null)
  })
})

describe('trailing ⋯ menu', () => {
  it('pops the session out, opens the activity panel and enters split view', async () => {
    const p = props()
    render(<MobileTopBar {...p} />)
    expect(trail.querySelector('[data-testid="mobile-topbar-more"]')).not.toBeNull()
    await openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: i18nT('pages.chatPage.pop_out_to_window') }))
    expect(p.openActivePopout).toHaveBeenCalledWith('slot-a', 'Release notes')
    await openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: i18nT('pages.chatPage.open_activity_panel') }))
    expect(p.toggleAct).toHaveBeenCalledTimes(1)
    await openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: i18nT('pages.chatPage.enter_split_view') }))
    expect(p.enterSplit).toHaveBeenCalledWith('slot-a')
  })

  it('focuses the popped-out window, hides an open activity panel row, and returns to the split', async () => {
    const p = props({ activePoppedOut: true, activityOpen: true, splitAnchorForActive: 'slot-z' })
    render(<MobileTopBar {...p} />)
    await openMore()
    expect(screen.queryByRole('menuitem', { name: i18nT('pages.chatPage.open_activity_panel') })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: i18nT('pages.chatPage.pop_out_to_window') })).toBeNull()
    fireEvent.click(await screen.findByRole('menuitem', { name: i18nT('pages.chatPage.focus_popped_out_window') }))
    expect(p.focusActivePopout).toHaveBeenCalledWith('slot-a')
    await openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: i18nT('pages.chatPage.return_to_split_view') }))
    expect(p.enterSplit).toHaveBeenCalledWith('slot-z')
  })

  it('offers no split row when the split feature is off, and enters split for the anchor itself', async () => {
    const { unmount } = render(<MobileTopBar {...props({ splitFeatureEnabled: false })} />)
    await openMore()
    expect(screen.queryByRole('menuitem', { name: i18nT('pages.chatPage.enter_split_view') })).toBeNull()
    unmount()
    const p = props({ splitAnchorForActive: 'slot-a', activeIsSplitAnchor: true })
    render(<MobileTopBar {...p} />)
    await openMore()
    fireEvent.click(await screen.findByRole('menuitem', { name: i18nT('pages.chatPage.enter_split_view') }))
    expect(p.enterSplit).toHaveBeenCalledWith('slot-a')
  })
})
