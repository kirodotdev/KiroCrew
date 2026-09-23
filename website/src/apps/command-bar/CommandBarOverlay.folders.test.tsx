import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatFolder } from '../../types'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * The Command Bar's FOLDERS VIEW.
 *
 * The bar is the default Cmd+K surface (`command-bar` ships `defaultEnabled: true`
 * and its manifest overlay claims the host's `quick-search` slot), so the palette's
 * Folders tab is unreachable for anyone who has not disabled the app. Folders are
 * reached here the way sessions are, and these tests pin that shape:
 *
 *  - the ROOT holds one row per corpus — `Search Folders` — and never the folder
 *    list itself, which is what keeps the first page from filling with the user's
 *    own filing before they have typed,
 *  - entering the view lands on the whole list, narrows as the user types, and
 *    reaches a child by an ancestor's name,
 *  - activating a row asks the sidebar to reveal that folder and lands on `/chat`,
 *  - the root issues NO request; entering the view is the activation event that may
 *    pay for one.
 */

const dispatch = vi.fn()
const navigate = vi.fn()

const storeState: {
  dashboard: { slots: Record<string, unknown>[]; unreadSlots: string[] }
  chat: { slotStatusDetail: Record<string, unknown>; activeSlot: string | null }
} = {
  dashboard: { slots: [], unreadSlots: [] },
  chat: { slotStatusDetail: {}, activeSlot: null },
}

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  requestFolderReveal: (folderId: string) => ({ type: 'requestFolderReveal', folderId }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({
    navigate,
    enterInsertOrNewSession: vi.fn(),
    newSessionWithToken: vi.fn(),
  }),
}))
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<
    typeof import('../../components/commandPalette/providers/recentsProvider')
  >()),
  useRecentsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))

/** Every network call the overlay could make, so a request is observable. */
const listApps = vi.fn(async () => [])
const chatFolders = vi.fn(async () => [] as unknown[])
vi.mock('../../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...(a as [])),
    chatFolders: (...a: unknown[]) => chatFolders(...(a as [])),
  },
}))

/**
 * `Sydney Property` holds `Inspections`, so a nested folder's breadcrumb and its
 * ancestor-as-keyword behaviour are both exercised. `Trading Desk` is the row that
 * must NOT surface when the query names another folder.
 */
const FOLDERS = [
  { id: 'f-syd', name: 'Sydney Property', parent_id: '', order: 0, collapsed: false, hidden: false },
  { id: 'f-insp', name: 'Inspections', parent_id: 'f-syd', order: 1, collapsed: false, hidden: false },
  { id: 'f-trade', name: 'Trading Desk', parent_id: '', order: 2, collapsed: false, hidden: false },
]

/**
 * Mount the bar with the folder list already in the shared cache, which is where
 * the sidebar's own read (or the WebSocket) leaves it in production.
 *
 * Pass `seed: false` for the cold-cache case: nothing is in the key, so the view's
 * own fetch is what produces the list.
 */
