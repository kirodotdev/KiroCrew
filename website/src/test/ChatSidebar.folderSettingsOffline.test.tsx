/**
 * A dimmed folder item that closes the menu on click shows the reader nothing —
 * the standing offline reason goes with it, so the refusal is indistinguishable
 * from the action having happened.
 *
 * Radix keys menu close on `onSelect`, so suppression has to happen there: an
 * `onClick` that merely returns early leaves the selection to proceed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'
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
/** The same folder already open, for the branch where the refused act is
 *  COLLAPSING rather than expanding. */
const openFolders: ChatFolder[] = [{ id: FOLDER_ID, name: 'Drafts', order: 0, collapsed: false } as ChatFolder]

/** `connected` is explicit on purpose: createTestStore() models a DISCONNECTED
 *  dashboard, so an inherited default would silently run the offline branch. */
function renderSidebar(connected: boolean, creatingSlot = false, fixture: ChatFolder[] = folders) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, creatingSlot } as unknown as RootState['chat'],
  })
  // The folder cache is FROZEN. `api` is mocked to resolve `[]` for everything, so
  // the mount refetch of ['chat-folders'] replaces the seeded folder with nothing a
  // tick later. Synchronous cases never noticed, but any case that awaits a DOM
  // change lost the folder mid-assertion and failed for the wrong reason. Freezing
  // makes the seed authoritative for the whole case.
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, refetchOnMount: false, refetchOnWindowFocus: false, refetchOnReconnect: false },
      mutations: { retry: false },
    },
  })
  qc.setQueryData(['chat-folders'], fixture)
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


describe('ChatSidebar \u2013 a rename in flight when the gateway drops keeps its draft', () => {
  /** GPT 5.6 (blocking) on 8a2e7de715: the gates stop an editor OPENING offline,
   *  but the gateway can drop while one is already open. `renameCommit` then fired
   *  the PATCH and cleared `editingId` unconditionally; the mutation's rollback
   *  restores the STORED name, so what was lost was the text just typed -- on the
   *  one path everything else here refuses. */
  it('offline the editor and the typed draft survive the commit, with a reason', () => {
    const store = renderSidebar(true)
    // Open the editor while CONNECTED -- that is the only way in, and it is the
    // precondition the finding describes.
    fireEvent.doubleClick(screen.getByText('Drafts'))
    const input = screen.getByDisplayValue('Drafts') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Renamed while online' } })
    // Held by reference, not re-queried: the row re-renders on the disconnect
    // below, and a display-value query would race that re-render.
    expect(input.value).toBe('Renamed while online')

    // Now the gateway drops, with the editor still open and the draft unsaved.
    // In act(), so React commits the new `connected` BEFORE the commit handler
    // runs -- a bare dispatch leaves the handler closed over the online value and
    // the test then passes for the wrong reason.
    act(() => { store.dispatch(sseDisconnected()) })
    fireEvent.keyDown(input, { key: 'Enter' })

    // The editor is still mounted AND still holds the typed text. Re-queried,
    // because the disconnect re-renders the row and React may hand back a new node.
    expect(screen.getByDisplayValue('Renamed while online')).toBeInTheDocument()
    // And the refusal names renaming, so the user knows why it did not commit.
    const notice = screen.getByTestId('folder-action-offline')
    expect(notice.textContent).toContain('rename folders')
  })
})

