import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import CommandBarOverlay from './CommandBarOverlay'
import { i18nT } from '../../i18n/t'
import { PREVIEW_CREW } from '../../utils/previewFlags'

/**
 * The Command Bar's CREWMATES VIEW.
 *
 * The crew is reached here the way sessions, artifacts and folders are — one row in
 * the root that opens a view — and these tests pin that shape:
 *
 *  - the ROOT holds one row for the corpus, `Search Crewmates`, and never the roster
 *    itself; the roster is a FETCH, so a root group would either issue a request on
 *    every open of the bar or render empty on a cold install,
 *  - entering the view lands on the whole roster, narrows on a PARTIAL name, and
 *    tells two mates apart by face, name, role and whether they are working,
 *  - selecting a row navigates to that mate's own chat thread (`/members?member=…`),
 *  - the root issues NO request; entering the view is the activation event that pays
 *    for one.
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
  useAppStore: () => ({ getState: () => storeState }),
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
const kirocrewConfig = vi.fn(async () => ({}) as unknown)
const agentCatalog = vi.fn(async () => ({ agents: [] as unknown[], default_agent: '' }))
vi.mock('../../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...(a as [])),
    chatFolders: (...a: unknown[]) => chatFolders(...(a as [])),
    kirocrewConfig: (...a: unknown[]) => kirocrewConfig(...(a as [])),
    agentCatalog: (...a: unknown[]) => agentCatalog(...(a as [])),
  },
}))

function mate(over: Record<string, unknown> & { name: string }) {
  return {
    kiro_agent: '',
    workspace: '',
    memory_store: '',
    description: '',
    source: '',
    selection_kind: 'member',
    ...over,
  }
}

/**
 * Three crewmates, which is the smallest roster that proves a filter narrowed
 * rather than merely rendered: `oncall` and `accountant` both contain `c`, and only
 * `accountant` survives `acc`. `reviewer` carries a display label hiding the
 * identity `qa-bot`, which is what the route and the running lookup must key on.
 */
const ROSTER = [
  mate({ name: 'oncall', description: 'Watches the pager and triages alerts.' }),
  mate({ name: 'accountant', description: 'Reconciles the spend ledger.' }),
  mate({ name: 'qa-bot', display_name: 'reviewer', description: 'Reviews diffs.' }),
]

/**
 * The option row whose visible text contains `text`.
 *
 * Matched on the ROW's `textContent`, not with `getByText`: a title the query
 * matched is rendered through `<Highlighted>`, which splits it into one element per
 * matched run, so the name exists on screen without existing as any single text
 * node.
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
 * The word the bar uses for a busy agent.
 *
 * Read from the catalog rather than written as a literal, because the POINT of the
 * label is that it is the same one the sessions view shows one keystroke away -- a
 * literal here would keep passing if the two drifted apart.
 */
const BUSY = i18nT('components.commandPalette.providers.recentsProvider.thinking')

/**
 * How many option rows are on screen.
 *
 * What a NARROWING test must wait on. The listing already contains the row a filter
 * is supposed to leave behind, so waiting for that row to be present is satisfied
 * before the view has debounced and proves nothing; and waiting for the filtered-out
 * row to be ABSENT is satisfied during the in-flight window, where there are no rows
 * at all. The count separates all three states: 3 before, 0 in flight, 1 after.
 */
const rowCount = (): number => screen.queryAllByRole('option').length

/** The crewmate rows on screen, top to bottom, by the name each row shows. */
const rowOrder = (names: string[]): string[] =>
  screen
    .queryAllByRole('option')
    .map(r => names.find(n => (r.textContent || '').includes(n)))
    .filter((n): n is string => !!n)

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onClose = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return { onClose, client }
}

const type = (text: string) => {
  fireEvent.change(screen.getByRole('combobox'), { target: { value: text } })
}

/**
 * Mount the bar and enter the crewmates view the way a user does — the root's own
 * row, with no query typed, which is how the view's listing state is reached.
 *
 * The scope is confirmed by the PLACEHOLDER rather than by a row: the error and
 * empty-roster cases legitimately have no rows, and a helper that waited for one
 * would hang on exactly the states those tests exist to pin.
 */