function mount(folders: unknown[] = FOLDERS, opts: { seed?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (opts.seed !== false) client.setQueryData(['chat-folders'], folders)
  const onClose = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return { onClose, client }
}

const type = (text: string) => {
  const input = screen.getByRole('combobox')
  fireEvent.change(input, { target: { value: text } })
}

/**
 * The option row whose visible text contains `text`.
 *
 * Matched on the ROW's `textContent`, not with `getByText`: a title the query
 * matched is rendered through `<Highlighted>`, which splits it into one element
 * per matched run, so the folder name exists on screen without existing as any
 * single text node. Asserting through the row is what makes a test read the same
 * whether the string was highlighted or not.
 */
const rowByText = (text: string): HTMLElement => {
  const rows = screen.queryAllByRole('option')
  const hit = rows.find(r => (r.textContent || '').includes(text))
  if (!hit) {
    throw new Error(
      `no option row containing "${text}"; rows: ${rows.map(r => r.textContent).join(' | ')}`,
    )
  }
  return hit
}

/** Whether any option row shows `text` — the negative form of {@link rowByText}. */
const hasRow = (text: string): boolean =>
  screen.queryAllByRole('option').some(r => (r.textContent || '').includes(text))

/**
 * Mount the bar and enter the folders view the way a user does — the root's own
 * row, with no query typed, which is how the view's listing state is reached.
 *
 * The scope is confirmed by the PLACEHOLDER rather than by a row: the error and
 * cold-cache cases legitimately have no rows yet, and a helper that waited for one
 * would hang on exactly the states those tests exist to pin.
 */
const openFoldersView = async (folders: unknown[] = FOLDERS, opts: { seed?: boolean } = {}) => {
  const mounted = mount(folders, opts)
  await waitFor(() => expect(hasRow('Search Folders')).toBe(true))
  fireEvent.mouseDown(rowByText('Search Folders'))
  await waitFor(() => expect(screen.getByPlaceholderText('Search all folders…')).toBeTruthy())
  return mounted
}

beforeEach(() => {
  vi.clearAllMocks()
  chatFolders.mockResolvedValue([])
  dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })
  storeState.dashboard = { slots: [], unreadSlots: [] }
  storeState.chat = { slotStatusDetail: {}, activeSlot: null }
})

describe('command bar — the root', () => {
  it('offers one row for the folders corpus and never the folder list itself', async () => {
    mount()
    type('sydney')
    // `Sydney Property` is in the cache and matches the query — and still must not
    // be a root row. A corpus is entered here, not flattened into the first page.
    await waitFor(() => expect(screen.getByRole('combobox')).toBeTruthy())
    expect(hasRow('Sydney Property')).toBe(false)
    expect(hasRow('Inspections')).toBe(false)
  })

  it('lists Search Folders as a view row on an empty query', async () => {
    mount()
    await waitFor(() => expect(hasRow('Search Folders')).toBe(true))
    // A `view` row states that it opens a surface rather than acting.
    expect(rowByText('Search Folders').textContent).toContain('View')
  })

  it('keeps one row per corpus — sessions, artifacts and folders — and one tail row each', async () => {
    // Three corpora now share this surface, each reached the same way, and the row
    // that carries a typed query into one of them must not have displaced another's.
    // The folders rows were added last, so this is the assertion that would catch
    // them landing on top of a row that was already there.
    mount()
    await waitFor(() => expect(hasRow('Search Folders')).toBe(true))
    expect(hasRow('Search Sessions')).toBe(true)
    expect(hasRow('Search Artifacts')).toBe(true)
    type('sydney')
    await waitFor(() => expect(hasRow('Search folders for')).toBe(true))
    expect(hasRow('Search sessions for')).toBe(true)
    expect(hasRow('Search artifacts for')).toBe(true)
  })

  it('carries a typed query into the folders view instead of dead-ending', async () => {
    mount()
    type('sydney')
    // The tail row for the corpus the typed word did not reach in the root.
    await waitFor(() => expect(hasRow('Search folders for')).toBe(true))
    fireEvent.mouseDown(rowByText('Search folders for'))
    // The query survives the hand-off: the view opens already narrowed, so the
    // reader does not retype what they just typed.
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
    expect(hasRow('Trading Desk')).toBe(false)
  })

  it('builds the root WITHOUT issuing a request, which is the launcher invariant', async () => {
    mount()
    await waitFor(() => expect(hasRow('Search Folders')).toBe(true))
    type('sydney')
    await waitFor(() => expect(hasRow('Search folders for')).toBe(true))
    // Cache-only, and not even that: the root builds no folder row at all, so the
    // endpoint's synchronous on-disk session walk is never paid for a keystroke.
    expect(chatFolders).not.toHaveBeenCalled()
    expect(listApps).not.toHaveBeenCalled()
  })
})

