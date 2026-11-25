import { useEffect, useState } from 'react'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CREW_HISTORY_SEARCH_TAB_ID, useCrewHistorySearchTab } from '../../pages/members/useCrewHistorySearchTab'
import { historySearchHostMarks, useMessageSearch } from '../../hooks/useMessageSearch'
import { anotherEscapeOwnerIsOpen } from '../chat-input/escapeOwners'
import ChatPaneHistorySearch from './ChatPaneHistorySearch'

vi.mock('./ChatHistorySearch', () => ({ default: () => <div data-testid="search-body" /> }))

const noop = () => {}

// The Bots page wiring: ChatPane's find state and its opener and closer
// (`onHistorySearchOpener`), the side panel (docked, or the overlay on a
// phone) with its strip, and the Search tab's body kept mounted and `hidden`
// behind another tab, as SidePanel does.
function BotHost({ isMobile = false, panelOpenAtStart = false, startTab = 'crew-sessions' }: { isMobile?: boolean; panelOpenAtStart?: boolean; startTab?: string }) {
  const [activeTabId, setActive] = useState<string | null>(startTab)
  const [dockedOpen, setDockedOpen] = useState(panelOpenAtStart && !isMobile)
  const [overlayOpen, setOverlayOpen] = useState(panelOpenAtStart && isMobile)
  const beside = !isMobile
  const panelOpen = beside ? dockedOpen : overlayOpen
  const find = useMessageSearch('bot-chat')
  const tab = useCrewHistorySearchTab({
    activeTabId, setActive, fallbackTabId: 'crew-dashboard', isMobile, beside, setDockedOpen, setOverlayOpen,
    closeReplyThread: noop, panelOpen, panelShown: panelOpen,
  })
  const { setHistorySearchOpener } = tab
  const { open, close } = find
  useEffect(() => {
    setHistorySearchOpener(open, close)
    return () => setHistorySearchOpener(null)
  }, [setHistorySearchOpener, open, close])
  const setPanel = (next: boolean) => (beside ? setDockedOpen(next) : setOverlayOpen(next))
  return <>
    {tab.openHistorySearch && <button type="button" onClick={tab.openHistorySearch}>Search</button>}
    <button type="button" onClick={() => setPanel(!panelOpen)}>toggle panel</button>
    {panelOpen && (
      <div data-testid="panel" className={beside ? '' : 'fixed inset-0'}>
        <div data-testid="strip">
          <button type="button" onClick={() => setActive('crew-dashboard')}>dashboard</button>
          {tab.historySearchOpen && (
            <span data-testid="search-chip">
              <button type="button" onClick={() => setActive(CREW_HISTORY_SEARCH_TAB_ID)}>search tab</button>
              <button type="button" aria-label="Close tab" onClick={tab.closeHistorySearch}>x</button>
            </span>
          )}
        </div>
        <div data-testid="active-tab">{activeTabId}</div>
      </div>
    )}
    {tab.historySearchOpen && (
      <div hidden={!panelOpen || activeTabId !== CREW_HISTORY_SEARCH_TAB_ID}>
        <div data-testid="search-host" ref={tab.setHistorySearchHost} {...historySearchHostMarks(tab.historySearchOnScreen)} />
      </div>
    )}
    <ChatPaneHistorySearch slotKey="bot-chat" isOpen={find.isOpen} focusNonce={find.focusNonce} onClose={find.close}
      host={tab.historySearchHost} onOpenChange={tab.onHistorySearchOpenChange} />
  </>
}

const openSearch = async () => {
  fireEvent.click(await screen.findByRole('button', { name: 'Search' }))
  // Wait for ChatPaneHistorySearch's lazy ChatHistorySearch chunk.
  return screen.findByTestId('search-body', {}, { timeout: 5000 })
}
const closeChip = () => fireEvent.click(within(screen.getByTestId('search-chip')).getByRole('button', { name: 'Close tab' }))