const openMatesView = async () => {
  const mounted = mount()
  await waitFor(() => expect(hasRow('Search Crewmates')).toBe(true))
  fireEvent.mouseDown(rowByText('Search Crewmates'))
  await waitFor(() => expect(screen.getByPlaceholderText('Search all crewmates…')).toBeTruthy())
  return mounted
}

beforeEach(() => {
  vi.clearAllMocks()
  // The gate is OFF by default in the product, so every other test in this file has to
  // turn it on explicitly -- which is also what keeps the two gate tests above honest.
  localStorage.setItem(PREVIEW_CREW, '1')
  chatFolders.mockResolvedValue([])
  agentCatalog.mockResolvedValue({ agents: ROSTER, default_agent: '' })
  dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })
  storeState.dashboard = { slots: [], unreadSlots: [] }
  storeState.chat = { slotStatusDetail: {}, activeSlot: null }
})

describe('command bar — the crewmates preview gate', () => {
  it('advertises NOTHING about crewmates while the preview is off', async () => {
    // This bar is an INGRESS to `/members`, and that page is registered with
    // `previewFlag: PREVIEW_CREW`, so every other door applies this gate. The app ships
    // `defaultEnabled: true`, so without it a DEFAULT install advertises a page the
    // operator has not opted into.
    localStorage.removeItem(PREVIEW_CREW)
    mount()
    await waitFor(() => expect(hasRow('Search Sessions')).toBe(true))
    expect(hasRow('Search Crewmates')).toBe(false)
    // All three rows, because each is independently reachable.
    type('oncall')
    await waitFor(() => expect(hasRow('Search sessions for')).toBe(true))
    expect(hasRow('Search crewmates for')).toBe(false)
    // And nothing fetched the roster to decide any of that.
    expect(agentCatalog).not.toHaveBeenCalled()
  })

  it('offers them once the operator turns the preview on', async () => {
    localStorage.setItem(PREVIEW_CREW, '1')
    mount()
    await waitFor(() => expect(hasRow('Search Crewmates')).toBe(true))
    type('oncall')
    await waitFor(() => expect(hasRow('Search crewmates for')).toBe(true))
  })
})

describe('command bar — the root', () => {
  it('lists Search Crewmates as a view row on an empty query', async () => {
    mount()
    await waitFor(() => expect(hasRow('Search Crewmates')).toBe(true))
    // A `view` row states that it opens a surface rather than acting.
    expect(rowByText('Search Crewmates').textContent).toContain('View')
  })

  it('offers one row for the corpus and never the roster itself', async () => {
    mount()
    type('oncall')
    // A corpus is entered here, not flattened into the first page — so even an exact
    // crew name must not produce a crewmate row in the root.
    await waitFor(() => expect(hasRow('Search crewmates for')).toBe(true))
    expect(hasRow('Watches the pager')).toBe(false)
  })

  it('keeps one row per corpus, and one tail row each, with the crew row added last', async () => {
    // Four corpora now share this surface, each reached the same way. The crewmate
    // rows were added last, so this is the assertion that would catch them landing on
    // top of a row that was already there.
    mount()
    await waitFor(() => expect(hasRow('Search Crewmates')).toBe(true))
    expect(hasRow('Search Sessions')).toBe(true)
    expect(hasRow('Search Artifacts')).toBe(true)
    expect(hasRow('Search Folders')).toBe(true)
    type('oncall')
    await waitFor(() => expect(hasRow('Search crewmates for')).toBe(true))
    expect(hasRow('Search sessions for')).toBe(true)
    expect(hasRow('Search artifacts for')).toBe(true)
    expect(hasRow('Search folders for')).toBe(true)
  })

  it('reaches the view through a word the reader knows instead of our own', async () => {
    // "Crewmate" is this product's vocabulary. A reader who has not opened the
    // Crewmates page reaches for "agent", and the row has to answer to it.
    mount()
    type('agent')
    await waitFor(() => expect(hasRow('Search Crewmates')).toBe(true))
  })

  it('carries a typed query into the crewmates view instead of dead-ending', async () => {
    mount()
    type('acc')
    await waitFor(() => expect(hasRow('Search crewmates for')).toBe(true))
    fireEvent.mouseDown(rowByText('Search crewmates for'))
    // The query survives the hand-off: the view opens already narrowed, so the reader
    // does not retype what they just typed. The count is what says "narrowed" — the
    // view arriving on its full listing would also show `accountant`.
    await waitFor(() => expect(rowCount()).toBe(1))
    expect(hasRow('accountant')).toBe(true)
    expect(hasRow('oncall')).toBe(false)
  })

  it('builds the root WITHOUT fetching the catalog, which is the launcher invariant', async () => {
    mount()
    await waitFor(() => expect(hasRow('Search Crewmates')).toBe(true))
    type('oncall')
    await waitFor(() => expect(hasRow('Search crewmates for')).toBe(true))
    // The whole reason the roster is a view and not a root group: `useAgents` fetches
    // this endpoint on mount, and the root promises never to issue a request.
    expect(agentCatalog).not.toHaveBeenCalled()
    expect(chatFolders).not.toHaveBeenCalled()
    expect(listApps).not.toHaveBeenCalled()
  })
})

