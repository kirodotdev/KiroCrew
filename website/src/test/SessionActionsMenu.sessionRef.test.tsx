/**
 * Two discoverability actions on the shared session menu (#11823): "Copy session
 * ID" (always present — the bare key, not a URL) and "Reference in chat" (present
 * only when the row passes a handler, the way "Fork chat" is sidebar-only because
 * the header's own session would be a self-reference). "Copy link" beside them
 * reports whether its share URL reached the clipboard the same way Copy session
 * ID does.
 */
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

// "Copy Kiro Crew session ID" copies the bare key through this, and "Copy link"
// its share URL; stub it so the tests assert what was written and choose
// whether the write landed, not the jsdom clipboard.
const copyToClipboard = vi.hoisted(() => vi.fn(() => Promise.resolve(true)))
vi.mock('../utils/clipboard', () => ({ copyToClipboard }))

import SessionActionsMenu from '../components/SessionActionsMenu'
import type { RootState } from '../store'

const dashboardState = {
  status: {}, connected: true, slots: [{ key: 'chat-1', title: 'My Session' }], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as unknown as RootState['dashboard']

function renderMenu(onReferenceInChat?: () => void) {
  const store = createTestStore({ dashboard: dashboardState })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <DropdownMenu>
              <DropdownMenuTrigger>menu</DropdownMenuTrigger>
              <DropdownMenuContent>
                <SessionActionsMenu variant="dropdown" slotKey="chat-1" onReferenceInChat={onReferenceInChat} />
              </DropdownMenuContent>
            </DropdownMenu>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.keyDown(utils.getByText('menu'), { key: 'Enter' })
  return utils
}

describe('SessionActionsMenu copy-session-id item', () => {
  // The confirm-window test installs a fake clock; restore the real one after
  // every test so a failed assertion cannot leak it into the next test.
  afterEach(() => { vi.useRealTimers() })
  beforeEach(() => { copyToClipboard.mockClear() })
  it('always renders Copy Kiro Crew session ID and copies the bare key (not a URL)', async () => {
    renderMenu()
    fireEvent.click(await screen.findByText('Copy Kiro Crew session ID'))
    expect(copyToClipboard).toHaveBeenCalledWith('chat-1')
  })

  it('says a refused copy through ErrorNotice that outlives the acknowledgment, and never claims success', async () => {
    copyToClipboard.mockResolvedValueOnce(false)
    renderMenu()
    const item = await screen.findByText('Copy Kiro Crew session ID')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => { fireEvent.click(item) })
    // Both clipboard layers refused: the failure is said through ErrorNotice
    // plus its menu hand-off, not only by a transient label.
    const notice = screen.getByTestId('session-menu-copy-id-error')
    expect(notice.textContent).toContain('Couldn’t copy the session ID')
    // Its hand-off item is wired to this notice.
    const noticeId = notice.getAttribute('id') ?? notice.querySelector('[id]')?.getAttribute('id')
    expect(noticeId).toBeTruthy()
    expect(document.querySelector(`[role="menuitem"][aria-describedby="${noticeId}"]`)).toBeTruthy()
    expect(screen.queryByText('Copied')).toBeNull()
    // It stays past the hook's acknowledgment window...
    act(() => { vi.advanceTimersByTime(5000) })
    expect(screen.getByTestId('session-menu-copy-id-error')).toBeTruthy()
    // ...and clears when the next press succeeds.
    await act(async () => { fireEvent.click(screen.getByText('Copy Kiro Crew session ID')) })
    expect(screen.queryByTestId('session-menu-copy-id-error')).toBeNull()
    expect(screen.getByTestId('session-menu-copy-id-label').textContent).toBe('Copied')
  })

  it('keeps the failure notice when a quick retry also fails', async () => {
    copyToClipboard.mockResolvedValueOnce(false).mockResolvedValueOnce(false)
    renderMenu()
    const item = await screen.findByText('Copy Kiro Crew session ID')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => { fireEvent.click(item) })
    expect(screen.getByTestId('session-menu-copy-id-error')).toBeTruthy()
    // Retry inside the acknowledgment window: the status stays `failed`.
    act(() => { vi.advanceTimersByTime(500) })
    await act(async () => { fireEvent.click(screen.getByText('Copy Kiro Crew session ID')) })
    expect(copyToClipboard).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('session-menu-copy-id-error')).toBeTruthy()
    // Still there once every acknowledgment timer has run out.
    act(() => { vi.advanceTimersByTime(5000) })
    expect(screen.getByTestId('session-menu-copy-id-error')).toBeTruthy()
    expect(screen.queryByText('Copied')).toBeNull()
  })

  it('confirms a successful copy in the item itself, then returns to its label', async () => {
    renderMenu()
    const item = await screen.findByText('Copy Kiro Crew session ID')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    // Settle the clipboard promise inside act so the acknowledgment lands.
    await act(async () => { fireEvent.click(item) })
    // The menu stays open and the same item now reads "Copied" with a check.
    const label = screen.getByTestId('session-menu-copy-id-label')
    expect(label.textContent).toBe('Copied')
    expect(label.getAttribute('aria-live')).toBe('polite')
    expect(label.closest('[role="menuitem"]')?.querySelector('svg.lucide-check')).toBeTruthy()
    act(() => { vi.advanceTimersByTime(1499) })
    expect(screen.getByTestId('session-menu-copy-id-label').textContent).toBe('Copied')
    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.getByTestId('session-menu-copy-id-label').textContent).toBe('Copy Kiro Crew session ID')
  })
})

