import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { localizedSettingLabel } from '../../components/commandPalette/settingsSearchCore'
import { settingsSubtitle, settingsTabLabel } from '../../components/commandPalette/settingsTabLabel'
import CommandBarOverlay from './CommandBarOverlay'

/**
 * Row-level behaviour of the launcher: what a row DOES when activated, and what the
 * user is told when it does not work. `rootIndex.test.ts` covers ranking; these are
 * the assertions that need the component rendered.
 */

const dispatch = vi.fn()
const navigate = vi.fn()
const newSessionWithToken = vi.fn()
const enterInsertOrNewSession = vi.fn()

/** Live store the overlay reads for the attention section. Mutated per test. */
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
  switchSlot: (key: string) => ({ type: 'switchSlot', key }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({ navigate, enterInsertOrNewSession, newSessionWithToken }),
}))
const sessionSearch = vi.fn(async () => [] as unknown[])
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: sessionSearch }),
}))
const recentsSearch = vi.fn(async () => [] as unknown[])
// `sessionStatus` is kept REAL: it is the pure classifier that decides which slots
// owe the user something, and a stubbed one would let the attention section pass
// its tests while disagreeing with every other surface about what "needs input"
// means.
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<typeof import('../../components/commandPalette/providers/recentsProvider')>()),
  useRecentsProvider: () => ({ search: recentsSearch }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
const cycleTheme = vi.fn()
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: cycleTheme }) }))

/** Resolve the promise `createSlot` dispatch is expected to produce. */
const resolvingDispatch = () => dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })
const rejectingDispatch = () =>
  dispatch.mockReturnValue({ unwrap: () => Promise.reject(new Error('gateway down')) })

function mount(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return onClose
}

/** Mount with control over the `open` prop, for the dismiss-mid-flight cases. */
function mountControllable(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return {
    onClose,
    unmount: view.unmount,
    rerender: (open: boolean) =>
      view.rerender(
        <QueryClientProvider client={client}>
          <CommandBarOverlay open={open} onClose={onClose} />
        </QueryClientProvider>,
      ),
  }
}

const rowByText = (text: string) =>
  screen.getByText(text).closest('[role="option"]') as HTMLElement

/** The title the overlay renders for a settings entry — the shared resolver
 *  (localized + fan-out suffix), same as the component. */
const renderedTitle = (entry: (typeof SETTINGS_REGISTRY)[number]) =>
  localizedSettingLabel(entry)

const channelsEntry = SETTINGS_REGISTRY.find(e => e.tab === 'channels')!