describe('ChatPaneHistorySearch in a side panel tab', () => {
  it('owns Escape only while its tab is on screen', async () => {
    render(<BotHost />)
    await openSearch()
    expect(anotherEscapeOwnerIsOpen()).toBe(true)

    // Another tab in front: the search stays mounted but owns no Escape, so
    // the composer's Escape-to-stop runs and Escape leaves the search open.
    fireEvent.click(screen.getByRole('button', { name: 'dashboard' }))
    expect(anotherEscapeOwnerIsOpen()).toBe(false)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByTestId('search-body')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'search tab' }))
    expect(anotherEscapeOwnerIsOpen()).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('search-body')).toBeNull()
  })
})

describe('the bot Search tab is on demand', () => {
  it('a panel opened normally shows no Search tab, also after a search was left open and the panel hidden', async () => {
    render(<BotHost panelOpenAtStart />)
    expect(screen.queryByTestId('search-chip')).toBeNull()
    await openSearch()
    expect(screen.getByTestId('search-chip')).toBeInTheDocument()
    // Hiding the panel ends the search, so it cannot sit in the strip.
    fireEvent.click(screen.getByRole('button', { name: 'toggle panel' }))
    fireEvent.click(screen.getByRole('button', { name: 'toggle panel' }))
    expect(screen.getByTestId('panel')).toBeInTheDocument()
    expect(screen.queryByTestId('search-chip')).toBeNull()
    expect(screen.queryByTestId('search-body')).toBeNull()
    expect(screen.getByTestId('active-tab')).toHaveTextContent('crew-sessions')
  })

  it('a search that opened the panel closes the panel when its close button ends it', async () => {
    render(<BotHost />)
    expect(screen.queryByTestId('panel')).toBeNull()
    await openSearch()
    expect(screen.getByTestId('active-tab')).toHaveTextContent(CREW_HISTORY_SEARCH_TAB_ID)
    closeChip()
    expect(screen.queryByTestId('search-body')).toBeNull()
    expect(screen.queryByTestId('panel')).toBeNull()
  })

  it('a panel already open on another tab goes back to that tab and stays open', async () => {
    render(<BotHost panelOpenAtStart startTab="crew-dashboard" />)
    await openSearch()
    expect(screen.getByTestId('active-tab')).toHaveTextContent(CREW_HISTORY_SEARCH_TAB_ID)
    closeChip()
    expect(screen.queryByTestId('search-chip')).toBeNull()
    expect(screen.getByTestId('panel')).toBeInTheDocument()
    expect(screen.getByTestId('active-tab')).toHaveTextContent('crew-dashboard')
  })

  it('a remount after the search closed brings no Search tab back', async () => {
    const view = render(<BotHost panelOpenAtStart />)
    await openSearch()
    closeChip()
    view.unmount()
    render(<BotHost panelOpenAtStart />)
    expect(screen.queryByTestId('search-chip')).toBeNull()
  })
})

describe('the bot Search tab on a phone', () => {
  it('opens the overlay on the focused Search tab, with no sheet of its own', async () => {
    render(<BotHost isMobile />)
    const body = await openSearch()
    expect(screen.getByTestId('panel')).toBeInTheDocument()
    expect(screen.getByTestId('active-tab')).toHaveTextContent(CREW_HISTORY_SEARCH_TAB_ID)
    expect(within(screen.getByTestId('search-host')).getByTestId('search-body')).toBe(body)
    expect(screen.getByTestId('chat-pane-history-search')).not.toHaveClass('fixed')
  })

  it('the close button closes the overlay, even one already open on another tab', async () => {
    render(<BotHost isMobile panelOpenAtStart startTab="crew-dashboard" />)
    await openSearch()
    closeChip()
    expect(screen.queryByTestId('search-body')).toBeNull()
    expect(screen.queryByTestId('panel')).toBeNull()
  })

  it('Escape in the overlay closes it back to the chat', async () => {
    render(<BotHost isMobile />)
    await openSearch()
    act(() => { fireEvent.keyDown(document, { key: 'Escape' }) })
    expect(screen.queryByTestId('panel')).toBeNull()
  })
})
