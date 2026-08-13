import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import path from 'node:path'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { settingsSubtitle, settingsTabLabel } from '../../components/commandPalette/settingsTabLabel'
import { i18nT } from '../../i18n/t'
import CommandBarOverlay from './CommandBarOverlay'

/**
 * Row-level behaviour of the launcher: what a row DOES when activated, and what the
 * user is told when it does not work. `rootIndex.test.ts` covers ranking; these are
 * the assertions that need the component rendered.
 */

const dispatch = vi.fn()
const navigate = vi.fn()

vi.mock('../../store', () => ({ useAppDispatch: () => dispatch }))
vi.mock('../../store/chatSlice', () => ({ createSlot: (arg: unknown) => ({ type: 'createSlot', arg }) }))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({ navigate }),
}))
const sessionSearch = vi.fn(async () => [] as unknown[])
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: sessionSearch }),
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

const rowByText = (text: string) =>
  screen.getByText(text).closest('[role="option"]') as HTMLElement

/** The title the overlay renders for a settings entry: catalog string when keyed. */
const renderedTitle = (entry: (typeof SETTINGS_REGISTRY)[number]) =>
  entry.labelKey ? i18nT(entry.labelKey) : entry.label

const channelsEntry = SETTINGS_REGISTRY.find(e => e.tab === 'channels')!

describe('CommandBarOverlay rows', () => {
  beforeEach(() => {
    dispatch.mockReset()
    navigate.mockReset()
    sessionSearch.mockReset()
    sessionSearch.mockResolvedValue([])
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
    const files = execFileSync(
      'grep',
      ['-rln', "queryKey: \\['apps'\\]", '--include=*.ts', '--include=*.tsx', srcRoot],
      { encoding: 'utf-8' },
    ).trim().split('\n')
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
    // unpainted otherwise, or the surface is either permanently boxed or, on the
    // empty sessions view, shows a keyboard user no focus at all.
    const src = readFileSync(
      path.join(__dirname, 'CommandBarOverlay.tsx'),
      'utf-8',
    )
    expect(src).toContain("rowCount === 0 ? ' focus-visible:ring-1 focus-visible:ring-accent/40' : ''")
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

  it('reports a failed session search instead of claiming no matches', async () => {
    // A rejected search leaves `data` undefined, which by row count alone looks
    // identical to an empty result -- so the empty copy would tell the user their
    // session does not exist. That is the one state that lies.
    sessionSearch.mockRejectedValue(new Error('gateway down'))
    mount()
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'quarterly' } })
    expect(await screen.findByText('Search failed')).toBeTruthy()
    expect(screen.queryByText('No sessions match')).toBeNull()
    // And the failure is recoverable in place.
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })
})