describe('CommandBarOverlay rows', () => {
  beforeEach(() => {
    dispatch.mockReset()
    navigate.mockReset()
    sessionSearch.mockReset()
    sessionSearch.mockResolvedValue([])
    recentsSearch.mockReset()
    recentsSearch.mockResolvedValue([])
    enterInsertOrNewSession.mockReset()
    newSessionWithToken.mockReset()
    storeState.dashboard = { slots: [], unreadSlots: [] }
    storeState.chat = { slotStatusDetail: {}, activeSlot: null }
    localStorage.clear()
  })

  it('creates a session and closes when New Session succeeds', async () => {
    resolvingDispatch()
    const onClose = mount()
    fireEvent.mouseDown(rowByText('New Session'))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: undefined })
  })

  it('keeps the bar open and says so when New Session fails', async () => {
    // Closing on failure is the defect this pins: once the bar is gone, a session
    // that was never created is indistinguishable from one that was.
    rejectingDispatch()
    const onClose = mount()
    fireEvent.mouseDown(rowByText('New Session'))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(onClose).not.toHaveBeenCalled()
    // The copy has to name the row and the recovery: keeping the bar open so Enter
    // retries is invisible otherwise.
    expect(screen.getByRole('alert').textContent).toMatch(/New Session/)
    expect(screen.getByRole('alert').textContent).toMatch(/Enter/)
  })

  it('navigates to the new session so a success is visible off the chat page', async () => {
    // Created from Settings or Task Runner without this, the session lands off-screen
    // and the success reads as a failure -- the user runs it again into a duplicate.
    resolvingDispatch()
    const onClose = mount()
    fireEvent.mouseDown(rowByText('New Session'))
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat'))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('clears a stale failure once the user types again', async () => {
    rejectingDispatch()
    mount()
    fireEvent.mouseDown(rowByText('New Session'))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'set' } })
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  it('renders settings subtitles that tell same-label rows apart', () => {
    // Two `Speed` selects live in the Voice tab, distinguished in the registry only
    // by their description. A tab-only subtitle renders them identically, which is
    // the shipped defect: the user cannot tell which row they are choosing. The tab
    // name must also be localized, never a raw machine key like `computer-use`.
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'speed' } })
    const dupes = SETTINGS_REGISTRY.filter(e => e.label === 'Speed' && e.tab === 'voice')
    expect(dupes.length).toBe(2)
    const subtitles = dupes.map(e => settingsSubtitle(e))
    expect(new Set(subtitles).size).toBe(2)
    for (const s of subtitles) {
      expect(s).toContain(settingsTabLabel('voice'))
      expect(screen.getByText(s)).toBeTruthy()
    }
    expect(settingsTabLabel('computer-use')).not.toBe('computer-use')
  })

  it('navigates and closes on a settings row', () => {
    const onClose = mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: channelsEntry.label } })
    fireEvent.mouseDown(rowByText(renderedTitle(channelsEntry)))
    expect(navigate).toHaveBeenCalled()
    expect(onClose).toHaveBeenCalled()
  })

  it('Escape leaves a scope before it closes the bar', async () => {
    const onClose = mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    // Inside the scope the chip is present; Escape must return to the root rather
    // than discarding everything the user typed to get here.
    await waitFor(() => expect(screen.getByText('Search Sessions')).toBeTruthy())
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('Backspace on an empty input leaves the scope', async () => {
    // The undocumented twin of the scope chip: the same gesture that deletes a
    // character steps out once there is nothing left to delete.
    const onClose = mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    await waitFor(() => expect(screen.getByText('Search Sessions')).toBeTruthy())
    fireEvent.keyDown(input, { key: 'Backspace' })
    expect(onClose).not.toHaveBeenCalled()
    // Back at the root, the command rows are listed again.
    expect(screen.getByText('New Session')).toBeTruthy()
  })

  it('arrow keys move the selection and wrap at both ends', () => {
    mount()
    const input = screen.getByRole('combobox')
    const selectedId = () => input.getAttribute('aria-activedescendant')
    expect(selectedId()).toBe('command-bar-row-0')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(selectedId()).toBe('command-bar-row-1')
    // Up from the first row wraps to the last rather than sticking, so a user can
    // reach the bottom of the list without knowing how long it is.
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expect(selectedId()).not.toBe('command-bar-row-0')
  })

  it('Enter activates the selected row', async () => {
    resolvingDispatch()
    const onClose = mount()
    const input = screen.getByRole('combobox')
    // Walk to New Session rather than assuming its index: the root order is
    // frecency-ranked, so a hardcoded position would encode today's tie-break.
    const target = screen
      .getAllByRole('option')
      .findIndex(o => o.textContent?.includes('New Session'))
    expect(target).toBeGreaterThanOrEqual(0)
    for (let i = 0; i < target; i++) fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input.getAttribute('aria-activedescendant')).toBe(`command-bar-row-${target}`)
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: undefined }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('carries the typed text into the sessions view on one Enter', async () => {
    // The fallback row is what keeps a content search one keystroke away: whatever
    // the user typed at the root must arrive in the scope, not be retyped.
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'quarterly' } })
    const fallback = await screen.findByText(/Search sessions for/)
    fireEvent.mouseDown(fallback.closest('[role="option"]') as HTMLElement)
    await waitFor(() => expect(screen.getByText('Search Sessions')).toBeTruthy())
    expect((input as HTMLInputElement).value).toBe('quarterly')
  })

  it('runs the work once when Enter is pressed twice before it resolves', async () => {
    // The bar stays open until the promise settles, so a second Enter in that window
    // would create a second session from one intent.
    let release: (v: unknown) => void = () => {}
    dispatch.mockReturnValue({ unwrap: () => new Promise(r => (release = r)) })
    const onClose = mount()
    const input = screen.getByRole('combobox')
    const row = rowByText('New Session')
    fireEvent.mouseDown(row)
    // In flight: the row says so, and a second press is refused.
    await waitFor(() => expect(screen.getByLabelText('Working…')).toBeTruthy())
    fireEvent.mouseDown(row)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(dispatch).toHaveBeenCalledTimes(1)
    release('slot-1')
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('releases the guard after a failure so the user can retry', async () => {
    rejectingDispatch()
    mount()
    const row = rowByText('New Session')
    fireEvent.mouseDown(row)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.queryByLabelText('Working…')).toBeNull()
    fireEvent.mouseDown(rowByText('New Session'))
    expect(dispatch).toHaveBeenCalledTimes(2)
  })

  it('offers Toggle Theme as a command, closing the palette-parity dead end', async () => {
    // The palette this replaces serves a theme action, so a habituated user typing
    // "theme" must not dead-end at the empty state. Matched titles are split across
    // highlight spans, so the row is found by its option's text content.
    const onClose = mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'theme' } })
    const row = screen
      .getAllByRole('option')
      .find(o => o.textContent?.includes('Toggle Theme'))
    expect(row).toBeTruthy()
    fireEvent.mouseDown(row as HTMLElement)
    await waitFor(() => expect(cycleTheme).toHaveBeenCalled())
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('makes the recovery hint an actionable row, not just a statement', async () => {
    // Stating the way back without offering it leaves the user to close, navigate to
    // the App Store, find the app and disable it by hand.
    const onClose = mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzzznomatch' } })
    const hint = await screen.findByText(/disable Command Bar in the App Store/)
    fireEvent.mouseDown(hint.closest('[role="option"]') as HTMLElement)
    expect(navigate).toHaveBeenCalledWith('/apps/detail/command-bar')
    expect(onClose).toHaveBeenCalled()
  })

  it('shows the recovery row only at the dead end, not under every query', async () => {
    // Gating it on "a query exists" put a row about switching the feature off under
    // every successful search, and ArrowUp from the top wrapped onto it.
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'theme' } })
    await waitFor(() =>
      expect(
        screen.getAllByRole('option').some(o => o.textContent?.includes('Toggle Theme')),
      ).toBe(true),
    )
    expect(screen.queryByText(/disable Command Bar in the App Store/)).toBeNull()
    // With nothing matched, the dead end is real and the row appears.
    fireEvent.change(input, { target: { value: 'zzzznomatch' } })
    expect(await screen.findByText(/disable Command Bar in the App Store/)).toBeTruthy()
  })

  it('Escape dismisses from a focusable sibling, not only from the input', async () => {
    // The handler used to live on the input, which was equivalent while the input was
    // the only focusable element. It is not: Tab reaches the scope chip, and Escape
    // pressed there did nothing, so a keyboard user had to Shift+Tab back to dismiss.
    const onClose = mount()
    fireEvent.mouseDown(rowByText('Search Sessions'))
    const chip = await screen.findByRole('button', { name: 'Back to all commands' })
    // In a scope, Escape from the chip pops the scope rather than closing.
    fireEvent.keyDown(chip, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText('New Session')).toBeTruthy())
    // At the root, Escape from anywhere in the dialog closes.
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('keeps the sessions provider inert until its view is entered', () => {
    // The root's request-free promise was guarded only in rootIndex.ts, which cannot
    // see this: `useSessionsProvider` runs its own ['instances'] query, so on a warm
    // install merely opening the root issued listInstances(). The provider is now
    // constructed inert and activated by entering the scope, and the assertion is on
    // the call site because the leak was the construction, not the search.
    const src = readFileSync(
      path.join(__dirname, 'CommandBarOverlay.tsx'),
      'utf-8',
    )
    expect(src).toContain("useSessionsProvider({ active: scope === 'sessions' })")
    expect(src).not.toMatch(/useSessionsProvider\(\{\s*\}\)/)
  })

  it('every shared apps-cache reader fetches through the same api call', () => {
    // The nav-rail response is published into the shared ['apps'] cache from an
    // imperative api.listApps() call that lives far from its readers, so a reader
    // whose queryFn returned a different shape would poison that cache silently
    // rather than fail loudly. What keeps the shapes honest is that the writer and
    // every reader go through the one api function -- so that, not a snapshot of
    // today's field list, is what is pinned here. The overlay's reader also carried
    // an `as Promise<AppNavRecord[]>` assertion, which would have hidden exactly the
    // divergence this guards; it type-checks without one, so it no longer has one.
    const srcRoot = path.join(__dirname, '..', '..')
    const files: string[] = []
    const pending = [srcRoot]
    while (pending.length) {
      const dir = pending.pop()!
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const file = path.join(dir, entry.name)
        if (entry.isDirectory()) pending.push(file)
        else if (/\.tsx?$/.test(entry.name) && readFileSync(file, 'utf-8').includes("queryKey: ['apps']")) files.push(file)
      }
    }
    expect(files.length).toBeGreaterThanOrEqual(3)
    for (const f of files) {
      const body = readFileSync(f, 'utf-8')
      // Invalidation-only call sites hold no queryFn and cannot diverge.
      const readers = body.match(/queryKey: \['apps'\],\s*\n\s*queryFn:[^\n]*/g) ?? []
      for (const reader of readers) {
        expect(reader).toContain('api.listApps()')
        expect(reader).not.toContain(' as Promise')
      }
    }
  })

  it('carries a focus cue on the field only when no row can hold one', () => {
    // `aria-activedescendant` is the cue while rows exist, and it is omitted when
    // there are none -- so the field must paint in exactly that state and stay
    // unpainted otherwise, or the surface is either permanently boxed or, on an empty
    // view, shows a keyboard user no focus at all.
    //
    // What it paints is a NEUTRAL hairline. An accent ring around the one element
    // that is always focused was the loudest thing on a surface whose visual weight
    // is supposed to sit on the selected row.
    const src = readFileSync(
      path.join(__dirname, 'CommandBarOverlay.tsx'),
      'utf-8',
    )
    expect(src).toContain("rowCount === 0 ? ' focus-visible:ring-1 focus-visible:ring-border-strong' : ''")
    expect(src).not.toContain('focus-visible:ring-accent/40\' : \'\'')
    // Unconditional forms are what produced the permanent box.
    expect(src).not.toMatch(/placeholder:text-muted focus-visible:ring/)
    expect(src).not.toMatch(/placeholder:text-muted focus-visible:bg/)
  })

  it('labels every row with what activating it produces', () => {
    // Groups always render as contiguous blocks under their own header, so this is
    // not about groups interleaving. The label earns its place because the only
    // other per-row kind signal is the group icon, which reads only to someone who
    // already knows it. `view` is called out separately from its group because it
    // opens a surface inside the bar instead of acting and closing -- the one value
    // the icon cannot convey, and the difference the reader acts on.
    mount()
    expect(rowByText('New Session').textContent).toContain('Command')
    expect(rowByText('Search Sessions').textContent).toContain('View')
    expect(rowByText('Search Sessions').textContent).not.toContain('Command')
    expect(rowByText('Toggle Theme').textContent).toContain('Command')
    // The arrow needs a slot on EVERY row: rendered inline it pushed a `view` row's
    // label left by its own width and the labels stopped sharing a right edge.
    const src = readFileSync(path.join(__dirname, 'CommandBarOverlay.tsx'), 'utf-8')
    expect(src).toContain('shrink-0 w-[13px] flex justify-end')
  })

  it('reports a failed session search as a row, not a paragraph with a button', async () => {
    // A rejected search leaves `data` undefined, which by row count alone looks
    // identical to an empty result -- so the empty copy would tell the user their
    // session does not exist. That is the one state that lies.
    //
    // The retry is a ROW because the keyboard path that reaches this state has
    // selection inside the listbox: as a bare <button> in the empty-state paragraph
    // it could only be reached by Tabbing out of the list, and Enter -- the key the
    // user already has their finger on -- did nothing.
    sessionSearch.mockRejectedValue(new Error('gateway down'))
    mount()
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'quarterly' } })
    const failure = await screen.findByText('Search failed')
    const row = failure.closest('[role="option"]') as HTMLElement
    expect(row).toBeTruthy()
    expect(screen.queryByText('No sessions match')).toBeNull()
    // And Enter on it is what re-runs the search.
    const before = sessionSearch.mock.calls.length
    fireEvent.mouseDown(row)
    await waitFor(() => expect(sessionSearch.mock.calls.length).toBeGreaterThan(before))
  })

  it('opens the sessions view on its recents listing, not on an empty screen', async () => {
    // Entering a view used to land on a centred "keep typing" sentence: no rows, so no
    // selection, so nothing Enter could do -- and that rowless state is the only
    // reason the input needs a focus box at all.
    recentsSearch.mockResolvedValue([
      {
        id: 'recents:cur:slot-1',
        providerId: 'recents',
        title: 'Quarterly planning',
        icon: null,
        score: 0,
        indices: [],
        groupLabel: 'Current',
        timestamp: '09:46',
        onActivate: vi.fn(),
      },
    ])
    mount()
    fireEvent.mouseDown(rowByText('Search Sessions'))
    expect(await screen.findByText('Quarterly planning')).toBeTruthy()
    // The listing is not a search: the sessions engine was never asked.
    expect(sessionSearch).not.toHaveBeenCalled()
    // A row exists, so the cue rides the active option rather than the field.
    expect(screen.getByRole('combobox').getAttribute('aria-activedescendant')).toBe(
      'command-bar-row-0',
    )
    // Where it lives and when it was last touched are what tell two similarly-titled
    // conversations apart.
    expect(rowByText('Quarterly planning').textContent).toContain('09:46')
  })

  it('offers the listing when a search matches nothing, instead of bottoming out', async () => {
    sessionSearch.mockResolvedValue([])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'zzzznomatch' } })
    // One row carrying both halves: the search found nothing, and the listing is one
    // Enter away.
    const row = await screen.findByText(/show recent instead/)
    fireEvent.mouseDown(row.closest('[role="option"]') as HTMLElement)
    await waitFor(() => expect((input as HTMLInputElement).value).toBe(''))
  })

  it('names what Enter will do for the selected row', () => {
    // The row carried its TYPE ("Command", "View") while the verb -- run it, open it,
    // step into it -- was left to be inferred from having pressed Enter before.
    mount()
    const input = screen.getByRole('combobox')
    const footer = () =>
      (document.querySelector('[role="dialog"]') as HTMLElement).textContent ?? ''
    const selectRow = (title: string) => {
      const target = screen.getAllByRole('option').findIndex(o => o.textContent?.includes(title))
      expect(target).toBeGreaterThanOrEqual(0)
      fireEvent.mouseEnter(screen.getAllByRole('option')[target])
    }
    selectRow('New Session')
    expect(footer()).toContain('Run')
    // A `view` row does not act and close, and the verb has to say so.
    selectRow('Search Sessions')
    expect(footer()).toContain('Open View')
    expect(input.getAttribute('aria-activedescendant')).toBeTruthy()
  })

  it('shows the hidden alias that matched so no listed row is unexplained', () => {
    // `blank` is a New Session keyword: it appears in neither the title nor the
    // subtitle, so before this the row rendered with no highlight anywhere and the
    // list answered "these matched" with a row that visibly did not. Typing `theme`
    // produced a screenful of exactly that.
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'blank' } })
    const row = screen.getAllByRole('option').find(o => o.textContent?.includes('New Session'))
    expect(row).toBeTruthy()
    expect(row?.textContent).toContain('blank')
  })

  it('gives each command its own glyph instead of one shared group outline', () => {
    // Filed by group alone every row in a group renders the same outline, so the icon
    // column carries no more information than the group header above it.
    mount()
    const glyph = (title: string) => {
      const svg = rowByText(title).querySelector('svg')
      return Array.from(svg?.classList ?? []).find(c => c.startsWith('lucide-') && c !== 'lucide-inline')
    }
    const glyphs = [glyph('New Session'), glyph('Toggle Theme'), glyph('Search Sessions')]
    for (const g of glyphs) expect(g).toBeTruthy()
    expect(new Set(glyphs).size).toBe(3)
    // App rows resolve through the SAME chain the rail and the palette use; a second
    // copy of it is how one surface silently falls through to the generic outline.
    const src = readFileSync(path.join(__dirname, 'CommandBarOverlay.tsx'), 'utf-8')
    expect(src).toContain('icon: appIcon(target)')
  })

  it('keeps the recents provider inert until the sessions view is entered', () => {
    // Same leak class as the sessions provider: a listing engine that fetches on
    // construction would put a request behind merely opening the root, which is the
    // one thing this surface promises not to do.
    const src = readFileSync(path.join(__dirname, 'CommandBarOverlay.tsx'), 'utf-8')
    expect(src).toContain('enabled: listingArmed')
    expect(src).toMatch(/const listingArmed = scope === 'sessions' && !searchArmed/)
  })

  it('leads the root with the sessions that owe the user something', () => {
    // The launcher's own object: a session holding an approval is the most
    // time-sensitive thing this product has, and it had no presence here at all --
    // it lived one screen in, behind a row the user had to know to enter.
    storeState.dashboard.slots = [
      { key: 'slot-a', title: 'Deploy the pricing service', pending_approval: true, messages: 4 },
      { key: 'slot-b', title: 'Draft the launch email', needs_input: true, messages: 2 },
    ]
    mount()
    const rows = screen.getAllByRole('option')
    // FIRST, ahead of the commands: ordering is the claim, so it is asserted by
    // position rather than by mere presence.
    expect(rows[0].textContent).toContain('Deploy the pricing service')
    expect(rows[0].textContent).toContain('Approve')
    expect(rows[1].textContent).toContain('Answer')
    expect(screen.getByText('Needs You')).toBeTruthy()
    // The column carries live state INSTEAD of a static kind word.
    expect(rows[0].textContent).not.toContain('Command')
    // Activating one switches to it, the same way every other surface opens a session.
    fireEvent.mouseDown(rows[0])
    expect(dispatch).toHaveBeenCalledWith({ type: 'switchSlot', key: 'slot-a' })
  })

  it('shows no attention section when nothing is waiting on the user', () => {
    // A section that is always present is a section the user learns to skip; the whole
    // value of this one is that its presence means something. A RUNNING session is not
    // waiting on anyone, so it must not be lifted here either.
    storeState.dashboard.slots = [
      { key: 'slot-c', title: 'Refactor the parser', running: true, messages: 9 },
      { key: 'slot-d', title: 'Idle thread', messages: 3 },
    ]
    mount()
    expect(screen.queryByText('Needs You')).toBeNull()
    expect(screen.queryByText('Refactor the parser')).toBeNull()
    // The commands are back at the top where they were.
    expect(screen.getAllByRole('option')[0].textContent).toMatch(/New Session|Search Sessions|Toggle Theme/)
  })

  it('always gives the typed text a way to reach an agent', async () => {
    // Every other surface in this product ends in saying something to one, so this is
    // the general case rather than a corner-of-the-screen fallback for failed search.
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'why did the deploy stall' } })
    const row = screen.getByText(/Ask the agent/).closest('[role="option"]') as HTMLElement
    expect(row).toBeTruthy()
    // It states what it will send, rather than advertising a feature.
    expect(row.textContent).toContain('why did the deploy stall')
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    fireEvent.mouseDown(row)
    // A NEW session, never the active chat's composer: ChatPage consumes
    // `pendingInput` by REPLACING the slot's draft and persisting it, so inserting
    // here would silently destroy a half-written message the user had not sent.
    // Created WITHOUT activating, so nothing has focus until the claim is checked.
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } }))
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'switchSlot', key: 'slot-new' }))
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'setPendingInput', text: 'why did the deploy stall' }))
    expect(enterInsertOrNewSession).not.toHaveBeenCalled()
  })

  it('keeps the question recoverable when the session cannot be created', async () => {
    // The row carries a whole sentence the user composed. Fired and forgotten, a
    // gateway that refuses the create left the bar closing on nothing with the
    // question gone -- so it closes only once the session exists.
    rejectingDispatch()
    const onClose = mount()
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: 'why did the deploy stall' } })
    fireEvent.mouseDown(screen.getByText(/Ask the agent/).closest('[role="option"]') as HTMLElement)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(onClose).not.toHaveBeenCalled()
    // And the text is still there to retry with.
    expect((input as HTMLInputElement).value).toBe('why did the deploy stall')
  })

  it('lets the accessories yield their space to the row title', () => {
    // The title is what identifies a row; the status detail is a bonus. Held rigid,
    // a long tool name collapsed the title on a 320px viewport, inverting that. Only
    // the pill and the dot stay unshrinkable -- they are the signal and a few pixels
    // wide -- and the detail drops outright below the `sm` breakpoint.
    const src = readFileSync(path.join(__dirname, 'CommandBarOverlay.tsx'), 'utf-8')
    const accessory = src.slice(src.indexOf('function statusAccessory'))
    const body = accessory.slice(0, accessory.indexOf('\n}\n'))
    expect(body).not.toMatch(/shrink-0 flex items-center/)
    expect(body).toContain('hidden sm:inline')
    // The dot and the pill keep theirs.
    expect(body).toMatch(/shrink-0 w-1\.5 h-1\.5/)
    expect(body).toMatch(/shrink-0 px-1\.5 rounded/)
    // The textual accessories on the other row kinds shrink for the same reason.
    expect(src).not.toMatch(/shrink-0 max-w-\[140px\]/)
    expect(src).not.toMatch(/shrink-0 max-w-\[180px\]/)
  })

  it('renders a session row live state in place of its folder and clock', async () => {
    // The old palette rendered this and the rewrite dropped it: the provider computes
    // an approval pill, a pulsing "Thinking…" and the running tool's name, and the row
    // was showing a folder and a timestamp instead.
    recentsSearch.mockResolvedValue([
      {
        id: 'recents:cur:slot-e',
        providerId: 'recents',
        title: 'Refactor the parser',
        icon: null,
        score: 0,
        indices: [],
        groupLabel: 'Current',
        folder: 'Backend',
        timestamp: '09:46',
        statusStyle: 'dot',
        statusColorVar: '--accent',
        statusPulse: true,
        statusLabel: 'Thinking…',
        statusDetail: 'Reading files',
        onActivate: vi.fn(),
      },
    ])
    mount()
    fireEvent.mouseDown(rowByText('Search Sessions'))
    const row = (await screen.findByText('Refactor the parser')).closest('[role="option"]') as HTMLElement
    expect(row.textContent).toContain('Thinking…')
    expect(row.textContent).toContain('Reading files')
    // Live state outranks the static metadata for the same column.
    expect(row.textContent).not.toContain('09:46')
  })

  it('writes nothing when the bar is dismissed while the ask is still creating', async () => {
    // Dismiss during a slow create and this activation no longer owns the dashboard:
    // seeding then would replace and persist the draft of whatever session the user
    // moved on to. The extra empty session is the cheaper outcome, and it is the same
    // trade ChatPage makes with its own ownsLifecycle() guard.
    let release: (v: unknown) => void = () => {}
    dispatch.mockReturnValue({ unwrap: () => new Promise(r => (release = r)) })
    const { rerender, onClose } = mountControllable()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'why did the deploy stall' } })
    fireEvent.mouseDown(screen.getByText(/Ask the agent/).closest('[role="option"]') as HTMLElement)
    await waitFor(() => expect(screen.getByLabelText('Working…')).toBeTruthy())
    // The user walks away before the gateway answers.
    rerender(false)
    release({ key: 'slot-9' })
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } }))
    // No activation, no seed, no navigation -- the abandoned create is allowed to leak
    // a slot, but it must not touch shared state.
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'switchSlot', key: 'slot-9' })
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: 'why did the deploy stall' })
    expect(navigate).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('writes nothing when the overlay UNMOUNTS while the ask is still creating', async () => {
    // Distinct from the prop edge above: an unmount never sets `open` false, the
    // effect keyed on that prop never runs again, and the in-flight callback still
    // holds the ref OBJECT -- so without a teardown revocation it compares equal and
    // passes its own guard. This is the hole a review found after the prop edge was
    // already covered.
    let release: (v: unknown) => void = () => {}
    dispatch.mockReturnValue({ unwrap: () => new Promise(r => (release = r)) })
    const { unmount } = mountControllable()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'why did the deploy stall' } })
    fireEvent.mouseDown(screen.getByText(/Ask the agent/).closest('[role="option"]') as HTMLElement)
    await waitFor(() => expect(screen.getByLabelText('Working…')).toBeTruthy())
    unmount()
    release({ key: 'slot-9' })
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } }))
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'switchSlot', key: 'slot-9' })
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: 'why did the deploy stall' })
    expect(navigate).not.toHaveBeenCalled()
  })

  it('refuses a second ask while the first is still switching sessions', async () => {
    // The guard used to be released before the switch await, so a second Enter during
    // a slow slot fetch started a second create -- two blank sessions from one intent.
    let releaseSwitch: (v: unknown) => void = () => {}
    let call = 0
    dispatch.mockImplementation((action: { type?: string }) => {
      call += 1
      if (action?.type === 'switchSlot') return { unwrap: () => new Promise(r => (releaseSwitch = r)) }
      return { unwrap: () => Promise.resolve({ key: 'slot-new' }) }
    })
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'why did the deploy stall' } })
    const askRow = () => screen.getByText(/Ask the agent/).closest('[role="option"]') as HTMLElement
    fireEvent.mouseDown(askRow())
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'switchSlot', key: 'slot-new' }))
    const afterFirst = call
    // Still switching: the row refuses.
    fireEvent.mouseDown(askRow())
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    expect(call).toBe(afterFirst)
    releaseSwitch(undefined)
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({ type: 'setPendingInput', text: 'why did the deploy stall' }),
    )
    // Exactly one create for one intent.
    expect(dispatch.mock.calls.filter(c => c[0]?.type === 'createSlot').length).toBe(1)
  })

  it('sizes the panel and its rows for the content they actually carry', () => {
    // At `max-w-xl` (576px) a settings row truncated its title AND its tab subtitle on
    // a 1440 screen, which is the one thing that column exists to prevent. The
    // entrance is the shell's standard scale-in because a launcher that hard-cuts into
    // place reads as a repaint rather than as a surface arriving.
    const src = readFileSync(path.join(__dirname, 'CommandBarOverlay.tsx'), 'utf-8')
    expect(src).toContain('max-w-[680px]')
    expect(src).not.toContain('max-w-xl')
    expect(src).toContain('animate-scale-in')
    expect(src).toMatch(/px-3 py-2 cursor-pointer text-\[13px\]/)
  })
})
