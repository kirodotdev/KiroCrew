/**
 * A dimmed folder item that closes the menu on click shows the reader nothing —
 * the standing offline reason goes with it, so the refusal is indistinguishable
 * from the action having happened.
 *
 * Radix keys menu close on `onSelect`, so suppression has to happen there: an
 * `onClick` that merely returns early leaves the selection to proceed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder } from '../types'
import type { RootState } from '../store'

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import ChatSidebar from '../pages/ChatSidebar'

const FOLDER_ID = 'f-settings'
const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'Drafts', order: 0, collapsed: true } as ChatFolder]

/** `connected` is explicit on purpose: createTestStore() models a DISCONNECTED
 *  dashboard, so an inherited default would silently run the offline branch. */
function renderSidebar(connected: boolean) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

// Synchronous by necessity: Radix tears the folder menu down on the first
// macrotask in jsdom, so callers must drive it in the same tick.
function openFolderMenu() {
  fireEvent.keyDown(screen.getByTestId(`folder-menu-${FOLDER_ID}`), { key: 'Enter' })
  expect(screen.getByTestId(`folder-settings-${FOLDER_ID}`)).toBeTruthy()
}

beforeEach(() => { localStorage.setItem('mc-session-stale-collapse-ms', '0') })

describe('ChatSidebar – a refused folder item does not take the reason away with it', () => {
  it('offline Folder settings leaves the menu open instead of closing on a refusal', () => {
    const store = renderSidebar(false)
    openFolderMenu()
    // Control: the store really is offline, so the survival below is the
    // suppression firing rather than the item never having been gated.
    expect(store.getState().dashboard.connected).toBe(false)
    fireEvent.click(screen.getByTestId(`folder-settings-${FOLDER_ID}`))
    // Radix keys close on onSelect, so an onClick that only returns early lets
    // the menu — and the standing offline reason inside it — disappear.
    expect(screen.getByTestId(`folder-settings-${FOLDER_ID}`)).toBeInTheDocument()
    expect(screen.getByTestId(`folder-rename-${FOLDER_ID}`)).toBeInTheDocument()
  })

  it('offline Folder settings opens no settings modal', () => {
    renderSidebar(false)
    openFolderMenu()
    fireEvent.click(screen.getByTestId(`folder-settings-${FOLDER_ID}`))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('connected Folder settings still opens the modal, so the gate is not blanket', () => {
    renderSidebar(true)
    openFolderMenu()
    expect(screen.queryByTestId('folder-offline-reason')).toBeNull()
    fireEvent.click(screen.getByTestId(`folder-settings-${FOLDER_ID}`))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('the gated rows READ as dimmed, not merely aria-disabled', () => {
    renderSidebar(false)
    openFolderMenu()
    // A screenshot of this menu showed seven aria-disabled rows rendering at full
    // weight, because opacity alone is illegible here without the muted colour.
    const row = screen.getByTestId(`folder-rename-${FOLDER_ID}`)
    expect(row.className).toContain('opacity-40')
    expect(row.className).toContain('text-muted')
    // The local show/hide toggle is the deliberate full-weight exception — it
    // changes a view preference and reaches no gateway — so it must not pick it up.
    expect(screen.getByTestId(`folder-visibility-${FOLDER_ID}`).className).not.toContain('text-muted')
  })

  it('connected the same row carries no dim — the control for the case above', () => {
    renderSidebar(true)
    openFolderMenu()
    const row = screen.getByTestId(`folder-rename-${FOLDER_ID}`)
    expect(row.className).not.toContain('opacity-40')
    expect(row.className).not.toContain('text-muted')
  })
})

describe('ChatSidebar \u2013 an offline folder refusal is not an update failure', () => {
  it('says only that the gateway is offline, offers no hand-off, and retires on reconnect', async () => {
    const store = renderSidebar(false)
    fireEvent.doubleClick(screen.getByText('Drafts'))
    const notice = screen.getByTestId('folder-action-error')
    // Nothing was sent, so "Folder update failed" would be a false claim, and a
    // hand-off cannot reach a gateway that is down.
    expect(notice.textContent).toContain('Gateway offline')
    expect(notice.textContent).not.toContain('Folder update failed')
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
    // The retirement runs in an effect, so it needs the reconnect to COMMIT.
    store.dispatch(sseConnected())
    await waitFor(() => expect(screen.queryByTestId('folder-action-error')).toBeNull())
  })

  it('wraps on the same rule as the rename notice beside it', () => {
    renderSidebar(false)
    fireEvent.doubleClick(screen.getByText('Drafts'))
    // Both notices sit at the same narrow width, so one squeezing its text to a
    // word per line while its sibling does not is the difference a reader sees.
    const notice = screen.getByTestId('folder-action-error')
    expect(notice.querySelector('.flex-wrap')).not.toBeNull()
  })
})