describe('command bar — crewmates view, listing and search', () => {
  it('lands on the whole roster, alphabetically, and pays for exactly one fetch', async () => {
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(rowOrder(['accountant', 'oncall', 'reviewer'])).toEqual([
      'accountant',
      'oncall',
      'reviewer',
    ])
    // Entering the view is the activation event, and it pays once.
    expect(agentCatalog).toHaveBeenCalledTimes(1)
  })

  it('asks for the GLOBAL roster, not whichever pane the reader last touched', async () => {
    // The bar fires from anywhere in the dashboard, so a session-scoped catalog read
    // would make which crew is reachable depend on the active chat's project.
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(agentCatalog).toHaveBeenCalledWith()
  })

  it('narrows on a PARTIAL name, which is the gesture the ticket asks for', async () => {
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(rowCount()).toBe(3)
    type('acc')
    // Waited on the COUNT, which is the only unambiguous signal here: `accountant` is
    // already on screen in the unfiltered listing, so waiting for it to be present
    // resolves before the view has debounced, and waiting for `oncall` to be absent
    // resolves during the in-flight window where no rows exist at all.
    await waitFor(() => expect(rowCount()).toBe(1))
    expect(hasRow('accountant')).toBe(true)
    expect(hasRow('oncall')).toBe(false)
  })

  it('narrows from ONE character, because the corpus is already in hand', async () => {
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(rowCount()).toBe(3)
    type('r')
    // Only `reviewer` carries an r; the count is what proves the filter ran rather
    // than the listing simply still being on screen.
    await waitFor(() => expect(rowCount()).toBe(1))
    expect(hasRow('reviewer')).toBe(true)
  })

  it('gives each row an avatar, a name and a one-line role, so two mates are distinguishable', async () => {
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    const row = rowByText('oncall')
    // The role, which is what tells two similarly-named mates apart.
    expect(row.textContent).toContain('Watches the pager and triages alerts.')
    // The face. It is the row's own art rather than a per-corpus glyph, which is the
    // only thing that makes the icon column identify the ROW.
    expect(row.querySelector('svg, img')).toBeTruthy()
    // And the two rows really are different faces, not one repeated glyph.
    const other = rowByText('accountant')
    expect(row.querySelector('svg, img')?.outerHTML).not.toBe(
      other.querySelector('svg, img')?.outerHTML,
    )
  })

  it('shows a working crewmate as working, read off the live slot frames', async () => {
    // A member slot is born with its crew pinned to `agent`, which is why the running
    // set is keyed on the name rather than on a slot key the catalog cannot rebuild.
    storeState.dashboard = {
      slots: [{ key: 'member-oncall', mode: 'member', agent: 'oncall', running: true }],
      unreadSlots: [],
    }
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(rowByText('oncall').textContent).toContain(BUSY)
    // And an idle mate is not labelled — the column is for state that is CHANGING.
    expect(rowByText('accountant').textContent).not.toContain(BUSY)
  })

  it('counts a mate whose SUBAGENTS are running as working', async () => {
    storeState.dashboard = {
      slots: [
        { key: 'member-oncall', mode: 'member', agent: 'oncall', running: false, subagents_running: true },
      ],
      unreadSlots: [],
    }
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(rowByText('oncall').textContent).toContain(BUSY)
  })

  it('ignores a running slot that is not a member thread', async () => {
    // An ordinary chat running the same agent is not that crewmate's own thread, and
    // reporting it as the mate working would tell the reader not to interrupt a mate
    // that is idle.
    storeState.dashboard = {
      slots: [{ key: 'chat-1', mode: 'chat', agent: 'oncall', running: true }],
      unreadSlots: [],
    }
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(rowByText('oncall').textContent).not.toContain(BUSY)
  })

  it('re-derives when a frame changes which mate is working, not after a stale window', async () => {
    // The running set is part of the query's identity precisely so this reaches the
    // screen: rebuilding the engine alone does not re-run a resolved query.
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(rowByText('oncall').textContent).not.toContain(BUSY)
    storeState.dashboard = {
      slots: [{ key: 'member-oncall', mode: 'member', agent: 'oncall', running: true }],
      unreadSlots: [],
    }
    // An ArrowDown, deliberately NOT a keystroke in the query. Typing would change
    // `mateQuery` and so change the key on its own, which would let this pass with the
    // running set absent from the key — the very thing it exists to pin. This only
    // re-renders, so the republished store read is the ONLY thing that can move the
    // key, and a stale cached row is what fails here.
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
    await waitFor(() => expect(rowByText('oncall').textContent).toContain(BUSY))
  })

  it('names Enter for the destination in the footer', async () => {
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    // The bar's promise is that Enter does something specific, and the footer says
    // what: this Enter lands in a conversation.
    expect(document.body.textContent).toContain('Open Chat')
  })

  it('offers the way back when a query matches nothing, so the view never dead-ends', async () => {
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    type('zzzzz-no-such-crew')
    await waitFor(() => expect(hasRow('show all instead')).toBe(true))
    fireEvent.mouseDown(rowByText('show all instead'))
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
  })

  it('tells a reader with no crew that they have none, AND takes them where one is made', async () => {
    // Not "no matches": the reader typed nothing, so reporting a failed match would
    // describe a query they never made. And not a centred sentence either -- naming the
    // Crewmates page without a way to reach it left a reader reporting that "nothing
    // looks like a link", so the one thing they came to do had no affordance.
    agentCatalog.mockResolvedValue({ agents: [], default_agent: '' })
    await openMatesView()
    await waitFor(() => expect(hasRow('No crewmates yet')).toBe(true))
    // A ROW, so the keyboard reaches it without leaving the list.
    expect(rowCount()).toBe(1)
    fireEvent.mouseDown(rowByText('No crewmates yet'))
    // No `?member=`: there is no crewmate to open.
    expect(navigate).toHaveBeenCalledWith('/members')
  })

  it('does not navigate away on a STALE Enter at that empty row either', async () => {
    // The guard covers every row a scoped view synthesizes, not only its results. This
    // row leaves the bar: with the check inside `case 'result':`, a keystroke followed by
    // Enter inside the debounce window navigated to `/members` and closed the bar off a
    // row that answered the previous query.
    agentCatalog.mockResolvedValue({ agents: [], default_agent: '' })
    await openMatesView()
    await waitFor(() => expect(hasRow('No crewmates yet')).toBe(true))
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'anyone' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(navigate).not.toHaveBeenCalled()
    // And once the rows answer the query, the same Enter goes where the row says. Retried
    // rather than waited on a condition: this row's text is the same for every query, so
    // there is nothing on screen that changes when the debounce settles.
    await waitFor(() => {
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(navigate).toHaveBeenCalledWith('/members')
    })
  })

  it('does not DISCARD the typed query on a stale Enter at a dead end', async () => {
    // The worst case of the same hole, and why the guard sits above the switch: the
    // no-match row's action wipes the query to re-arm the listing. At a zero-result dead
    // end -- exactly where a reader types another character -- an Enter inside the
    // debounce window erased the text they had just typed, while the stale row's own
    // label still showed the old query, so nothing warned them.
    await openMatesView()
    await waitFor(() => expect(rowCount()).toBe(3))
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'zzzznope' } })
    await waitFor(() => expect(hasRow('No crewmates match')).toBe(true))
    fireEvent.change(input, { target: { value: 'zzzznopq' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect((input as HTMLInputElement).value).toBe('zzzznopq')
  })

  it('shows the skeleton, not "no crewmates", while the roster is still loading', async () => {
    // The empty row is a CLAIM about the reader's crew. Made before the catalog answers,
    // it is a claim about nothing, and it would flash on every cold open.
    let release: (v: unknown) => void = () => {}
    agentCatalog.mockReturnValue(new Promise(r => { release = r }))
    await openMatesView()
    expect(hasRow('No crewmates yet')).toBe(false)
    release({ agents: [], default_agent: '' })
    await waitFor(() => expect(hasRow('No crewmates yet')).toBe(true))
  })

  it('EXCLUDES templates from the view, since they have no chat thread', async () => {
    agentCatalog.mockResolvedValue({
      agents: [...ROSTER, mate({ name: 'shipper', selection_kind: 'template' })],
      default_agent: '',
    })
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    expect(hasRow('shipper')).toBe(false)
  })

  it('NAMES the failure over the list, not just a bare Retry row', async () => {
    // The retry row deliberately carries no text, because this notice is what says
    // what failed. Without the notice a catalog rejection rendered one unlabelled
    // Retry and nothing else -- and a failed read then looks exactly like a crew-less
    // install, which is the pair of states `useAgents` records six triage passes
    // being lost to.
    agentCatalog.mockRejectedValue(new Error('catalog read refused: 503'))
    await openMatesView()
    await waitFor(() => expect(hasRow('Retry')).toBe(true))
    const notice = screen.getByText(/Search failed/i)
    expect(notice).toBeTruthy()
    // And the notice sits OUTSIDE the listbox, so an interactive control can never
    // end up inside an option row.
    expect(notice.closest('[role="option"]')).toBeNull()
  })

  it('keeps the reader\'s selection when a slot elsewhere changes the running set', async () => {
    // The running set is part of the query key, so a member slot finishing anywhere in
    // the dashboard mints a new key on an event the reader did not cause. Without
    // held-over rows the list blanks and the clamp effect pulls the selection back to
    // row 0 mid-navigation.
    await openMatesView()
    await waitFor(() => expect(rowCount()).toBe(3))
    const input = screen.getByRole('combobox')
    // Listing order is alphabetical: accountant, oncall, reviewer. Move to `reviewer`.
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    storeState.dashboard = {
      slots: [{ key: 'member-oncall', mode: 'member', agent: 'oncall', running: true }],
      unreadSlots: [],
    }
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    // The list never emptied, so the selection still has somewhere to be.
    await waitFor(() => expect(rowCount()).toBe(3))
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(navigate).toHaveBeenCalledWith('/members?member=qa-bot')
  })

  it('offers a retry ROW when the catalog read fails, reachable from the keyboard', async () => {
    // This is the one corpus with no warm-cache path, and `useAgents` records what a
    // swallowed failure on this endpoint cost: a failed load and a one-crew install
    // rendered identically through six triage passes. A bare <button> in a paragraph
    // would be unreachable for the keyboard path that got here.
    agentCatalog.mockRejectedValue(new Error('catalog read refused: 503'))
    await openMatesView()
    await waitFor(() => expect(hasRow('Retry')).toBe(true))
    agentCatalog.mockResolvedValue({ agents: ROSTER, default_agent: '' })
    fireEvent.mouseDown(rowByText('Retry'))
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
  })

  it('leaves the view on Backspace at an empty query, back to the root', async () => {
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Backspace' })
    await waitFor(() => expect(hasRow('Search Crewmates')).toBe(true))
    expect(hasRow('Watches the pager')).toBe(false)
  })
})

