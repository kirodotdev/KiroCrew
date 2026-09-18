import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * Folder rows in the Command Bar launcher.
 *
 * The bar is the DEFAULT Cmd+K surface (`command-bar` ships `defaultEnabled: true`
 * and its manifest overlay claims the host's `quick-search` slot), so the palette's
 * Folders tab is unreachable for anyone who has not disabled the app. These tests
 * pin the behaviour on the surface the user actually opens:
 *
 *  - a folder is reachable by typing its name,
 *  - activating it asks the sidebar to reveal that folder and lands on `/chat`,
 *  - the root still issues NO request to build the rows, which is the invariant
 *    the whole launcher design rests on.
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
const chatFolders = vi.fn(async () => [])
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

function mount(folders: unknown[] = FOLDERS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Seeded, not fetched: the overlay's folders query is `enabled: false`, so this
  // mirrors production where the sidebar's own read (or the WebSocket) fills the key.
  client.setQueryData(['chat-folders'], folders)
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

beforeEach(() => {
  vi.clearAllMocks()
  dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })
  storeState.dashboard = { slots: [], unreadSlots: [] }
  storeState.chat = { slotStatusDetail: {}, activeSlot: null }
})

describe('command bar — folder rows', () => {
  it('finds a folder by name and files it under the Folders group', async () => {
    mount()
    type('sydney')
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
    expect(screen.getByText('Folders')).toBeTruthy() // the group header, never highlighted
    // The row names its own kind, so a reader can tell it from a command.
    expect(rowByText('Sydney Property').textContent).toContain('Folder')
  })

  it('shows a nested folder with its ancestry path, not a bare name', async () => {
    mount()
    type('inspections')
    await waitFor(() => expect(hasRow('Inspections')).toBe(true))
    // The breadcrumb is what tells two same-named leaves apart.
    expect(rowByText('Inspections').textContent).toContain('Sydney Property')
  })

  it('reaches a folder by an ANCESTOR name, which its own title does not contain', async () => {
    mount()
    type('sydney')
    // `Inspections` matches nothing in "sydney" itself; the path carries it.
    await waitFor(() => expect(hasRow('Inspections')).toBe(true))
  })

  it('does not surface an unrelated folder', async () => {
    mount()
    type('sydney')
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
    expect(hasRow('Trading Desk')).toBe(false)
  })

  it('asks the sidebar to reveal the folder, then lands on the chat surface', async () => {
    const { onClose } = mount()
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
    mount()
    type('trading')
    await waitFor(() => expect(hasRow('Trading Desk')).toBe(true))
    fireEvent.mouseDown(rowByText('Trading Desk'))
    await waitFor(() => expect(navigate).toHaveBeenCalled())
    // Ordering is the guarantee: the store holds the request precisely because the
    // sidebar may mount only as a result of the navigate.
    const revealOrder = dispatch.mock.invocationCallOrder[0]
    expect(revealOrder).toBeLessThan(navigate.mock.invocationCallOrder[0])
  })

  it('builds the rows WITHOUT issuing a request, which is the launcher invariant', async () => {
    mount()
    type('sydney')
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
    // Cache-only. A folder fetch here would pay for the endpoint's synchronous
    // on-disk session walk on every keystroke in the root.
    expect(chatFolders).not.toHaveBeenCalled()
    expect(listApps).not.toHaveBeenCalled()
  })

  it('renders no folder rows on a cold cache instead of fetching', async () => {
    mount([])
    type('sydney')
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(false))
    expect(chatFolders).not.toHaveBeenCalled()
  })

  it('survives a folders cache holding a non-array', async () => {
    // The key is shared, and a bad payload must not take the whole launcher down.
    mount({ not: 'an array' } as unknown as unknown[])
    type('sydney')
    await waitFor(() => expect(screen.getByRole('combobox')).toBeTruthy())
    expect(hasRow('Sydney Property')).toBe(false)
  })

  it('ignores a folder whose parent_id names a folder that does not exist', async () => {
    mount([
      { id: 'f-orphan', name: 'Orphan Desk', parent_id: 'f-gone', order: 0, collapsed: false, hidden: false },
    ])
    type('orphan')
    // An orphan is re-rooted rather than dropped, so the row is still reachable —
    // a folder the user can see in the sidebar must be findable here.
    await waitFor(() => expect(hasRow('Orphan Desk')).toBe(true))
  })

  it('survives a corrupt PARENT name on a query that misses every folder title', async () => {
    // The crash Opus found, reproduced from its own chain: a folder row's `keywords`
    // carry its ancestor names, and `rankRootRows` hands each keyword to `fuzzyMatch`,
    // which calls `.toLowerCase()` on the candidate. A non-string PARENT name therefore
    // only throws on a query that misses the child's title AND its subtitle, so the
    // ranker falls through to the keyword field -- which is why guarding title and
    // subtitle alone left the launcher crashing in render.
    mount([
      { id: 'f-bad', name: 42, order: 0, collapsed: false, hidden: false },
      { id: 'f-kid', name: 'Quarterly', order: 0, parent_id: 'f-bad', collapsed: false, hidden: false },
    ] as unknown as ChatFolder[])
    // `zzz` is a subsequence of neither `Quarterly` nor its breadcrumb, so the keyword
    // field is reached.
    type('zzz')
    await waitFor(() => expect(screen.getByRole('combobox')).toBeTruthy())
    // Still standing, and the corrupt name is not findable as text either.
    expect(hasRow('Quarterly')).toBe(false)
    type('42')
    await waitFor(() => expect(screen.getByRole('combobox')).toBeTruthy())
    expect(hasRow('Quarterly')).toBe(false)
  })

  it('names the Enter action "Open", not "Run", while a folder row is highlighted', async () => {
    // The footer's job is to say what Enter does. "Run" is the strongest verb it
    // has — it is what the rows that approve or merge carry — so on a folder it
    // invites the reader to stop and check before pressing Enter. A folder row is
    // wired as an `invoke` (landing on one is a reveal, not only a route change),
    // which is why the verb is chosen by the row's GROUP and not by its handler.
    mount()
    type('sydney')
    await waitFor(() => expect(hasRow('Sydney Property')).toBe(true))
    // The first row is pre-highlighted, and with this query it is the folder.
    await waitFor(() => expect(screen.queryByText('Open')).not.toBeNull())
    expect(screen.queryByText('Run')).toBeNull()
  })
})
