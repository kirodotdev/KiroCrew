import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

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
  // Mirrors the real thunk's `SwitchSlotArg`: a bare key, or an options object whose
  // `keepTargetOnMissing` decides whether a 404 unwinds to the previous slot.
  switchSlot: (arg: string | { key: string; keepTargetOnMissing?: boolean }) =>
    typeof arg === 'string' ? { type: 'switchSlot', key: arg } : { type: 'switchSlot', ...arg },
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
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({
        type: 'switchSlot',
        key: 'slot-new',
        keepTargetOnMissing: true,
      }),
    )
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
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({
        type: 'switchSlot',
        key: 'slot-new',
        keepTargetOnMissing: true,
      }),
    )
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


/**
 * Commands contributed by an installed app — the seam that lets a launcher row live
 * outside this repository.
 *
 * What is worth pinning here is not that a row renders. It is that the host stays in
 * charge of a declaration it did not write: a malformed contribution is skipped
 * rather than taking the Cmd+K gesture down for every app, an argument is checked
 * against the app's own pattern BEFORE a session exists, and a command that sends
 * its prompt shows the reader that prompt first.
 */
describe('CommandBarOverlay contributed commands', () => {
  beforeEach(() => {
    dispatch.mockReset()
    navigate.mockReset()
    sessionSearch.mockReset()
    sessionSearch.mockResolvedValue([])
    recentsSearch.mockReset()
    recentsSearch.mockResolvedValue([])
    enterInsertOrNewSession.mockReset()
    newSessionWithToken.mockReset()
    cycleTheme.mockReset()
    storeState.dashboard = { slots: [], unreadSlots: [] }
    storeState.chat = { slotStatusDetail: {}, activeSlot: null }
    window.localStorage.clear()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  const LINK = 'https://github.com/kirodotdev/KiroCrew/pulls?q=is%3Aopen'

  /** A well-formed argument-taking command, as an app would declare it. */
  const APPROVE_ALL = {
    id: 'approve-all',
    title: 'Approve all PRs',
    subtitle: 'Approve every pull request behind a link',
    icon: 'Check',
    keywords: ['pr', 'lgtm'],
    prompt: 'Approve every pull request behind {argument}. Skip any I authored.',
    autoSend: true,
    argument: {
      placeholder: 'Paste a GitHub link…',
      hint: 'A PR search, a label, or a single pull request.',
      kind: 'url',
      hosts: ['github.com'],
      patternError: 'Not a GitHub link.',
    },
  }

  /** A command that needs nothing: activating it IS the action. */
  const STANDUP = {
    id: 'standup',
    title: 'Write my standup',
    prompt: 'Summarise what I did yesterday.',
  }

  const appWith = (commands: unknown, over: Record<string, unknown> = {}) => ({
    name: 'pr-bulk-ops',
    displayName: 'PR Bulk Ops',
    enabled: true,
    origin: 'registry',
    manifest: { contributes: { commands } },
    ...over,
  })

  /** Mount with the shared `['apps']` cache seeded, which is how the bar reads apps. */
  function mountWithApps(apps: unknown[], onClose = vi.fn()) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], apps)
    render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    return onClose
  }

  const optionByTitle = (re: RegExp) =>
    screen.getAllByRole('option').find(el => re.test(el.textContent ?? '')) as HTMLElement

  function enterCommand(title: RegExp) {
    fireEvent.mouseDown(optionByTitle(title))
    return screen.getByRole('combobox') as HTMLInputElement
  }

  it('renders a command an installed app contributed', () => {
    mountWithApps([appWith([APPROVE_ALL])])
    const row = optionByTitle(/Approve all PRs/)
    expect(row).toBeTruthy()
    // The app's own subtitle, so the row says which app put it there.
    expect(row.textContent).toContain('Approve every pull request behind a link')
  })

  it('contributes nothing while the app is disabled', () => {
    // The enable switch is the reader's control over the whole app; a row that still
    // ran from a disabled app would make that switch a lie.
    mountWithApps([appWith([APPROVE_ALL], { enabled: false })])
    expect(optionByTitle(/Approve all PRs/)).toBeUndefined()
  })

  it('skips a malformed contribution without taking the launcher down', () => {
    // A third party must not be able to break the Cmd+K gesture for every other app.
    mountWithApps([
      appWith([
        { ...APPROVE_ALL, id: 'Approve_All' }, // id is not a kebab slug
        { ...APPROVE_ALL, id: 'no-prompt', prompt: '' },
        { ...APPROVE_ALL, id: 'bad-kind', argument: { ...APPROVE_ALL.argument, kind: 'regex' } },
        { ...APPROVE_ALL, id: 'bad-hosts', argument: { kind: 'text', hosts: ['github.com'] } },
        { ...APPROVE_ALL, id: 'orphan-token', argument: undefined },
        STANDUP,
      ]),
    ])
    // Every bad entry is gone; the good one in the same array still renders.
    expect(optionByTitle(/Approve all PRs/)).toBeUndefined()
    expect(optionByTitle(/Write my standup/)).toBeTruthy()
    // And the builtin rows are untouched.
    expect(optionByTitle(/New Session/)).toBeTruthy()
  })

  it('survives contributes.commands not being an array', () => {
    mountWithApps([appWith({ approve: APPROVE_ALL })])
    expect(optionByTitle(/New Session/)).toBeTruthy()
  })

  it('namespaces a contributed row so it cannot impersonate a builtin', () => {
    // A contribution claiming the New Session id would otherwise inherit its
    // frecency record and its place in the list.
    mountWithApps([appWith([{ ...APPROVE_ALL, id: 'new-session' }])])
    const rows = screen.getAllByRole('option').map(el => el.textContent ?? '')
    expect(rows.filter(t => /New Session/.test(t))).toHaveLength(1)
    expect(optionByTitle(/Approve all PRs/)).toBeTruthy()
  })

  it('does not lead the empty query with a command that needs an argument', () => {
    // A launcher pre-highlights its first row, and a row that cannot act until it is
    // given a value has nothing to offer a bar that just opened.
    mountWithApps([appWith([APPROVE_ALL])])
    expect(screen.getAllByRole('option')[0].textContent ?? '').not.toMatch(/Approve all PRs/)
  })

  it('the first Enter collects the argument instead of acting', () => {
    resolvingDispatch()
    mountWithApps([appWith([APPROVE_ALL])])
    enterCommand(/Approve all PRs/)
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } })
    expect(navigate).not.toHaveBeenCalled()
  })

  it('runs a command with no argument straight away', () => {
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    mountWithApps([appWith([STANDUP])])
    enterCommand(/Write my standup/)
    expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } })
  })

  it("refuses a value the app's own pattern rejects, creating nothing", async () => {
    resolvingDispatch()
    const onClose = mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: 'https://gitlab.com/g/p/-/merge_requests/1' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // The app supplies the message, because only the app knows what shape it wanted.
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Not a GitHub link.'))
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } })
    expect(onClose).not.toHaveBeenCalled()
    expect(input.value).toBe('https://gitlab.com/g/p/-/merge_requests/1')
  })

  it('refuses to fire a command whose app was disabled while the field was open', async () => {
    // The field stays open across an arbitrary pause -- the reader is pasting a link --
    // and the app can be switched off from the Apps page in another tab meanwhile. The
    // row disappears at once, but the command object captured when the field opened
    // would not, so a snapshot-trusting submit would run the prompt of an app the
    // reader had just disabled.
    resolvingDispatch()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })

    // The app is disabled underneath the open argument field.
    client.setQueryData(['apps'], [appWith([APPROVE_ALL], { enabled: false })])
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain('no longer available'),
    )
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } })
  })

  it('refuses to send a prompt that differs from the one it previewed', async () => {
    // Re-resolving at submit fixed a stale snapshot firing after its app was disabled,
    // but the preview still renders the snapshot -- so an app whose prompt changes while
    // the field is open would have the reader consenting to one instruction and sending
    // another. The preview refreshes and nothing goes out; the next Enter acts on what is
    // now on screen.
    resolvingDispatch()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })

    // The app rewrites the prompt underneath the open field.
    client.setQueryData(['apps'], [
      appWith([{ ...APPROVE_ALL, prompt: 'Delete every branch behind {argument}.' }]),
    ])
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('changed'))
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } })
    // The refreshed preview shows the new text, so the next Enter is informed.
    expect(screen.getByText(/Delete every branch behind https/)).toBeTruthy()
  })

  it('revokes an in-flight activation when the argument state is left', async () => {
    // Escape abandons the question but leaves the bar open, so the close-edge and unmount
    // revokes never fire. Without a revoke here, a slow session create started by Enter
    // resolves afterwards and seeds an auto-sent session the reader already cancelled.
    //
    // Only `createSlot` is held. Holding every dispatch would stall the chain at
    // `switchSlot` and the test would pass whether or not the revoke works — which is
    // exactly what the first version of it did.
    let release: ((v: unknown) => void) | undefined
    dispatch.mockImplementation((action: { type?: string }) => ({
      unwrap: () =>
        action?.type === 'createSlot'
          ? new Promise(r => {
              release = r
            })
          : Promise.resolve('ok'),
    }))
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // Step back out while the create is still in flight, then let it land.
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    release?.({ key: 'slot-new' })
    await waitFor(() => expect(release).toBeDefined())
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: expect.anything() })
    expect(navigate).not.toHaveBeenCalledWith('/chat?autoSend=1')
  })

  it('keeps the fresh slot selected through its own 404 so the seed cannot land elsewhere', async () => {
    // `switchSlot.rejected` treats a 404 as "target is gone" and restores the slot it
    // came from (#6309). Without `keepTargetOnMissing`, a create/fetch race on the slot
    // just created would put the reader back in their PREVIOUS conversation and then
    // auto-send a bulk instruction into it.
    dispatch.mockImplementation((action: { type?: string }) => ({
      unwrap: () =>
        action?.type === 'switchSlot'
          ? Promise.reject(Object.assign(new Error('not found'), { status: 404 }))
          : Promise.resolve({ key: 'slot-new' }),
    }))
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({
        type: 'switchSlot',
        key: 'slot-new',
        keepTargetOnMissing: true,
      }),
    )
    // The rejection is survivable: the seed still happens, on the slot just created.
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({ type: 'setPendingInput', text: expect.stringContaining(LINK) }),
    )
  })

  it('abandons an in-flight seed when its app is disabled during session creation', async () => {
    // `owned()` tracks the dialog's lifetime and cannot see this: disabling the app from
    // the Apps page while the create is still awaiting leaves the run legitimately owned
    // and the command gone, so the disabled app's prompt would still be sent.
    let release: ((v: unknown) => void) | undefined
    dispatch.mockImplementation((action: { type?: string }) => ({
      unwrap: () =>
        action?.type === 'createSlot'
          ? new Promise(r => {
              release = r
            })
          : Promise.resolve('ok'),
    }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // The app is disabled while the create is still in flight, then the create lands.
    client.setQueryData(['apps'], [appWith([APPROVE_ALL], { enabled: false })])
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    release?.({ key: 'slot-new' })
    await waitFor(() => expect(release).toBeDefined())
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: expect.anything() })
    expect(navigate).not.toHaveBeenCalledWith('/chat?autoSend=1')
  })

  it('refuses to send when a broadened matcher means no preview was ever shown', async () => {
    // The preview is withheld until the value validates. If the app widens its matcher
    // while the field is open, the reader has been looking at a rejection and never saw a
    // preview, while the live matcher now passes -- and the prompt itself is unchanged, so
    // comparing prompts cannot catch it.
    resolvingDispatch()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    const input = enterCommand(/Approve all PRs/)
    // Refused by the declared url+github.com matcher, so no preview renders.
    fireEvent.change(input, { target: { value: 'https://gitlab.com/g/p/-/merge_requests/1' } })
    expect(screen.queryByText(/Approve every pull request behind https/)).toBeNull()

    // The app widens the matcher to plain text, which would accept that value.
    client.setQueryData(['apps'], [
      appWith([{ ...APPROVE_ALL, argument: { kind: 'text' } }]),
    ])
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('changed'))
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } })
  })

  it('a stale activation does not clear a live one\'s duplicate-run guard', async () => {
    // `pendingRow` is what makes a second Enter a no-op while a create is in flight.
    // Cleared unconditionally, a STALE run wipes a LIVE one's: revoke during create A,
    // start create B, then let A resolve -- A's finally would release B's guard and the
    // next Enter would start a second create for the same intent, which with autoSend is
    // a duplicate session that sends.
    const releases: Array<(v: unknown) => void> = []
    dispatch.mockImplementation((action: { type?: string }) => ({
      unwrap: () =>
        action?.type === 'createSlot'
          ? new Promise(r => releases.push(r))
          : Promise.resolve('ok'),
    }))
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(releases).toHaveLength(1))

    // Revoke A (Escape out of the argument state), then start B. The pasted link stays in
    // the box as the root query, so clear it before the row is matchable again.
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '' } })
    const again = enterCommand(/Approve all PRs/)
    fireEvent.change(again, { target: { value: LINK } })
    fireEvent.keyDown(again, { key: 'Enter' })
    await waitFor(() => expect(releases).toHaveLength(2))

    // A resolves late. It must not release B's guard. Wrapped in `act` so the state
    // update from the promise callback is flushed before the next keystroke reads it --
    // without that the handler closure still sees the pre-clear value and the test
    // cannot tell the two behaviours apart.
    await act(async () => {
      releases[0]({ key: 'slot-a' })
      await Promise.resolve()
      await Promise.resolve()
    })

    // A third Enter while B is still in flight must start no further create.
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await Promise.resolve()
    expect(releases).toHaveLength(2)
  })

  it('refuses an over-cap value instead of sending a truncated prefix', async () => {
    // The field carried `maxLength` for one round, which made the BROWSER clip a paste to
    // the cap before `onChange` -- so a 2001-character value arrived as a valid-looking
    // 2000-character prefix and was sent. That is the silent truncation this module
    // refuses to do, and it had been reintroduced one layer below the check that refuses
    // it. The validator must see the whole value.
    resolvingDispatch()
    mountWithApps([appWith([{ ...APPROVE_ALL, argument: { kind: 'text' } }])])
    const input = enterCommand(/Approve all PRs/)
    // The load-bearing assertion. The behavioural half below CANNOT catch a regression
    // here: `fireEvent.change` assigns `.value` directly, which bypasses jsdom's
    // maxlength enforcement, so re-adding the attribute leaves every behavioural
    // expectation passing. Asserting the attribute is absent is what actually guards it.
    expect(input.getAttribute('maxLength')).toBeNull()
    const oversized = 'x'.repeat(2001)
    fireEvent.change(input, { target: { value: oversized } })

    // The whole value stays in the field -- nothing clipped it on the way in.
    expect(input.value).toHaveLength(2001)
    // No preview, because the value does not validate.
    expect(screen.queryByText(/WILL SEND/i)).toBeNull()

    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } })
  })

  it('shows the resolved prompt before an auto-sending command fires', async () => {
    // The consent mechanism for autoSend: app-authored text goes to an agent with
    // tools as if the reader typed it, so the reader is shown the instruction with
    // their own value already spliced in.
    resolvingDispatch()
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    await waitFor(() => expect(screen.getByText(/Approve every pull request behind https/)).toBeTruthy())
    expect(screen.getByText(/Skip any I authored/)).toBeTruthy()
  })

  describe('the clipped-preview cue', () => {
    // jsdom does no layout: `scrollHeight` and `clientHeight` are both 0, so
    // `scrollHeight > clientHeight + 1` is `0 > 1` and the cue's branch is UNREACHABLE
    // in a test that does not stub the metrics. That is why it shipped with no coverage
    // at all, and why its only evidence was a screenshot -- one that happens to show a
    // prompt clipped mid-sentence with no cue in the frame. Stubbing the two properties
    // is what makes the safety cue testable rather than merely asserted.
    const stubMetrics = (scrollHeight: number, clientHeight: number) => {
      const proto = HTMLPreElement.prototype
      const original = {
        scrollHeight: Object.getOwnPropertyDescriptor(proto, 'scrollHeight'),
        clientHeight: Object.getOwnPropertyDescriptor(proto, 'clientHeight'),
      }
      Object.defineProperty(proto, 'scrollHeight', { configurable: true, value: scrollHeight })
      Object.defineProperty(proto, 'clientHeight', { configurable: true, value: clientHeight })
      return () => {
        for (const [key, desc] of Object.entries(original)) {
          if (desc) Object.defineProperty(proto, key, desc)
          else delete (proto as unknown as Record<string, unknown>)[key]
        }
      }
    }

    it('warns when the instruction continues out of sight', async () => {
      const restore = stubMetrics(500, 160)
      try {
        resolvingDispatch()
        mountWithApps([appWith([APPROVE_ALL])])
        const input = enterCommand(/Approve all PRs/)
        fireEvent.change(input, { target: { value: LINK } })
        await waitFor(() => expect(screen.getByText(/Scroll to read the rest/)).toBeTruthy())
      } finally {
        restore()
      }
    })

    it('stays silent when the whole instruction is visible', async () => {
      // The converse, so the cue cannot pass by always warning -- which would train the
      // reader to ignore the one line that means the tail is hidden.
      const restore = stubMetrics(120, 160)
      try {
        resolvingDispatch()
        mountWithApps([appWith([APPROVE_ALL])])
        const input = enterCommand(/Approve all PRs/)
        fireEvent.change(input, { target: { value: LINK } })
        await waitFor(() =>
          expect(screen.getByText(/Approve every pull request behind https/)).toBeTruthy(),
        )
        expect(screen.queryByText(/Scroll to read the rest/)).toBeNull()
      } finally {
        restore()
      }
    })

    it('re-measures when the box changes, not only when the text does', async () => {
      // The effect's deps are the previewed CONTENT, so on its own it cannot notice the
      // box shrinking: narrow the viewport and a prompt that fitted starts clipping with
      // the cue absent -- and with autoSend, Enter then sends a tail never shown. That is
      // the unsafe direction, so the box itself is observed. jsdom supplies no
      // ResizeObserver, so it is stubbed the way the rest of this repo stubs it.
      const callbacks: Array<() => void> = []
      class FakeResizeObserver {
        constructor(cb: () => void) {
          callbacks.push(cb)
        }
        observe = vi.fn()
        unobserve = vi.fn()
        disconnect = vi.fn()
      }
      const originalRO = globalThis.ResizeObserver
      globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
      // Starts fitting, so the cue is correctly absent.
      let restoreMetrics = stubMetrics(120, 160)
      try {
        resolvingDispatch()
        mountWithApps([appWith([APPROVE_ALL])])
        const input = enterCommand(/Approve all PRs/)
        fireEvent.change(input, { target: { value: LINK } })
        await waitFor(() =>
          expect(screen.getByText(/Approve every pull request behind https/)).toBeTruthy(),
        )
        expect(screen.queryByText(/Scroll to read the rest/)).toBeNull()
        expect(callbacks.length).toBeGreaterThan(0)

        // The box now overflows with the TEXT UNCHANGED, which only the observer can see.
        restoreMetrics()
        restoreMetrics = stubMetrics(500, 160)
        await act(async () => {
          for (const cb of callbacks) cb()
        })
        expect(screen.getByText(/Scroll to read the rest/)).toBeTruthy()
      } finally {
        restoreMetrics()
        globalThis.ResizeObserver = originalRO
      }
    })

    it('gives the preview a keyboard stop, so the scroll instruction is followable', async () => {
      // Structural, not behavioural, and deliberately so: jsdom does not scroll, so no
      // behavioural test can show a keyboard user reaching the tail. The defect was that
      // the cue said "scroll to read the rest" while the box was a scroll container with
      // no tab stop -- unreachable without a mouse, on the surface that carries consent.
      const restore = stubMetrics(500, 160)
      try {
        resolvingDispatch()
        mountWithApps([appWith([APPROVE_ALL])])
        const input = enterCommand(/Approve all PRs/)
        fireEvent.change(input, { target: { value: LINK } })
        await waitFor(() => expect(screen.getByText(/Scroll to read the rest/)).toBeTruthy())
        const preview = screen.getByRole('region', { name: /will send/i })
        expect(preview.getAttribute('tabIndex')).toBe('0')
        // The name matters as much as the stop: a bare focusable block is a silent halt.
        expect(preview.getAttribute('aria-labelledby')).toBeTruthy()
      } finally {
        restore()
      }
    })
  })

  it('names the contributing app in the row meta, where a subtitle cannot hide it', () => {
    // `subtitle || appLabel` meant an app that wrote its own subtitle left the root row
    // visually identical to a builtin -- same badge, same shape -- while its prompt goes
    // to an agent with tools. An argument-less command never reaches the argument state
    // either, so provenance could go unanswered end to end. The meta column is the one
    // place in the root list that can carry it.
    resolvingDispatch()
    mountWithApps([appWith([{ id: 'standup', title: 'Zzstandup', prompt: 'Summarise' }])])
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzstandup' } })
    // The composed meta, not a bare app-name match: with no subtitle the fallback renders
    // the app name too, so a loose matcher would pass on the subtitle alone and prove
    // nothing about the meta column this fix is about.
    expect(screen.getByText(`PR Bulk Ops \u00B7 Command`)).toBeTruthy()
  })

  it('names the contributing app even when the app wrote its own subtitle', () => {
    // `subtitle || appLabel` meant any app that supplied a subtitle displaced the only
    // mention of who authored the prompt -- and this is the step right before that prompt
    // goes to an agent with tools, so a third-party instruction read as a native one.
    resolvingDispatch()
    mountWithApps([appWith([APPROVE_ALL])])
    enterCommand(/Approve all PRs/)
    expect(screen.getByText(APPROVE_ALL.subtitle)).toBeTruthy()
    expect(screen.getByText('PR Bulk Ops')).toBeTruthy()
  })

  it.each(['__proto__', 'constructor', 'toString'])(
    'renders a row whose icon names the inherited key %s',
    (icon) => {
      // Measured, not assumed: with a plain `CONTRIBUTED_ICONS[name] ?? fallback`, only
      // `__proto__` actually throws -- "Objects are not valid as a React child" -- and it
      // throws while BUILDING the row list, so the whole overlay goes down on every open
      // rather than that one row degrading. `constructor` and `toString` return FUNCTIONS,
      // which are equally not-nullish and equally never the intended glyph, but which this
      // React version tolerates rather than rejecting; they are covered here because the
      // lookup being wrong is the defect, not because they crash today. `icon` is
      // deliberately unvalidated past being a string, so any name reaches this line.
      resolvingDispatch()
      mountWithApps([appWith([{ ...APPROVE_ALL, icon }])])
      expect(screen.getByText(/Approve all PRs/)).toBeTruthy()
    },
  )

  it('does not promise a view for a row that opens none', () => {
    // Uses an ARGUMENT-LESS contributed command, which is the shape the label was most
    // wrong for: its Enter creates a seeded session, so "Open View" named something that
    // does not happen at all. It also makes the selection deterministic -- a unique query
    // leaves this row selected at index 0, and the footer names the SELECTED row, so
    // without that the assertion would read whatever row happened to be first and pass
    // for the wrong reason. Asserting the old label is ABSENT is the half that guards
    // against reverting to the view row's shared key, since any correct label satisfies
    // the positive check.
    resolvingDispatch()
    mountWithApps([
      appWith([{ id: 'standup', title: 'Zzstandup', prompt: 'Summarise yesterday' }]),
    ])
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzstandup' } })
    expect(screen.queryByText('Open View')).toBeNull()
    expect(screen.getByText('Continue')).toBeTruthy()
  })

  it('withholds the preview until the value satisfies the pattern', () => {
    // A half-built template would advertise text that is not what would be sent.
    // Asserted on a phrase unique to the PROMPT: the subtitle shares its opening
    // words, so a looser matcher is satisfied by copy that is always on screen.
    resolvingDispatch()
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: 'https://gitl' } })
    expect(screen.queryByText(/Skip any I authored/)).toBeNull()
  })

  it('seeds the resolved prompt and sends it when the app asked to', async () => {
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    const onClose = mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: { activate: false } }))
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({
        type: 'switchSlot',
        key: 'slot-new',
        keepTargetOnMissing: true,
      }),
    )
    const seeded = dispatch.mock.calls.map(c => c[0]).find(a => a?.type === 'setPendingInput')
    expect(seeded.text).toBe(`Approve every pull request behind ${LINK}. Skip any I authored.`)
    // `autoSend=1` and NOT `newSession=1`: the session already exists.
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat?autoSend=1'))
    expect(onClose).toHaveBeenCalled()
  })

  it('leaves a non-auto-sending command in the composer', async () => {
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    mountWithApps([appWith([{ ...APPROVE_ALL, autoSend: false }])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat'))
  })

  it('Escape leaves the command before it closes the bar', () => {
    resolvingDispatch()
    const onClose = mountWithApps([appWith([APPROVE_ALL])])
    enterCommand(/Approve all PRs/)
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('Backspace on an empty field leaves the command too', () => {
    resolvingDispatch()
    const onClose = mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.keyDown(input, { key: 'Backspace' })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getAllByRole('option').length).toBeGreaterThan(0)
  })

  it('refuses a second Enter while the first is still creating', () => {
    let release: ((v: unknown) => void) | undefined
    dispatch.mockReturnValue({ unwrap: () => new Promise(r => { release = r }) })
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(dispatch.mock.calls.filter(c => c[0]?.type === 'createSlot')).toHaveLength(1)
    release?.({ key: 'slot-new' })
  })

  it('seeds nothing when the bar is dismissed mid-create', async () => {
    let release: ((v: unknown) => void) | undefined
    dispatch.mockReturnValue({ unwrap: () => new Promise(r => { release = r }) })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    fireEvent.mouseDown(optionByTitle(/Approve all PRs/))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: LINK } })
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open={false} onClose={onClose} />
      </QueryClientProvider>,
    )
    release?.({ key: 'slot-new' })
    await Promise.resolve()
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: expect.anything() })
    expect(navigate).not.toHaveBeenCalledWith('/chat?autoSend=1')
  })
})