describe('command bar — crewmates view, selection', () => {
  it('navigates into the mate\'s own chat thread and closes the bar', async () => {
    const { onClose } = await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    fireEvent.mouseDown(rowByText('oncall'))
    expect(navigate).toHaveBeenCalledWith('/members?member=oncall')
    expect(onClose).toHaveBeenCalled()
  })

  it('navigates by the crewmate\'s IDENTITY even when a display label hides it', async () => {
    // The page resolves `?member=` against the immutable name, so routing by the
    // label would open the page on a crew that does not exist.
    await openMatesView()
    await waitFor(() => expect(hasRow('reviewer')).toBe(true))
    fireEvent.mouseDown(rowByText('reviewer'))
    expect(navigate).toHaveBeenCalledWith('/members?member=qa-bot')
  })

  it('encodes a crew name that would otherwise truncate the parameter', async () => {
    agentCatalog.mockResolvedValue({ agents: [mate({ name: 'Review & QA' })], default_agent: '' })
    await openMatesView()
    await waitFor(() => expect(hasRow('Review')).toBe(true))
    fireEvent.mouseDown(rowByText('Review'))
    expect(navigate).toHaveBeenCalledWith('/members?member=Review%20%26%20QA')
  })

  it('refuses a STALE Enter, so a fast typist never opens the wrong crewmate', async () => {
    // The view ranks from the DEBOUNCED query, so for 150ms after a keystroke the rows
    // answer the previous one. Typing `onc` and pressing Enter immediately opened
    // `accountant` -- the row selected against the older query. Enter must do nothing
    // until the rows describe what was typed: a dropped keystroke, never the wrong chat.
    await openMatesView()
    await waitFor(() => expect(rowCount()).toBe(3))
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'onc' } })
    // No debounce tick: the rows on screen are still the full roster.
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(navigate).not.toHaveBeenCalled()
    // Once the rows catch up, the same Enter opens the crewmate that was typed.
    await waitFor(() => expect(rowCount()).toBe(1))
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(navigate).toHaveBeenCalledWith('/members?member=oncall')
  })

  it('drops the previous words\' rows rather than holding them while the new read runs', async () => {
    // The hole the debounce comparison alone leaves. This view holds the rows the reader
    // is looking at across a key change, so the running set moving does not blank the
    // list under their keyboard — and `prev => prev` held them across a change of the
    // QUERY too. Past the catalog's stale window the post-debounce key change starts a
    // real read, so rows answering the previous words stayed on screen, and actionable,
    // for as long as it took: the debounced query matched what was typed while the rows
    // did not. A hanging read with the cache invalidated is that window held open.
    const { client } = await openMatesView()
    await waitFor(() => expect(rowCount()).toBe(3))
    agentCatalog.mockImplementation(() => new Promise(() => {}))
    await client.invalidateQueries({ queryKey: ['agents-catalog', 'global'] })
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'onc' } })
    await waitFor(() => expect(hasRow('accountant')).toBe(false))
    expect(hasRow('oncall')).toBe(false)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(navigate).not.toHaveBeenCalled()
  })

  it('still opens the crewmate a POINTER pressed inside that same window', async () => {
    // A pointer names its own target: the reader pressed the row they could read, and it
    // opens the mate it says. Only Enter names an INDEX, and only an index means a
    // different crewmate once the rows move under it — so the guard is on the keyboard
    // path alone. Guarding the shared activation instead made a visible row not respond.
    await openMatesView()
    await waitFor(() => expect(rowCount()).toBe(3))
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'onc' } })
    fireEvent.mouseDown(rowByText('accountant'))
    expect(navigate).toHaveBeenCalledWith('/members?member=accountant')
  })

  it('opens the SELECTED row on Enter after arrowing, not the first one', async () => {
    await openMatesView()
    await waitFor(() => expect(hasRow('oncall')).toBe(true))
    const input = screen.getByRole('combobox')
    // Listing order is alphabetical: accountant, oncall, reviewer.
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(navigate).toHaveBeenCalledWith('/members?member=oncall')
  })
})
