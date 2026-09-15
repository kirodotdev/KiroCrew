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
function renderSidebar(connected: boolean, creatingSlot = false) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, creatingSlot } as unknown as RootState['chat'],
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
  it('is a status notice, not an alert, offers no hand-off, and retires on reconnect', async () => {
    const store = renderSidebar(false)
    fireEvent.doubleClick(screen.getByText('Drafts'))
    const notice = screen.getByTestId('folder-action-offline')
    // Nothing was sent, so "Folder update failed" would be a false claim, a
    // hand-off cannot reach a gateway that is down, and the danger surface would
    // dress a refusal as a failure — hence status, and no error notice at all.
    expect(notice.getAttribute('role')).toBe('status')
    expect(notice.textContent).toContain('Gateway offline')
    expect(notice.textContent).not.toContain('Folder update failed')
    expect(screen.queryByTestId('folder-action-error')).toBeNull()
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
    // The retirement runs in an effect, so it needs the reconnect to COMMIT.
    store.dispatch(sseConnected())
    await waitFor(() => expect(screen.queryByTestId('folder-action-offline')).toBeNull())
  })

  it('wraps on the same rule as the rename notice beside it', () => {
    renderSidebar(false)
    fireEvent.doubleClick(screen.getByText('Drafts'))
    // Both notices sit at the same narrow width, so one squeezing its text to a
    // word per line while its sibling does not is the difference a reader sees.
    const notice = screen.getByTestId('folder-action-offline')
    expect(notice.querySelector('.flex-wrap')).not.toBeNull()
  })
})


describe('ChatSidebar – the offline dim reaches the rows that set their own colour', () => {
  /** Opus 5 on 6c5c0651c2: `cn` is tailwind-merge, so the LAST class in a
   *  conflicting group wins. With the primitive composing `offline` before
   *  `className`, the delete rows' own `text-danger` won the text-colour group
   *  and they rendered full-saturation red at 40% opacity while every sibling
   *  gated row went muted — the dim half-applied on the one row where
   *  "unavailable" matters most. */
  it('offline the delete row goes muted, its danger colour dropped', () => {
    renderSidebar(false)
    openFolderMenu()
    const del = screen.getByTestId(`folder-delete-${FOLDER_ID}`)
    expect(del.className).toContain('text-muted')
    expect(del.className).toContain('opacity-40')
    // tailwind-merge drops the losing class outright, so its absence is the
    // binding signal: with the old order this read text-danger and no text-muted.
    // Matched as a CLASS TOKEN, not a substring — `focus:text-danger` survives on
    // purpose (a different variant group, so focus still reads as destructive)
    // and a substring check would fail on it while the base colour is long gone.
    expect(del.className.split(/\s+/)).not.toContain('text-danger')
  })

  it('connected the same row keeps its danger colour and no dim', () => {
    renderSidebar(true)
    openFolderMenu()
    const del = screen.getByTestId(`folder-delete-${FOLDER_ID}`)
    expect(del.className).toContain('text-danger')
    expect(del.className).not.toContain('opacity-40')
  })
})

describe('ChatSidebar – an in-flight create does not advertise its inert rows as available', () => {
  /** Opus 5 on 6c5c0651c2: `offlineProps` returns `{ 'aria-disabled': false }`
   *  when ONLINE, and the create rows spread it alongside `disabled={creatingSlot}`.
   *  During an in-flight create Radix makes them inert (`data-disabled` +
   *  pointer-events-none) while that spread announced aria-disabled="false" —
   *  unfocusable and inert, advertised as available. Spread only while offline. */
  it('online with a create in flight the row never announces aria-disabled=false', () => {
    renderSidebar(true, true)
    fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
    const row = screen.getByTestId('new-plain-chat')
    expect(row.getAttribute('aria-disabled')).not.toBe('false')
  })

  it('offline the same row still carries the offline announcement', () => {
    renderSidebar(false)
    fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
    const row = screen.getByTestId('new-plain-chat')
    expect(row.getAttribute('aria-disabled')).toBe('true')
    expect(row.getAttribute('aria-label')).toBe('New chat disabled \u2014 gateway offline')
  })
})

describe('ChatSidebar – New folder is gated like the subfolder row it shares a modal with', () => {
  it('offline the New folder row is announced disabled and opens no dialog', () => {
    renderSidebar(false)
    fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
    const row = screen.getByTestId('new-folder')
    expect(row.getAttribute('aria-disabled')).toBe('true')
    expect(row.getAttribute('aria-label')).toBe('New folder disabled \u2014 gateway offline')
    expect(row.className).toContain('opacity-40')
    fireEvent.click(row)
    // The menu — and the standing reason inside it — survive the refusal.
    expect(screen.getByTestId('new-folder')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('connected the New folder row is live and opens the create dialog', () => {
    renderSidebar(true)
    fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
    fireEvent.click(screen.getByTestId('new-folder'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})


describe('ChatSidebar \u2013 every gated row announces WHICH row is unavailable', () => {
  /** Opus 5 on 7cceaa33d3: three rows passed no label to `offlineProps`, so it
   *  skipped its aria-label branch and the reason lived only in `title=`. A screen
   *  reader heard a plain "Delete folder ... disabled" with no why \u2014 on the row
   *  the primitive's own comment calls the one where "unavailable" matters most. */
  /* Only the delete row is asserted: the hide row is behind
   * `folderOffersHide(...)` and does not render for this fixture, so an assertion
   * on it would fail for the wrong reason. Its label fix is the same one-argument
   * change, applied at the same time. */
  it('offline the delete row carries the offline accessible name', () => {
    renderSidebar(false)
    openFolderMenu()
    expect(screen.getByTestId(`folder-delete-${FOLDER_ID}`).getAttribute('aria-label'))
      .toBe('Delete folder disabled \u2014 gateway offline')
  })

  it('connected it carries no offline name \u2014 the control for the case above', () => {
    renderSidebar(true)
    openFolderMenu()
    const label = screen.getByTestId(`folder-delete-${FOLDER_ID}`).getAttribute('aria-label')
    expect(label === null || !label.includes('gateway offline')).toBe(true)
  })
})
