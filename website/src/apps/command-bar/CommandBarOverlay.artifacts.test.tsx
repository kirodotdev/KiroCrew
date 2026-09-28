import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * The Command Bar's artifacts view.
 *
 * The bar is the DEFAULT Cmd+K surface, so the palette's Artifacts tab is
 * unreachable for anyone who has not disabled the app — a saved widget had no
 * keyboard route at all. These tests pin the route on the surface the user opens,
 * and the two properties the launcher's whole design rests on:
 *
 *  - the ROOT reaches no artifact endpoint; entering the view is what fetches,
 *  - the view searches by NAME only, so the server reads metadata rather than
 *    every artifact's body (content search is the next step).
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

/**
 * The journal lookup, spied.
 *
 * The failure notice shows a friendly sentence, so the rendered string is no longer
 * the journal's lookup key. That makes "the structured context still resolves" an
 * invariant nothing on screen can prove, which is what this spy is for. The rest of
 * the module is kept: the render path records errors through it.
 */
const findReportSpy = vi.fn(() => undefined)
vi.mock('../../utils/errorReport', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/errorReport')>()),
  findReport: (...args: unknown[]) => findReportSpy(...(args as [])),
}))

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

/**
 * Every network call the overlay could make, so a request is observable — and so
 * the arguments of the artifact one can be asserted, which is where the name-only
 * contract actually lives.
 */
const listApps = vi.fn(async () => [])
const chatFolders = vi.fn(async () => [])
const artifacts = vi.fn(async (_filters?: Record<string, unknown>) => ({ artifacts: [] }))
vi.mock('../../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...(a as [])),
    chatFolders: (...a: unknown[]) => chatFolders(...(a as [])),
    artifacts: (...a: unknown[]) => artifacts(...(a as [])),
  },
}))

/** Two artifacts whose names differ, so a filtered result is distinguishable. */
const ARTIFACTS = [
  {
    slug: 'q3-revenue-chart',
    name: 'Q3 Revenue Chart',
    kind: 'widget',
    description: 'Bar chart of quarterly revenue',
    tags: [],
    version: 3,
    updated_at: '2026-09-01T00:00:00Z',
  },
  {
    slug: 'onboarding-runbook',
    name: 'Onboarding Runbook',
    kind: 'markdown',
    description: 'How a new hire gets set up',
    tags: [],
    version: 1,
    updated_at: '2026-08-01T00:00:00Z',
  },
]

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
 * The option row whose visible text contains `text`.
 *
 * Matched on the ROW rather than with `getByText` because a matched title renders
 * through `<Highlighted>`, which splits it into one element per matched run — so the
 * name is on screen without being any single text node.
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

/** Enter the artifacts view the way a user does: activate its root row. */
const enterView = async () => {
  await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
  fireEvent.mouseDown(rowByText('Search Artifacts'))
  // The chip naming the view is what proves the scope was entered.
  await waitFor(() => expect(screen.getByRole('button', { name: /Back to all commands/ })).toBeTruthy())
}

beforeEach(() => {
  vi.clearAllMocks()
  artifacts.mockResolvedValue({ artifacts: [] })
  dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })
  storeState.dashboard = { slots: [], unreadSlots: [] }
  storeState.chat = { slotStatusDetail: {}, activeSlot: null }
  localStorage.clear()
})