describe('command bar — folders view', () => {
  it('opens on the whole folder list, not on an empty screen', async () => {
    await openFoldersView()
    // The listing IS the view's empty state, so Enter always has a row to act on.
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
    expect(hasRow('Inspections')).toBe(true)
    expect(hasRow('Trading Desk')).toBe(true)
  })

  it('narrows on one character, since the corpus is already local', async () => {
    await openFoldersView()
    await waitFor(() => expect(hasRow('Trading Desk')).toBe(true))
    // `y` is in `Sydney Property` and in no other folder's name, so one keystroke
    // is enough to separate them. There is no minimum query length here: the
    // sessions view has one because its engine is a backend, and this one's corpus
    // is a cached list.
    type('y')
    await waitFor(() => expect(hasRow('Trading Desk')).toBe(false))
    // Awaited separately: a new query drops the previous result set for a tick, so
    // reading the positive too early sees the gap rather than the answer.
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
  })

  it('shows a nested folder with its ancestry path, not a bare name', async () => {
    await openFoldersView()
    type('inspections')
    await waitFor(() => expect(hasRow('Inspections')).toBe(true))
    // The breadcrumb is what tells two same-named leaves apart.
    expect(rowByText('Inspections').textContent).toContain('Sydney Property')
  })

  it('reaches a folder by an ANCESTOR name, which its own title does not contain', async () => {
    await openFoldersView()
    type('sydney')
    // `Inspections` matches nothing in "sydney" itself; the path carries it.
    await waitFor(() => expect(hasRow('Inspections')).toBe(true))
  })

  it('asks the sidebar to reveal the folder, then lands on the chat surface', async () => {
    const { onClose } = await openFoldersView()
    type('trading')
    await waitFor(() => expect(hasRow('Trading Desk')).toBe(true))
    fireEvent.mouseDown(rowByText('Trading Desk'))
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({ type: 'requestFolderReveal', folderId: 'f-trade' }),
    )
    // The route change is part of the action: the sidebar only renders on /chat, so
    // revealing without navigating would flash a row the user cannot see.
    expect(navigate).toHaveBeenCalledWith('/chat')
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('dispatches the reveal BEFORE navigating, so an unmounted sidebar replays it', async () => {
    await openFoldersView()
    type('trading')
    await waitFor(() => expect(hasRow('Trading Desk')).toBe(true))
    dispatch.mockClear()
    navigate.mockClear()
    fireEvent.mouseDown(rowByText('Trading Desk'))
    await waitFor(() => expect(navigate).toHaveBeenCalled())
    // Ordering is the guarantee: the store holds the request precisely because the
    // sidebar may mount only as a result of the navigate.
    expect(dispatch.mock.invocationCallOrder[0]).toBeLessThan(
      navigate.mock.invocationCallOrder[0],
    )
  })

  it('names the Enter action "Open" on a folder row, not "Open Session"', async () => {
    // The footer's job is to say what Enter does, and what it does here is reveal a
    // folder. The sessions view's own verb would promise a conversation.
    await openFoldersView()
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
    await waitFor(() => expect(screen.queryByText('Open')).not.toBeNull())
    expect(screen.queryByText('Open Session')).toBeNull()
  })

  it('offers the full list when a query matches nothing, instead of bottoming out', async () => {
    await openFoldersView()
    type('zzzznope')
    // A dead end is a ROW, so the keyboard can act on it without leaving the list.
    await waitFor(() => expect(hasRow('No folders match')).toBe(true))
    fireEvent.mouseDown(rowByText('No folders match'))
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
  })

  it('names the Enter action on that row for the list it returns to, not recency', async () => {
    // The sessions view's own verb is "Show Recent", which the folder listing is
    // not: it is every folder in sidebar order, and a folder has no recency for the
    // word to stand for. The row and the footer are keyed off the same slot tag, so
    // neither can name a destination the other does not.
    await openFoldersView()
    type('zzzznope')
    await waitFor(() => expect(hasRow('No folders match')).toBe(true))
    await waitFor(() => expect(screen.queryByText('Show All Folders')).not.toBeNull())
    expect(screen.queryByText('Show Recent')).toBeNull()
  })

  it('tells a reader with no folders that they have none, not that no sessions matched', async () => {
    // The one way to reach the empty state in this view: a failure pushes the retry
    // row, a query that matched nothing pushes the row above, and a load in flight
    // draws the skeleton. So an empty list here is an empty corpus, and the copy has
    // to be the folders one — the sessions string reports a match that was never
    // attempted against a query the reader never typed.
    await openFoldersView([])
    await waitFor(() => expect(screen.queryByText(/No folders yet/)).not.toBeNull())
    expect(screen.queryByText('No sessions match')).toBeNull()
  })

  it('fetches the list once on a cold cache — entering the view is what pays', async () => {
    chatFolders.mockResolvedValue(FOLDERS)
    await openFoldersView(FOLDERS, { seed: false })
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
    expect(chatFolders).toHaveBeenCalledTimes(1)
  })

  it('reports a failed folder read through ErrorNotice, with Retry still in the list', async () => {
    chatFolders.mockRejectedValue(new Error('gateway down'))
    await openFoldersView(FOLDERS, { seed: false })
    // WHAT failed is said ABOVE the list by the shared error surface. An error shown
    // to the reader renders through `ErrorNotice`, never as a hand-written red row
    // inside an option (AUTOSDE `errors-use-error-notice`), so the row below carries
    // the action only.
    const failure = await screen.findByRole('alert')
    expect(failure.textContent).toContain('Search failed')
    // The raw rejection is not user-facing here either.
    expect(failure.textContent).not.toContain('gateway down')
    // Outside the listbox, so no option ever owns two competing interactions, and
    // the failure text is not re-added as a row by a later change.
    expect(failure.closest('[role="option"]')).toBeNull()
    expect(hasRow('Search failed')).toBe(false)
    // A failure is not an empty corpus, and the way out is reachable by keyboard.
    chatFolders.mockResolvedValue(FOLDERS)
    fireEvent.mouseDown(rowByText('Retry'))
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
  })

  it('survives a folders cache holding a non-array', async () => {
    // The key is shared, and a bad payload must not take the whole launcher down.
    await openFoldersView({ not: 'an array' } as unknown as unknown[])
    expect(screen.getByRole('combobox')).toBeTruthy()
    expect(hasRow('Sydney Property')).toBe(false)
  })

  it('keeps a folder whose parent_id names a folder that does not exist', async () => {
    await openFoldersView([
      { id: 'f-orphan', name: 'Orphan Desk', parent_id: 'f-gone', order: 0, collapsed: false, hidden: false },
    ])
    type('orphan')
    // An orphan is re-rooted rather than dropped, so the row is still reachable —
    // a folder the user can see in the sidebar must be findable here.
    await waitFor(() => expect(hasRow('Orphan Desk')).toBe(true))
  })

  it('survives a corrupt PARENT name on a query that misses every folder title', async () => {
    // A folder's ancestry is matched as a string, and a non-string name off disk
    // reaches `.toLowerCase()` inside the matcher. `folderTree` guards it at the
    // source; this holds that guarantee from the surface the user opens.
    await openFoldersView([
      { id: 'f-bad', name: 42, order: 0, collapsed: false, hidden: false },
      { id: 'f-kid', name: 'Quarterly', order: 0, parent_id: 'f-bad', collapsed: false, hidden: false },
    ] as unknown as ChatFolder[])
    // `zzz` is a subsequence of neither `Quarterly` nor its breadcrumb, so the path
    // field is reached.
    type('zzz')
    await waitFor(() => expect(hasRow('Quarterly')).toBe(false))
    // And the corrupt name is not findable as the literal text either: a non-string
    // name reads as EMPTY rather than being stringified into `42`.
    type('42')
    await waitFor(() => expect(hasRow('No folders match')).toBe(true))
    expect(hasRow('Quarterly')).toBe(false)
  })
})