describe('SessionActionsMenu copy-link item', () => {
  afterEach(() => { vi.useRealTimers() })
  beforeEach(() => { copyToClipboard.mockClear() })

  it('confirms a landed copy in the item itself, then returns to its label', async () => {
    renderMenu()
    const item = await screen.findByText('Copy link')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => { fireEvent.click(item) })
    // The share URL is what was written, not the bare key.
    expect(copyToClipboard).toHaveBeenCalledWith(expect.stringMatching(/\/chat(\/[^?]*)?\?sid=chat-1$/))
    // The menu stays open and the same item reads "Copied" with a check.
    const label = screen.getByTestId('session-menu-copy-link-label')
    expect(label.textContent).toBe('Copied')
    expect(label.getAttribute('aria-live')).toBe('polite')
    expect(label.closest('[role="menuitem"]')?.querySelector('svg.lucide-check')).toBeTruthy()
    expect(screen.queryByTestId('session-menu-copy-link-error')).toBeNull()
    act(() => { vi.advanceTimersByTime(1500) })
    expect(screen.getByTestId('session-menu-copy-link-label').textContent).toBe('Copy link')
  })

  it('says a refused copy through ErrorNotice that survives a failing retry and clears on success', async () => {
    copyToClipboard.mockResolvedValueOnce(false).mockResolvedValueOnce(false)
    renderMenu()
    const item = await screen.findByText('Copy link')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => { fireEvent.click(item) })
    const notice = screen.getByTestId('session-menu-copy-link-error')
    expect(notice.textContent).toContain('Couldn’t copy the link')
    // Its hand-off item is wired to this notice.
    const noticeId = notice.getAttribute('id') ?? notice.querySelector('[id]')?.getAttribute('id')
    expect(noticeId).toBeTruthy()
    expect(document.querySelector(`[role="menuitem"][aria-describedby="${noticeId}"]`)).toBeTruthy()
    expect(screen.queryByText('Copied')).toBeNull()
    // A retry that fails again keeps the notice past every acknowledgment timer.
    act(() => { vi.advanceTimersByTime(500) })
    await act(async () => { fireEvent.click(screen.getByText('Copy link')) })
    act(() => { vi.advanceTimersByTime(5000) })
    expect(screen.getByTestId('session-menu-copy-link-error')).toBeTruthy()
    expect(screen.queryByText('Copied')).toBeNull()
    // The next press that lands clears it and confirms.
    await act(async () => { fireEvent.click(screen.getByText('Copy link')) })
    expect(copyToClipboard).toHaveBeenCalledTimes(3)
    expect(screen.queryByTestId('session-menu-copy-link-error')).toBeNull()
    expect(screen.getByTestId('session-menu-copy-link-label').textContent).toBe('Copied')
  })
})

describe('SessionActionsMenu reference-in-chat item', () => {
  it('renders Reference in chat when the row passes a handler, and selecting it calls it', async () => {
    const onReferenceInChat = vi.fn()
    renderMenu(onReferenceInChat)
    fireEvent.click(await screen.findByText('Reference in chat'))
    expect(onReferenceInChat).toHaveBeenCalledTimes(1)
  })

  it('renders no Reference in chat item when no handler is passed', async () => {
    renderMenu()
    expect(await screen.findByText('Pin')).toBeTruthy()
    expect(screen.queryByText('Reference in chat')).toBeNull()
  })
})