describe('command bar — artifacts view', () => {
  it('offers the view as a command row, named as a view rather than a command', async () => {
    mount()
    await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
    // A `view` row promises to open a surface INSIDE the bar, which is a different
    // promise from a row that acts and closes.
    expect(rowByText('Search Artifacts').textContent).toContain('View')
    expect(rowByText('Search Artifacts').textContent).not.toContain('Command')
  })

  it('reaches the row by the word the user knows a saved widget by', async () => {
    mount()
    type('widget')
    // The title has no "widget" in it; the alias is what carries the row.
    await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
  })

  it('issues NO artifact request from the root, which is the launcher invariant', async () => {
    mount()
    type('artifact')
    await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
    expect(artifacts).not.toHaveBeenCalled()
  })

  it('lists the newest artifacts on entering, before anything is typed', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(hasRow('Q3 Revenue Chart')).toBe(true))
    expect(hasRow('Onboarding Runbook')).toBe(true)
    // Entering is the activation event: the listing asks for no `q` at all.
    expect(artifacts).toHaveBeenCalledWith({ q: undefined })
  })

  it('searches by NAME only — no content scan and no snippet', async () => {
    artifacts.mockResolvedValue({ artifacts: [ARTIFACTS[0]] })
    mount()
    await enterView()
    type('revenue')
    await waitFor(() => expect(artifacts).toHaveBeenCalledWith({ q: 'revenue' }))
    // `snippet=1` alone makes the server read every listed artifact's body, so
    // leaving it on would pay for the content scan this stage does not do. Asserted
    // on the ABSENT keys because that is what the cost depends on.
    for (const call of artifacts.mock.calls) {
      expect(call[0]).not.toHaveProperty('snippet')
      expect(call[0]).not.toHaveProperty('contentMatch')
    }
  })

  it('holds the request until the query is long enough to be worth one', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(artifacts).toHaveBeenCalledTimes(1))
    type('r')
    // Waited PAST the field's debounce on purpose. Asserting as soon as the listing
    // rows are on screen proves nothing: they are the rows the first request already
    // returned, so the count is still 1 simply because the debounced request has not
    // had time to fire. A mutation removing the threshold survived that version of
    // this test; it only fails once the window the request would arrive in has closed.
    await new Promise(resolve => setTimeout(resolve, 400))
    // One character would return most of the corpus — which the listing already
    // shows — so it stays on the listing rather than buying a second scan.
    expect(artifacts).toHaveBeenCalledTimes(1)
    expect(hasRow('Q3 Revenue Chart')).toBe(true)
  })

  it('opens the artifact and closes the bar when its row is activated', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    const { onClose } = mount()
    await enterView()
    await waitFor(() => expect(hasRow('Onboarding Runbook')).toBe(true))
    fireEvent.mouseDown(rowByText('Onboarding Runbook'))
    expect(navigate).toHaveBeenCalledWith('/artifacts/onboarding-runbook')
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('names the Enter action "Open Artifact", not "Open Session"', async () => {
    // The row shape is shared with the sessions view, so the footer is the only
    // thing that says which of the two this Enter does.
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(screen.queryByText('Open Artifact')).not.toBeNull())
    expect(screen.queryByText('Open Session')).toBeNull()
  })

  it('offers the listing back when a name matches nothing', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    artifacts.mockResolvedValue({ artifacts: [] })
    type('nothing-by-this-name')
    // A dead end with no selectable row is the state this avoids.
    await waitFor(() => expect(hasRow('show recent')).toBe(true))
    expect(rowByText('show recent').textContent).toContain('nothing-by-this-name')
  })

  it('says "No artifacts yet" on an empty corpus, not that a match failed', async () => {
    // The gap a UX review found and these tests had missed: with nothing saved and
    // nothing typed, the view reported a failed match against a query the user
    // never entered. It is the FIRST thing a new user sees here, every time, until
    // they save something.
    artifacts.mockResolvedValue({ artifacts: [] })
    mount()
    await enterView()
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/No artifacts yet/))
    // And it must not read as a failed search, which is the whole defect.
    expect(screen.getByRole('status').textContent).not.toMatch(/match/i)
    // No row to select, so the copy has to carry the next step itself.
    expect(screen.getByRole('status').textContent).toMatch(/save/i)
    // And it names the SAME three things the two row subtitles name. Listing only two
    // of them left a reader unable to tell whether a chart, a widget and an artifact
    // were one thing or three, which is the one question this screen exists to answer.
    const empty = screen.getByRole('status').textContent || ''
    for (const thing of ['chart', 'document', 'widget']) {
      expect(empty.toLowerCase()).toContain(thing)
    }
  })

  it('heads the listing "Recent" so the list says what it is', async () => {
    // Every group in the root announces itself (COMMANDS, SETTINGS). The listing
    // was unexplained rows: a reader can guess they are the recent ones, and
    // nothing on screen said so.
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(hasRow('Q3 Revenue Chart')).toBe(true))
    expect(screen.getByText('Recent')).toBeTruthy()
  })

  it('drops the header once a name narrows the list', async () => {
    // What those rows are is the word the reader just typed, so a header there
    // would be restating the query back at them.
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    await waitFor(() => expect(screen.getByText('Recent')).toBeTruthy())
    artifacts.mockResolvedValue({ artifacts: [ARTIFACTS[0]] })
    type('revenue')
    // Waited for the list to actually NARROW, and for BOTH facts at once. Waiting
    // on the chart alone passes on the stale listing (it is in both the listing and
    // the result) while the debounced request is still pending. Waiting on the
    // runbook alone passes mid-flight, when the new query key has no data yet and
    // the list is briefly empty. Only both together name the settled state.
    await waitFor(() => {
      expect(hasRow('Q3 Revenue Chart')).toBe(true)
      expect(hasRow('Onboarding Runbook')).toBe(false)
    })
    expect(screen.queryByText('Recent')).toBeNull()
  })

  it('names what an artifact IS on the row, and what the row lets you do', async () => {
    // A first-time reader called them "whatever they are", so the subtitle names the
    // things, which is what the settings rows in this same list do. It also leads with
    // the OUTCOME: describing only the corpus made it overlap the fallback row's
    // subtitle, and a reader could not tell the two rows apart.
    mount()
    await waitFor(() => expect(hasRow('Search Artifacts')).toBe(true))
    const sub = rowByText('Search Artifacts').textContent || ''
    expect(sub).toMatch(/charts, documents and widgets/i)
    expect(sub).toMatch(/Browse and open/)
  })

  it('renders a failed search through ErrorNotice and keeps Retry keyboard-reachable', async () => {
    artifacts.mockRejectedValue(new Error('gateway down'))
    mount()
    await enterView()

    const failure = await screen.findByRole('alert')
    // The notice names the corpus and says what to do. The two scopes' failure
    // states were otherwise separated only by the breadcrumb chip.
    expect(failure.textContent).toContain('Artifact search failed. Try again.')
    // The raw rejection is NOT user-facing. A reader shown "gateway unavailable"
    // had no idea what it meant and tied it to the "Run a local gateway" setting
    // they "would not dare touch" -- a cause they cannot read beside a fix that
    // frightens them. It stays in the network panel, not in the copy.
    expect(failure.textContent).not.toContain('gateway down')
    // ErrorNotice stays outside the option: nesting its possible hand-off button
    // in a listbox option would give the option two competing interactions.
    expect(failure.closest('[role="option"]')).toBeNull()
    // The query is still only local combobox state, so navigating to chat would
    // discard it. The no-hand-off decision must remain visible in the rendered shape.
    expect(failure.querySelector('button')).toBeNull()

    // A failed search is not an empty one. Retry remains on the Arrow/Enter path
    // that reached the failure instead of becoming a bare button outside the list.
    const retryRow = rowByText('Retry')
    expect(retryRow.textContent).not.toContain('gateway down')
    const before = artifacts.mock.calls.length
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(artifacts.mock.calls.length).toBeGreaterThan(before))
  })

  it('says how many matches the row cap is hiding, outside the listbox', async () => {
    // The cap was the one silent state left in this view: a name matching more
    // artifacts than fit drew a full list and said nothing about the rest, which is
    // the same dead end the empty and no-match states were fixed to avoid.
    const many = Array.from({ length: 17 }, (_, i) => ({
      ...ARTIFACTS[0],
      slug: `report-${i}`,
      name: `Report ${i}`,
    }))
    artifacts.mockResolvedValue({ artifacts: many })
    mount()
    await enterView()
    await waitFor(() => expect(hasRow('Report 0')).toBe(true))

    // 17 matches, 12 rows: the remainder is real, because the endpoint returns
    // every match rather than a page.
    const hint = await screen.findByText('+5 more — keep typing to narrow')
    // A fact about the list, not a row in it. As an option it would be the one
    // row Enter did nothing to.
    expect(hint.closest('[role="option"]')).toBeNull()
    expect(screen.getAllByRole('option').length).toBe(12)
  })

  it('resolves the failure report from the rejection, not from the sentence on screen', async () => {
    // The visible copy is friendly now, so `ErrorNotice`'s message-match lookup
    // would resolve nothing. The structured context (endpoint, status, backend
    // code) is instead resolved from the raw rejection and handed over directly.
    // Nothing on screen can show this, which is why it is asserted here.
    findReportSpy.mockClear()
    artifacts.mockRejectedValue(new Error('gateway down'))
    mount()
    await enterView()
    await screen.findByRole('alert')

    const args = findReportSpy.mock.calls.map(c => c[0])
    expect(args).toContain('gateway down')
    // Never the friendly sentence: that string is in no journal and would resolve
    // to undefined, silently dropping the context this call exists to keep.
    expect(args).not.toContain('Artifact search failed. Try again.')
  })

  it('keeps its artifact caches under the ["artifacts"] invalidation prefix', async () => {
    // Artifact mutations invalidate `['artifacts']`, and React Query prefix-matches
    // from the FIRST element, so a key starting with anything else is never reached:
    // saving an artifact then reopening the bar would serve the pre-save list for a
    // full staleTime. WidgetFrame.tsx documents the same trap.
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    const { client } = mount()
    await enterView()
    type('revenue')
    await waitFor(() => expect(hasRow('Q3 Revenue Chart')).toBe(true))

    const artifactKeys = client.getQueryCache().getAll()
      .map(q => q.queryKey as unknown[])
      .filter(k => JSON.stringify(k).toLowerCase().includes('artifact'))
    expect(artifactKeys.length).toBeGreaterThan(0)
    for (const key of artifactKeys) expect(key[0]).toBe('artifacts')
  })

  it('leaves the view on Backspace in an empty field, back to the launcher', async () => {
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    await enterView()
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Backspace' })
    await waitFor(() => expect(hasRow('New Session')).toBe(true))
  })

  it('stops naming artifacts among the corpora the bar cannot reach', async () => {
    // The recovery row exists to name what is NOT searchable here. Artifacts have a
    // view of their own now, so listing them would send the reader to disable the app
    // to reach something one Enter away.
    mount()
    type('zzzz-matches-nothing')
    await waitFor(() => expect(hasRow('turn full search back on')).toBe(true))
    const hint = rowByText('turn full search back on').textContent || ''
    expect(hint).toContain('Knowledge, skills and prompts')
    expect(hint).not.toContain('artifact')
  })

  it('names what the recovery row opens instead of what to switch off', async () => {
    // Two readings of this row failed in sequence. Truncated at "...disable Command
    // Bar" a test reader "would not dare click" it; front-loaded and given a second
    // line, the surviving instruction still read as a switch that would turn off the
    // search box they were using -- "I don't know how to get it back". Activating the
    // row only opens `/apps/detail/command-bar`. So the clause names that, and names
    // what it is for, and the word that described the end state is gone from the copy.
    mount()
    type('zzzz-matches-nothing')
    await waitFor(() => expect(hasRow('turn full search back on')).toBe(true))
    const row = rowByText('turn full search back on')
    const hint = (row.textContent || '').trim()
    // What it is comes before what to do about it.
    expect(hint.indexOf('Knowledge')).toBe(0)
    expect(hint.indexOf('Knowledge')).toBeLessThan(hint.indexOf('Command Bar'))
    // The row describes opening a page, not disabling the feature in use, and not a
    // store: a reader read "App Store" as something that would install or charge.
    expect(hint).not.toContain('disable')
    expect(hint).not.toContain('App Store')
    expect(hint).toContain('turn full search back on')
    // And the sentence is allowed to finish rather than being clipped to one line.
    const title = row.querySelector('span.line-clamp-2')
    expect(title).toBeTruthy()
    expect(title?.textContent || '').toContain('turn full search back on')
    expect(title?.className || '').not.toContain('truncate')
  })

  it('glosses the fallback row without repeating the view row word for word', async () => {
    // A reader reaching this row typed a NAME, so they never had to read the view
    // row's subtitle to get here -- "artifact" can still be our word and not theirs,
    // and the row needs a gloss of its own. But it cannot be the SAME gloss: when the
    // typed word also matches the command, both rows are on screen, and with one
    // shared subtitle a reader "can't tell how their results would differ". The two
    // now answer different questions: the view row says what you can DO there (browse
    // and open), this row says what it does with the text already typed.
    mount()
    type('widget')
    await waitFor(() => expect(hasRow('Search artifacts for')).toBe(true))
    const fallback = rowByText('Search artifacts for').textContent || ''
    expect(fallback).toContain('Finds saved charts, documents and widgets matching that text.')
    // Both rows are up, and they do not read the same.
    const command = rowByText('Search Artifacts').textContent || ''
    expect(command).toContain('Browse and open the charts, documents and widgets you saved.')
    expect(fallback).not.toContain('Browse and open')
    expect(command).not.toContain('matching that text')
  })

  it('finds the artifact when its NAME is what was typed at the root', async () => {
    // The journey the fallback row exists for, end to end. The root ranks launcher
    // rows on their own vocabulary, so an artifact's name matches nothing there: a
    // reader who types what they call the thing got a dead end unless they first
    // guessed the word "artifact". One Enter now carries that same text into the
    // view, and the artifact is on screen without it being retyped.
    artifacts.mockResolvedValue({ artifacts: ARTIFACTS })
    mount()
    type('Q3 Revenue')
    await waitFor(() => expect(hasRow('Search artifacts for')).toBe(true))
    fireEvent.mouseDown(rowByText('Search artifacts for'))
    await waitFor(() => expect(hasRow('Q3 Revenue Chart')).toBe(true))
    // Carried, not retyped: the field still holds it and the server was asked for it.
    expect((screen.getByRole('combobox') as HTMLInputElement).value).toBe('Q3 Revenue')
    await waitFor(() =>
      expect(artifacts).toHaveBeenCalledWith(expect.objectContaining({ q: 'Q3 Revenue' })),
    )
  })

  it('names the Enter action "Search Artifacts" on the fallback row', async () => {
    // Two fallback rows sit next to each other and they reach different corpora, so
    // the footer has to name which one this Enter lands in. Read off the footer
    // itself (the element holding the Enter keycap) rather than by text, because
    // "Search Artifacts" is also a root row's label.
    mount()
    type('revenue')
    await waitFor(() => expect(hasRow('Search artifacts for')).toBe(true))
    const footerText = () => screen.getByText('\u21B5').parentElement?.textContent || ''
    const rows = screen.queryAllByRole('option')
    const sessions = rows.findIndex(r => (r.textContent || '').includes('Search sessions for'))
    const artifactsRow = rows.findIndex(r => (r.textContent || '').includes('Search artifacts for'))
    expect(sessions).toBeGreaterThanOrEqual(0)
    expect(artifactsRow).toBe(sessions + 1)
    const input = screen.getByRole('combobox')
    // Selection starts on the first row, so reaching index N takes N presses.
    for (let i = 0; i < sessions; i++) fireEvent.keyDown(input, { key: 'ArrowDown' })
    await waitFor(() => expect(footerText()).toContain('Search Sessions'))
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    await waitFor(() => expect(footerText()).toContain('Search Artifacts'))
    expect(footerText()).not.toContain('Search Sessions')
  })

  it('offers no fallback row until something is typed', async () => {
    // The bare root is a command list, not a search result. A row offering to carry
    // an empty query would search for nothing.
    mount()
    await waitFor(() => expect(hasRow('New Session')).toBe(true))
    expect(hasRow('Search artifacts for')).toBe(false)
  })

  it('still issues no artifact request while the fallback row is only OFFERED', async () => {
    // The row is the cheap half of the design: it advertises the view without
    // reaching the corpus, so typing at the root stays free no matter how long the
    // row has been on screen.
    mount()
    type('revenue')
    await waitFor(() => expect(hasRow('Search artifacts for')).toBe(true))
    expect(artifacts).not.toHaveBeenCalled()
  })
})