describe('ChatSidebar \u2013 offline, disclosure still works: expand is a read, not a write', () => {
  /** Design + UX on 3b0e657a07: refusing the toggle offline made the cached
   *  sessions inside a collapsed folder unreachable for the whole outage, to
   *  protect a preference write nobody asked about. `toggleCollapse` now flips the
   *  CACHED flag and sends nothing, so the control works. Two earlier revisions
   *  reached for the opposite (a silent refusal, then a refusal that explained
   *  itself); this replaces both. */
  /* Asserted on `aria-expanded` and the label, not on whether the inner rows are
   * in the DOM: a row inside a collapsed folder stays MOUNTED here (FolderBody
   * animates height), so presence would pass in both states and prove nothing. */
  /* Awaited, not synchronous: the flip goes through `queryClient.setQueryData`, and
   * React Query delivers that notification on a microtask, so the cache is already
   * updated when `fireEvent` returns but the DOM is one tick behind. Measured while
   * writing these: the cache read `collapsed: false` immediately while the button
   * still said `aria-expanded="false"`. A synchronous assertion here fails for the
   * wrong reason. */
  it('offline the header toggles open, so cached contents are reachable', async () => {
    renderSidebar(false)
    const header = screen.getByLabelText('Expand folder Drafts')
    expect(header.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(header)
    // Opened from the local cache flip. Re-queried by its NEW label, which is
    // itself the evidence the state changed.
    await waitFor(() =>
      expect(screen.getByLabelText('Collapse folder Drafts').getAttribute('aria-expanded')).toBe('true'))
  })

  it('offline it closes again, so the toggle is a toggle and not a one-way open', async () => {
    renderSidebar(false, false, openFolders)
    fireEvent.click(screen.getByLabelText('Collapse folder Drafts'))
    await waitFor(() =>
      expect(screen.getByLabelText('Expand folder Drafts').getAttribute('aria-expanded')).toBe('false'))
  })

  it('offline the toggle raises no notice and no failure \u2014 nothing was sent', () => {
    renderSidebar(false)
    fireEvent.click(screen.getByLabelText('Expand folder Drafts'))
    // A write that went out and failed would surface as one of these two. Their
    // absence is how "flips locally, sends nothing" reads from the outside.
    expect(screen.queryByTestId('folder-action-error')).toBeNull()
    expect(screen.queryByTestId('folder-action-offline')).toBeNull()
  })

  it('offline the header advertises no refusal \u2014 it is not refusing anything', () => {
    renderSidebar(false)
    const header = screen.getByLabelText('Expand folder Drafts')
    // An earlier revision set both of these to explain a refusal. Keeping them on a
    // control that now WORKS would promise a reconnect the reader does not need.
    expect(header.getAttribute('aria-disabled')).not.toBe('true')
    expect(header.getAttribute('title')).toBeNull()
  })
})

describe('ChatSidebar \u2013 the split button\u2019s primary segment says why it refuses', () => {
  /** Opus 5 + UX on 349e1513e7: the segment bailed on `if (!connected) return` with
   *  nothing else -- no notice, and it kept `active:scale-95` and the hover fill, so
   *  the sidebar's most prominent create control depressed under the finger and
   *  produced nothing. That is the dead-click pattern the rest of this PR removes,
   *  on the one control a first-time user reaches for first. */
  it('offline it raises the neutral notice and announces itself', () => {
    renderSidebar(false)
    const primary = screen.getByLabelText('New chat session disabled \u2014 gateway offline')
    expect(primary.getAttribute('aria-disabled')).toBe('true')
    expect(primary.getAttribute('title')).toBe('Gateway offline \u2014 reconnect to create sessions')
    // The press affordances are off: a control that will refuse must not animate as
    // though it accepted.
    expect(primary.className).not.toContain('active:scale-95')
    expect(primary.className).not.toContain('hover:bg-accent-hover')
    fireEvent.click(primary)
    expect(screen.getByTestId('folder-action-offline').textContent).toContain('create sessions')
    expect(screen.queryByTestId('folder-action-error')).toBeNull()
  })

  it('connected it keeps its ordinary name and its press affordances', () => {
    renderSidebar(true)
    const primary = screen.getByLabelText('New chat session')
    // ABSENT, not "false": the spread is conditional on purpose here, because this
    // button also carries `disabled={creatingSlot}` and announcing
    // aria-disabled="false" on a control the browser had already made inert during an
    // in-flight create advertises an unfocusable button as available.
    expect(primary.getAttribute('aria-disabled')).toBeNull()
    expect(primary.getAttribute('title')).toBe('New chat')
    expect(primary.className).toContain('active:scale-95')
  })
})

describe('ChatSidebar \u2013 the quick-create + beside a folder refuses like the menu row', () => {
  /** Opus 5 on 3b0e657a07: the folder-row `+` buttons kept calling
   *  `createChatInFolder` with no gate, so offline they dispatched a doomed create
   *  and raised a create-failure banner while the `New chat` menu rows a few pixels
   *  away dimmed and refused. The bail now lives in the sink, which covers all four
   *  of those buttons at once; the button still carries its own announcement. */
  /* Addressed by testid, not by label: an empty folder renders the quick-create in
   * TWO places -- the header's hover cluster and the empty-folder placeholder row --
   * and both are gated, so a label query finds both and is ambiguous. That both
   * carry it is the point of putting the bail in the sink. */
  it('offline the + announces itself and opens no session', () => {
    renderSidebar(false)
    const plus = screen.getByTestId(`folder-new-chat-${FOLDER_ID}`)
    expect(plus.getAttribute('aria-disabled')).toBe('true')
    expect(plus.getAttribute('aria-label')).toBe('New chat in Drafts disabled \u2014 gateway offline')
    expect(plus.getAttribute('title')).toBe('Gateway offline \u2014 reconnect to create sessions')
    fireEvent.click(plus)
    // The refusal is the neutral notice, naming creating rather than a failure.
    expect(screen.getByTestId('folder-action-offline').textContent).toContain('create sessions')
    expect(screen.queryByTestId('folder-action-error')).toBeNull()
  })

  it('offline the empty-folder placeholder + is gated too, from the same sink', () => {
    renderSidebar(false)
    expect(screen.getByTestId(`folder-empty-new-chat-${FOLDER_ID}`).getAttribute('aria-disabled')).toBe('true')
  })

  it('connected the same + carries its ordinary name \u2014 the control for the cases above', () => {
    renderSidebar(true)
    const plus = screen.getByTestId(`folder-new-chat-${FOLDER_ID}`)
    expect(plus.getAttribute('aria-disabled')).toBe('false')
    expect(plus.getAttribute('aria-label')).toBe('New chat in Drafts')
    expect(plus.getAttribute('title')).toBe('New chat in Drafts')
  })
})

describe('ChatSidebar \u2013 Move to folder reads as unavailable offline', () => {
  /** UX on 3b0e657a07: the standing reason row teaches "the dimmed actions need a
   *  connection", so a full-weight `Move folder to` row read as working and the
   *  refusal only arrived after the reader had chosen a target. #10911 was to dim
   *  it; measured 2026-09-28 that PR is CONFLICTING, so the dim lands here. */
  it('offline the trigger is dimmed and names moving', () => {
    renderSidebar(false)
    openFolderMenu()
    const trigger = screen.getByText('Move folder to').closest('[role="menuitem"]') as HTMLElement
    expect(trigger.className).toContain('opacity-40')
    expect(trigger.className).toContain('text-muted')
    expect(trigger.getAttribute('aria-disabled')).toBe('true')
    expect(trigger.getAttribute('title')).toBe('Gateway offline \u2014 reconnect to move folders')
  })

  it('connected it carries no dim and no offline tooltip', () => {
    renderSidebar(true)
    openFolderMenu()
    const trigger = screen.getByText('Move folder to').closest('[role="menuitem"]') as HTMLElement
    expect(trigger.className).not.toContain('opacity-40')
    expect(trigger.getAttribute('title')).toBeNull()
  })
})
