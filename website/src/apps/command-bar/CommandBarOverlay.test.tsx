import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { localizedSettingLabel } from '../../components/commandPalette/settingsSearchCore'
import { settingsSubtitle, settingsTabLabel } from '../../components/commandPalette/settingsTabLabel'
import CommandBarOverlay from './CommandBarOverlay'
import { crewModePreviewOn } from '../../test/crewModePreview'

/**
 * Row-level behaviour of the launcher: what a row DOES when activated, and what the
 * user is told when it does not work. `rootIndex.test.ts` covers ranking; these are
 * the assertions that need the component rendered.
 *
 * Commands an installed app contributes are tested in
 * `CommandBarOverlay.contributedCommands.test.tsx`, which keeps its own copy of the
 * mocks below: a `vi.mock` applies only to the file that calls it.
 */

const dispatch = vi.fn()
const navigate = vi.fn()
const newSessionWithToken = vi.fn()
const enterInsertOrNewSession = vi.fn()

/** Live store the overlay reads for the attention section. Mutated per test. */
const storeState: {
  dashboard: { slots: Record<string, unknown>[]; unreadSlots: string[] }
  chat: { slotStatusDetail: Record<string, unknown>; activeSlot: string | null; lastCreatedActivation?: { slot: string; requestId: string } | null }
} = {
  dashboard: { slots: [], unreadSlots: [] },
  chat: { slotStatusDetail: {}, activeSlot: null },
}

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
  // The session rows read the active slot through a store HANDLE once their
  // switch settles; the same fixture state answers that read.
  useAppStore: () => ({ getState: () => storeState }),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  // Mirrors the real thunk's `SwitchSlotArg`: a bare key, or an options object whose
  // `keepTargetOnMissing` decides whether a 404 unwinds to the previous slot.
  switchSlot: (arg: string | { key: string; keepTargetOnMissing?: boolean }) =>
    typeof arg === 'string' ? { type: 'switchSlot', key: arg } : { type: 'switchSlot', ...arg },
  requestFolderReveal: (folderId: string) => ({ type: 'requestFolderReveal', folderId }),
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
// Passed through, and watched: a settings row must be scored by the scorer the
// other settings searches share, not by the bar's own field matcher.
const scoreSettingEntry = vi.hoisted(() => ({ spy: null as null | ReturnType<typeof vi.fn> }))
vi.mock('../../components/commandPalette/settingsSearchCore', async importOriginal => {
  const real = await importOriginal<typeof import('../../components/commandPalette/settingsSearchCore')>()
  scoreSettingEntry.spy = vi.fn(real.scoreSettingEntry)
  return { ...real, scoreSettingEntry: (...args: Parameters<typeof real.scoreSettingEntry>) => scoreSettingEntry.spy!(...args) }
})
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
const cycleTheme = vi.fn()
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: cycleTheme }) }))
// Only the filing call is stubbed — it talks to the folder API. `commandFolderName`
// stays REAL: it decides what the folder is called, which is exactly what these tests
// assert, and a stub would let them agree with themselves.
const fileSessionInCommandFolder = vi.fn(async () => 'folder-1')
vi.mock('./sessionFolder', async importOriginal => ({
  ...(await importOriginal<typeof import('./sessionFolder')>()),
  fileSessionInCommandFolder: (...args: unknown[]) => fileSessionInCommandFolder(...(args as [])),
}))

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

/** The overlay's whole source: the facade and every owner beside it, the file
 *  family `CommandBarOverlay.owners.test.tsx` pins. */
const overlaySource = () =>
  readdirSync(__dirname).filter(f => /^(?:use)?[Cc]ommandBarOverlay\w*\.tsx?$/.test(f)).sort()
    .map(f => readFileSync(path.join(__dirname, f), 'utf-8')).join('\n')

/** Mirrors the overlay's own query debounce (commandBarOverlayModel.ts DEBOUNCE_MS).
 *  The latch-close/pointer tests advance a fake clock by this to settle the
 *  debounced refetch deterministically instead of waiting on wall-clock time. */
const DEBOUNCE_MS = 150

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

  it('starts from the type-ahead the host caught, and Enter acts on it', async () => {
    // Keys typed while the chunk loaded arrive as `initialQuery`. Without them the
    // first render is the idle root, and Enter opens the newest Recent session.
    resolvingDispatch()
    const onClose = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} initialQuery="New session" />
      </QueryClientProvider>,
    )
    const input = screen.getByRole('combobox') as HTMLInputElement
    expect(input.value).toBe('New session')
    // The caret sits after the caught text, so the next key extends it.
    expect(input.selectionStart).toBe('New session'.length)
    expect(screen.getAllByRole('option')[0].textContent).toContain('New Session')
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: undefined })
  })

  /** A create the store opened: the reducer records it under its requestId. */
  const openingDispatch = (activation: { slot: string; requestId: string }) => {
    dispatch.mockReturnValue({ requestId: 'req-1', unwrap: () => Promise.resolve({ key: 'slot-1' }) })
    storeState.chat = { ...storeState.chat, activeSlot: activation.slot, lastCreatedActivation: activation }
  }

  it('navigates to the new session so a success is visible off the chat page', async () => {
    // Created from Settings or Task Runner without this, the session lands off-screen
    // and the success reads as a failure -- the user runs it again into a duplicate.
    window.history.replaceState(null, '', '/settings')
    try {
      openingDispatch({ slot: 'slot-1', requestId: 'req-1' })
      const onClose = mount()
      fireEvent.mouseDown(rowByText('New Session'))
      await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat'))
      await waitFor(() => expect(onClose).toHaveBeenCalled())
    } finally {
      window.history.replaceState(null, '', '/')
    }
  })

  it('stays put when the user opened another session during the create', async () => {
    // The store left the view on the session the user picked, so the activation
    // names no request of this row's.
    openingDispatch({ slot: 'slot-b', requestId: 'req-other' })
    const onClose = mount()
    fireEvent.mouseDown(rowByText('New Session'))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(navigate).not.toHaveBeenCalled()
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
    // Several `Speed` selects live in the Voice tab, one per TTS provider,
    // distinguished in the registry only by their description. A tab-only
    // subtitle renders them identically, which is the shipped defect: the user
    // cannot tell which row they are choosing. The tab name must also be
    // localized, never a raw machine key like `computer-use`.
    //
    // The count is derived, not pinned: adding a provider adds a row, and a
    // literal here would fail for that rather than for a lost subtitle. What
    // must hold is one DISTINCT subtitle per duplicate row, whatever the count.
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'speed' } })
    const dupes = SETTINGS_REGISTRY.filter(e => e.label === 'Speed' && e.tab === 'voice')
    expect(dupes.length).toBeGreaterThan(1)
    const subtitles = dupes.map(e => settingsSubtitle(e))
    expect(new Set(subtitles).size).toBe(dupes.length)
    for (const s of subtitles) {
      expect(s).toContain(settingsTabLabel('voice'))
      expect(screen.getByText(s)).toBeTruthy()
    }
    expect(settingsTabLabel('computer-use')).not.toBe('computer-use')
  })

  it('withholds the settings rows the other searches withhold', () => {
    // A browser window: About draws the gateway's switch and no app updater, so a
    // row for the app switch would land on a tab with nothing to flash.
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'automatic' } })
    const options = screen.getAllByRole('option').map(o => o.textContent ?? '')
    expect(options.some(o => o.includes('Update the gateway automatically'))).toBe(true)
    expect(options.some(o => o.includes('Install app updates automatically'))).toBe(false)
  })

  it('finds a settings row by the synonyms the other searches use', () => {
    // "auto-update" is in neither switch's label; it is the name both shipped under.
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'auto-update' } })
    const option = screen.getAllByRole('option').find(o => o.textContent?.includes('Update the gateway automatically'))
    // The synonym that matched is drawn on the row, since neither its title nor
    // its subtitle carries the word.
    expect(option).toHaveTextContent('auto-update')
  })

  it('marks a settings row matched by its description in its subtitle', () => {
    // In the description only: "Enterprise Grid org or workspace IDs to allow (starts with E or T). ..."
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'starts with E or T' } })
    const option = screen.getAllByRole('option').find(o => o.textContent?.includes('Allowed enterprise orgs'))!
    expect(option.querySelector('strong')?.textContent).toBe('starts with E or T')
  })

  it('scores settings rows with the scorer the other settings searches share', () => {
    mount()
    scoreSettingEntry.spy!.mockClear()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'automatic' } })
    expect(scoreSettingEntry.spy).toHaveBeenCalledWith('automatic', expect.objectContaining({ id: 'about.update-the-gateway-automatically' }))
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
    const hint = await screen.findByText(/turn full search back on/)
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
    expect(screen.queryByText(/turn full search back on/)).toBeNull()
    // With nothing matched, the dead end is real and the row appears.
    fireEvent.change(input, { target: { value: 'zzzznomatch' } })
    expect(await screen.findByText(/turn full search back on/)).toBeTruthy()
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
    const src = overlaySource()
    expect(src).toMatch(/useSessionsProvider\(\{ active: scope === 'sessions'(, openSessionKeys)? \}\)/)
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
    const src = overlaySource()
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
    const src = overlaySource()
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

  const hit = (title: string, onActivate: () => void) => ({
    id: `sessions:${title}`,
    providerId: 'sessions',
    title,
    icon: null,
    score: 1,
    indices: [],
    onActivate,
  })

  it('latches an Enter pressed in the debounce window and fires it on the live rows', async () => {
    // Every scoped view ranks from the DEBOUNCED query, so for one debounce interval
    // after a keystroke its rows answer the previous query. An Enter in that window
    // must never open the row selected against the OLD query, and must not be dropped:
    // it is held and fired once the rows answer what the reader typed. Reported in the
    // crewmates view; the latch is on the activation path all four views share.
    const openAlpha = vi.fn()
    const openBeta = vi.fn()
    sessionSearch.mockResolvedValue([hit('Alpha planning', openAlpha)])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'alpha' } })
    expect(await screen.findByText('Alpha planning')).toBeTruthy()
    sessionSearch.mockResolvedValue([hit('Beta review', openBeta)])
    fireEvent.change(input, { target: { value: 'beta' } })
    // No debounce tick yet: the row on screen still answers `alpha`.
    fireEvent.keyDown(input, { key: 'Enter' })
    // The stale row is never opened, not even for the instant before the rows catch up.
    expect(openAlpha).not.toHaveBeenCalled()
    // Once the rows answer `beta`, the LATCHED Enter fires on its own — no second press.
    await waitFor(() => expect(openBeta).toHaveBeenCalled())
    expect(openAlpha).not.toHaveBeenCalled()
  })

  it('drops the latch on Escape rather than firing it late', async () => {
    const openAlpha = vi.fn()
    const openBeta = vi.fn()
    sessionSearch.mockResolvedValue([hit('Alpha planning', openAlpha)])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'alpha' } })
    expect(await screen.findByText('Alpha planning')).toBeTruthy()
    sessionSearch.mockResolvedValue([hit('Beta review', openBeta)])
    fireEvent.change(input, { target: { value: 'beta' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // Escape steps out of the scope and must take the pending Enter with it.
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    // Let the debounce and any refetch settle: a surviving latch would fire here.
    await waitFor(() => expect(screen.queryByText('Alpha planning')).toBeNull())
    expect(openBeta).not.toHaveBeenCalled()
    expect(openAlpha).not.toHaveBeenCalled()
  })

  it('drops the latch when the reader leaves the scope', async () => {
    const openAlpha = vi.fn()
    const openBeta = vi.fn()
    sessionSearch.mockResolvedValue([hit('Alpha planning', openAlpha)])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'alpha' } })
    expect(await screen.findByText('Alpha planning')).toBeTruthy()
    sessionSearch.mockResolvedValue([hit('Beta review', openBeta)])
    fireEvent.change(input, { target: { value: 'beta' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // Empty the field, then Backspace leaves the scope — the latch must not survive it.
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.keyDown(input, { key: 'Backspace' })
    await waitFor(() => expect(screen.queryByText('Search Sessions')).not.toBeNull())
    expect(openBeta).not.toHaveBeenCalled()
    expect(openAlpha).not.toHaveBeenCalled()
  })

  it('drops the latch when the bar closes', async () => {
    const openAlpha = vi.fn()
    const openBeta = vi.fn()
    sessionSearch.mockResolvedValue([hit('Alpha planning', openAlpha)])
    const ctl = mountControllable()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'alpha' } })
    expect(await screen.findByText('Alpha planning')).toBeTruthy()
    sessionSearch.mockResolvedValue([hit('Beta review', openBeta)])
    fireEvent.change(input, { target: { value: 'beta' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // The bar closes under the pending Enter — reopening must not replay it. The debounce
    // is the overlay's own setTimeout (DEBOUNCE_MS), so a fake clock drives it exactly:
    // `shouldAdvanceTime` keeps real microtasks/`findBy*` flushing while advancing the
    // timer deterministically settles the `beta` query — the window in which a latch that
    // survived the close would wrongly fire on reopen, with no arbitrary wall-clock wait.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      act(() => ctl.rerender(false))
      act(() => ctl.rerender(true))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DEBOUNCE_MS * 2)
      })
    } finally {
      vi.useRealTimers()
    }
    expect(openBeta).not.toHaveBeenCalled()
    expect(openAlpha).not.toHaveBeenCalled()
  })

  it('drops the latch when the catch-up fetch fails onto a retry dead end', async () => {
    // The query the Enter is pressed against resolves to an error with no cached rows,
    // so slot 0 is the synthesized `retry` row, not a result. The latch must drop on
    // that dead end rather than fire it: firing the retry row would re-run the search,
    // so the test counts search invocations — a latch that wrongly fired the dead end
    // would drive a THIRD call, which makes this able to fail (it does not just rely on
    // there being nothing to open).
    const openAlpha = vi.fn()
    sessionSearch.mockResolvedValueOnce([hit('Alpha planning', openAlpha)])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'alpha' } })
    expect(await screen.findByText('Alpha planning')).toBeTruthy()
    const callsBeforeEnter = sessionSearch.mock.calls.length
    // The query the Enter is pressed against fails: the on-screen `alpha` row is stale,
    // and the failing `beta` key has no cached rows, so slot 0 becomes the retry row.
    sessionSearch.mockRejectedValue(new Error('gateway down'))
    fireEvent.change(input, { target: { value: 'beta' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // The failure row appears; the latch must drop rather than fire on the dead end.
    await screen.findByText('Search failed')
    await act(async () => {
      await new Promise(r => setTimeout(r, 0))
    })
    // Never opened the stale `alpha` row, and never fired the retry row (which would
    // have re-run the search beyond the renders the user's own typing drove).
    expect(openAlpha).not.toHaveBeenCalled()
    expect(sessionSearch.mock.calls.length).toBe(callsBeforeEnter + 1)
  })

  it('drops the latch when the reader keeps typing after the Enter', async () => {
    // 'onc' settles, then 'once' + Enter in the window confirms `once`; a further
    // keystroke to `oncex` means the reader never confirmed what the rows now answer.
    // A latch that committed whatever was typed next would open a query never
    // confirmed, so a further keystroke drops it.
    const openOncex = vi.fn()
    sessionSearch.mockResolvedValue([hit('Onc result', () => {})])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'onc' } })
    expect(await screen.findByText('Onc result')).toBeTruthy()
    sessionSearch.mockResolvedValue([hit('Oncex result', openOncex)])
    // Enter lands in the window (rows still answer `onc`); then the reader types on.
    fireEvent.change(input, { target: { value: 'once' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.change(input, { target: { value: 'oncex' } })
    // Rows settle on `oncex`; the never-confirmed `oncex` row is not opened.
    expect(await screen.findByText('Oncex result')).toBeTruthy()
    await new Promise(r => setTimeout(r, 50))
    expect(openOncex).not.toHaveBeenCalled()
  })

  it('drops the latch when the selection moves after the Enter', async () => {
    // ArrowDown after a latched Enter aims at a different row. Firing row 0 regardless
    // would open something the reader just moved off of, so a moved selection drops it.
    const openTop = vi.fn()
    const openSecond = vi.fn()
    sessionSearch.mockResolvedValue([hit('Top one', () => {}), hit('Top two', () => {})])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'top' } })
    expect(await screen.findByText('Top one')).toBeTruthy()
    sessionSearch.mockResolvedValue([
      hit('Fresh top', openTop),
      hit('Fresh second', openSecond),
    ])
    fireEvent.change(input, { target: { value: 'fresh' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // Move the selection off row 0 while the latch is armed.
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    // Rows settle; the latch dropped, so neither row opens from the stale Enter.
    expect(await screen.findByText('Fresh top')).toBeTruthy()
    await new Promise(r => setTimeout(r, 50))
    expect(openTop).not.toHaveBeenCalled()
    expect(openSecond).not.toHaveBeenCalled()
  })

  it('drops the latch when a pointer activation keeps the bar open', async () => {
    // A click acts on the row it is on, immediately. A latch left armed by a prior
    // keyboard Enter would then fire a SECOND time once the rows caught up, so a
    // pointer activation drops any pending Enter.
    const openStale = vi.fn()
    const openFresh = vi.fn()
    sessionSearch.mockResolvedValue([hit('Stale row', openStale)])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'stale' } })
    expect(await screen.findByText('Stale row')).toBeTruthy()
    sessionSearch.mockResolvedValue([hit('Fresh row', openFresh)])
    fireEvent.change(input, { target: { value: 'fresh' } })
    // Enter in the window arms the latch; then a click lands on the row still on
    // screen, before the debounce settles.
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.mouseDown(rowByText('Stale row'))
    // The click opened the row it was on, exactly once.
    expect(openStale).toHaveBeenCalledTimes(1)
    // Rows catch up to `fresh`: a surviving latch would fire openFresh here. The debounce
    // is the overlay's own setTimeout (DEBOUNCE_MS), so advancing a fake clock settles the
    // `fresh` refetch deterministically — `shouldAdvanceTime` keeps the microtask flush the
    // flow needs — and that advance is the window in which a dropped-clear mutation would
    // wrongly fire, with no arbitrary wall-clock wait.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DEBOUNCE_MS * 2)
      })
    } finally {
      vi.useRealTimers()
    }
    expect(openFresh).not.toHaveBeenCalled()
    expect(openStale).toHaveBeenCalledTimes(1)
  })

  it('drops the latch when the row under the captured index is a different row', async () => {
    // The reader arrows off row 0 onto a lower row, then presses Enter in the window.
    // If the new query is already cached its rows swap in under the same indices with
    // no empty frame, so the index still points somewhere — at a row the reader never
    // had highlighted. Firing it opens a stranger, the wrong-row hazard this latch
    // exists to close, so a changed row identity at that index drops the latch.
    const openA1 = vi.fn()
    const openA2 = vi.fn()
    const openB1 = vi.fn()
    const openB2 = vi.fn()
    // Settle `beta` first so its rows are cached and swap in without a fetch frame.
    sessionSearch.mockResolvedValue([hit('B one', openB1), hit('B two', openB2)])
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(input, { target: { value: 'beta' } })
    expect(await screen.findByText('B two')).toBeTruthy()
    // Now settle `alpha`: the on-screen list is [A one, A two].
    sessionSearch.mockResolvedValue([hit('A one', openA1), hit('A two', openA2)])
    fireEvent.change(input, { target: { value: 'alpha' } })
    expect(await screen.findByText('A two')).toBeTruthy()
    // Type `beta` (its key is cached) and arrow onto the stale `A two` at index 1,
    // then press Enter while the list still shows the alpha rows.
    fireEvent.change(input, { target: { value: 'beta' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    // Rows swap to [B one, B two]; the row now at index 1 is `B two`, which the reader
    // never selected. The latch must drop rather than open it.
    expect(await screen.findByText('B two')).toBeTruthy()
    await new Promise(r => setTimeout(r, 50))
    expect(openB2).not.toHaveBeenCalled()
    expect(openB1).not.toHaveBeenCalled()
    expect(openA1).not.toHaveBeenCalled()
    expect(openA2).not.toHaveBeenCalled()
  })

  it('leaves the sessions failure a row, with none of the artifacts scope notice', async () => {
    // Guard on a deliberate boundary. The artifacts scope renders its failure through
    // ErrorNotice above the list, and it would have been easy to reach that surface for
    // both scopes on the grounds that they should match. They should not: the sessions
    // row's text was never a backend string, so nothing about it misled anyone, and
    // changing it would mean a feature PR reshaping a surface it does not own. Without
    // this, the gate can be removed and every other test here still passes.
    sessionSearch.mockRejectedValue(new Error('gateway down'))
    mount()
    fireEvent.mouseDown(rowByText('Search Sessions'))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'quarterly' } })
    await screen.findByText('Search failed')
    expect(screen.queryByRole('alert')).toBeNull()
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
    let px = 0
    const selectRow = (title: string) => {
      const target = screen.getAllByRole('option').findIndex(o => o.textContent?.includes(title))
      expect(target).toBeGreaterThanOrEqual(0)
      // Hover selects on real pointer motion, so the pointer has to move across the row.
      const row = screen.getAllByRole('option')[target]
      fireEvent.mouseMove(row, { clientX: ++px, clientY: px })
      fireEvent.mouseMove(row, { clientX: ++px, clientY: px })
    }
    selectRow('New Session')
    expect(footer()).toContain('Run')
    // A `view` row does not act and close, and the verb has to say so.
    selectRow('Search Sessions')
    expect(footer()).toContain('Open View')
    expect(input.getAttribute('aria-activedescendant')).toBeTruthy()
  })

  it('keeps the selection when rows re-render under a pointer that has not moved', () => {
    // Re-ranking moves rows under a resting pointer, and the browser answers with
    // mouseover, mouseenter and a mousemove at the same coordinates. None of that is
    // the reader pointing at a row, so Enter must still act on the top match.
    mount()
    const input = screen.getByRole('combobox')
    const active = () => input.getAttribute('aria-activedescendant')
    const restOn = (row: HTMLElement) => {
      fireEvent.mouseOver(row, { clientX: 700, clientY: 260 })
      fireEvent.mouseEnter(row, { clientX: 700, clientY: 260 })
      fireEvent.mouseMove(row, { clientX: 700, clientY: 260 })
    }
    restOn(screen.getAllByRole('option')[2])
    fireEvent.change(input, { target: { value: 'new sess' } })
    restOn(screen.getAllByRole('option')[1])
    fireEvent.change(input, { target: { value: 'new session' } })
    restOn(screen.getAllByRole('option')[1])
    expect(active()).toBe('command-bar-row-0')
    // Real motion across a row still selects it, which is what hover is for.
    fireEvent.mouseMove(screen.getAllByRole('option')[1], { clientX: 704, clientY: 262 })
    expect(active()).toBe('command-bar-row-1')
  })

  it.each([
    ['a group header', () => screen.getAllByRole('option')[0].previousElementSibling as HTMLElement],
    ['the backdrop', () => document.querySelector('[role="presentation"]') as HTMLElement],
  ])('keeps the selection when the pointer stops on %s and the list re-ranks', (_, restTarget) => {
    // The pointer crosses rows, then comes to rest somewhere that is not a row. The
    // re-ranked list then puts a row under it, and the browser fires a mousemove there
    // at the resting coordinates. Compared against the last ROW position, that would
    // read as motion and select the row, so Enter would act on it instead of the top match.
    mount()
    const input = screen.getByRole('combobox')
    const active = () => input.getAttribute('aria-activedescendant')
    const rest = restTarget()
    expect(rest).toBeTruthy()
    fireEvent.mouseMove(screen.getAllByRole('option')[2], { clientX: 600, clientY: 300 })
    fireEvent.mouseMove(screen.getAllByRole('option')[2], { clientX: 602, clientY: 302 })
    expect(active()).toBe('command-bar-row-2')
    fireEvent.mouseMove(rest, { clientX: 650, clientY: 340 })
    fireEvent.change(input, { target: { value: 'new session' } })
    expect(active()).toBe('command-bar-row-0')
    const rows = screen.getAllByRole('option')
    fireEvent.mouseOver(rows[1], { clientX: 650, clientY: 340 })
    fireEvent.mouseEnter(rows[1], { clientX: 650, clientY: 340 })
    fireEvent.mouseMove(rows[1], { clientX: 650, clientY: 340 })
    expect(active()).toBe('command-bar-row-0')
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
    const src = overlaySource()
    expect(src).toContain('icon: appIcon(target)')
  })

  it('keeps the recents provider inert until the sessions view is entered', () => {
    // Same leak class as the sessions provider: a listing engine that fetches on
    // construction would put a request behind merely opening the root, which is the
    // one thing this surface promises not to do.
    const src = overlaySource()
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
    // The row unwraps the dispatch (the composer is focused once the switch lands),
    // so the mock answers with the thunk's shape.
    resolvingDispatch()
    fireEvent.mouseDown(rows[0])
    expect(dispatch).toHaveBeenCalledWith({ type: 'switchSlot', key: 'slot-a', announceOnMissing: true, gesture: true })
  })

  it('never drops a blocked session: past the cap a final row counts the rest and reveals them', () => {
    // Eight sessions wait on the reader. The section keeps its cap of six so it stays
    // a glance, but the seventh and eighth must not vanish from both groups: a row at
    // the end says how many more are waiting, and pressing it shows every one.
    storeState.dashboard.slots = Array.from({ length: 8 }, (_, i) => ({
      key: `slot-${i}`,
      title: `Blocked ${i}`,
      pending_approval: true,
      messages: 2,
    }))
    const onClose = mount()
    const blockedTitles = () =>
      screen.getAllByRole('option').map(r => r.textContent ?? '').filter(t => /Blocked \d/.test(t))
    expect(blockedTitles()).toHaveLength(6)
    const more = rowByText('+2 more blocked')
    expect(more).toBeTruthy()
    // It ends the section: the row right after it is no longer a blocked session.
    const rows = screen.getAllByRole('option')
    expect(rows.indexOf(more)).toBe(6)
    fireEvent.mouseDown(more)
    expect(blockedTitles()).toHaveLength(8)
    expect(screen.queryByText('+2 more blocked')).toBeNull()
    // Revealing is in place: the bar stays open and no session was switched to.
    expect(onClose).not.toHaveBeenCalled()
    expect(dispatch).not.toHaveBeenCalled()
  })

  it('shows no attention section when nothing is waiting on the user', () => {
    // A section that is always present is a section the user learns to skip; the whole
    // value of this one is that its presence means something. A RUNNING session is not
    // waiting on anyone, so it must not be lifted here either — it belongs to the
    // recent group below, which is a switch list and makes no claim on the reader.
    storeState.dashboard.slots = [
      { key: 'slot-c', title: 'Refactor the parser', running: true, messages: 9 },
      { key: 'slot-d', title: 'Idle thread', messages: 3 },
    ]
    mount()
    expect(screen.queryByText('Needs You')).toBeNull()
    expect(screen.queryByText('Approve')).toBeNull()
    expect(screen.queryByText('Answer')).toBeNull()
    expect(rowByText('Refactor the parser')).toBeTruthy()
  })

  it('offers the sessions the reader was last in, newest first, above the commands', () => {
    // The most common reason this surface is opened: get me back to what I was doing.
    // It used to mean entering the sessions view and typing a name from memory, so the
    // answer the store already held cost two steps and a recall.
    storeState.dashboard.slots = [
      { key: 'slot-old', title: 'Last week thread', messages: 3, last_activity_ts: 1_000 },
      { key: 'slot-new', title: 'This morning thread', messages: 5, last_activity_ts: 3_000 },
      { key: 'slot-mid', title: 'Yesterday thread', messages: 2, last_activity_ts: 2_000 },
    ]
    mount()
    const rows = screen.getAllByRole('option')
    expect(screen.getByText('Recent sessions')).toBeTruthy()
    // Order is the claim — recency, not the alphabet and not the frecency store.
    expect(rows[0].textContent).toContain('This morning thread')
    expect(rows[1].textContent).toContain('Yesterday thread')
    expect(rows[2].textContent).toContain('Last week thread')
    // Ahead of the commands, which keep their own block underneath.
    expect(rows[3].textContent).toMatch(/New Session|Search Sessions|Toggle Theme/)
    // A session row carries no static kind word; its column is for live state.
    expect(rows[0].textContent).not.toContain('Command')
    // Activating one switches to it, the same way every other surface opens a session.
    resolvingDispatch()
    fireEvent.mouseDown(rows[0])
    expect(dispatch).toHaveBeenCalledWith({
      type: 'switchSlot',
      key: 'slot-new',
      announceOnMissing: true,
      gesture: true,
    })
  })

  it('caps the recent group at three, leaving the rest to the sessions view', () => {
    // The group's value is that it needs no reading. A fourth row buys a little more
    // coverage and spends that property, and the whole corpus is one row below.
    storeState.dashboard.slots = Array.from({ length: 7 }, (_, i) => ({
      key: `slot-${i}`,
      title: `Thread ${i}`,
      messages: 2,
      last_activity_ts: 1_000 - i,
    }))
    mount()
    const titles = screen.getAllByRole('option').map(r => r.textContent ?? '')
    expect(titles.filter(t => /Thread \d/.test(t))).toHaveLength(3)
    expect(titles[0]).toContain('Thread 0')
    expect(screen.queryByText('Thread 3')).toBeNull()
    expect(rowByText('Search Sessions')).toBeTruthy()
  })

  it('keeps a session out of the recent group when it is already in the one above', () => {
    // A session waiting on the reader is on screen with its pill. A second, quieter
    // copy of it three rows down adds nothing and makes the page look longer than the
    // number of sessions it is actually about.
    storeState.dashboard.slots = [
      { key: 'slot-a', title: 'Deploy the pricing service', pending_approval: true, messages: 4, last_activity_ts: 3_000 },
      { key: 'slot-b', title: 'Idle thread', messages: 2, last_activity_ts: 1_000 },
    ]
    mount()
    expect(screen.getAllByText('Deploy the pricing service')).toHaveLength(1)
    expect(rowByText('Idle thread')).toBeTruthy()
  })

  it('leaves an empty untitled session out of the recent group', () => {
    // Switching into a blank chat is what New Session is for, and one blank row is
    // indistinguishable from another — so they would fill the group with rows that
    // cannot be told apart.
    storeState.dashboard.slots = [
      { key: 'slot-blank', title: 'New Session…', messages: 0, last_activity_ts: 9_000 },
      { key: 'slot-real', title: 'Real thread', messages: 4, last_activity_ts: 1_000 },
    ]
    mount()
    const rows = screen.getAllByRole('option')
    expect(rows[0].textContent).toContain('Real thread')
    // The New Session COMMAND is still there; what is absent is a session row for the
    // blank slot, which would be a second row reading the same way.
    expect(screen.getAllByText(/New Session/)).toHaveLength(1)
  })

  it('labels an untitled attention row "Untitled session", not its key (#20909)', () => {
    storeState.dashboard.slots = [
      { key: 'chat-4-1785440411', title: '', pending_approval: true, messages: 4 },
      { key: 'slot-b', title: 'Draft the launch email', needs_input: true, messages: 2 },
    ]
    mount()
    const rows = screen.getAllByRole('option')
    const untitled = rows.find(r => r.textContent?.includes('Approve'))!
    expect(untitled.textContent).toContain('Untitled session')
    expect(untitled.textContent).not.toContain('chat-4-1785440411')
    expect(rows.find(r => r.textContent?.includes('Answer'))!.textContent).toContain('Draft the launch email')
    // Activation still targets the session's own key.
    resolvingDispatch()
    fireEvent.mouseDown(untitled)
    expect(dispatch).toHaveBeenCalledWith({ type: 'switchSlot', key: 'chat-4-1785440411', announceOnMissing: true, gesture: true })
  })

  it('labels an untitled recent row "Untitled session", not its key (#20910)', () => {
    storeState.dashboard.slots = [
      { key: 'chat-5-1785440411', title: '', messages: 3, last_activity_ts: 3_000 },
      { key: 'slot-named', title: 'Named thread', messages: 2, last_activity_ts: 1_000 },
    ]
    mount()
    const rows = screen.getAllByRole('option')
    // Recency order is unchanged: the untitled, newer session still leads.
    expect(rows[0].textContent).toContain('Untitled session')
    expect(rows[0].textContent).not.toContain('chat-5-1785440411')
    expect(rows[1].textContent).toContain('Named thread')
    resolvingDispatch()
    fireEvent.mouseDown(rows[0])
    expect(dispatch).toHaveBeenCalledWith({ type: 'switchSlot', key: 'chat-5-1785440411', announceOnMissing: true, gesture: true })
  })

  it('still finds an untitled session row by its key once the key is no longer shown', () => {
    storeState.dashboard.slots = [
      { key: 'chat-5-1785440411', title: '', messages: 3, last_activity_ts: 3_000 },
      { key: 'chat-6-1785440411', title: '', pending_approval: true, messages: 2 },
    ]
    mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'chat-5-1785440411' } })
    const hit = screen.getAllByRole('option').find(r => r.textContent?.includes('Untitled session'))
    expect(hit).toBeTruthy()
    resolvingDispatch()
    fireEvent.mouseDown(hit!)
    expect(dispatch).toHaveBeenCalledWith({ type: 'switchSlot', key: 'chat-5-1785440411', announceOnMissing: true, gesture: true })
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
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) }))
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({
        type: 'switchSlot',
        key: 'slot-new',
        keepTargetOnMissing: true,
      }),
    )
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'setPendingInput', text: 'why did the deploy stall' }))
    expect(enterInsertOrNewSession).not.toHaveBeenCalled()
    // NOT filed into a command folder: this is a sentence the reader wrote, so it
    // belongs wherever they are working rather than under a command's name.
    expect(fileSessionInCommandFolder).not.toHaveBeenCalled()
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
    const src = overlaySource()
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
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) }))
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
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) }))
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
    const src = overlaySource()
    expect(src).toContain('max-w-[680px]')
    expect(src).not.toContain('max-w-xl')
    expect(src).toContain('animate-scale-in')
    expect(src).toMatch(/px-3 py-2 cursor-pointer text-\[13px\]/)
  })
})


/**
 * What the root list looks like once the reader has typed.
 *
 * Group order is the idle page's filing order — the product decision about what a
 * launcher OPENS on. A query is a different question, so these pin the two halves:
 * a typed query is one list ranked by match, and the section headers that name the
 * blocks go with the blocks.
 */
describe('CommandBarOverlay root ordering under a query', () => {
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
    window.localStorage.clear()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  /** The app the reader is typing toward, as the apps API returns it. */
  const DEV_FLEET = {
    name: 'dev-fleet',
    displayName: 'Dev Fleet',
    enabled: true,
    origin: 'registry',
    manifest: { ui: { pages: [{ label: 'Dev Fleet', route: '/apps/dev-fleet' }] } },
  }

  /** A DIFFERENT app, contributing the command that used to outrank it. */
  const PR_BULK_OPS = {
    name: 'pr-bulk-ops',
    displayName: 'PR Bulk Ops',
    enabled: true,
    origin: 'registry',
    manifest: {
      contributes: {
        commands: [
          {
            id: 'approve-merge-all',
            title: 'Approve and merge all PRs',
            subtitle:
              'Merge every ready pull request behind a link, approving first where allowed',
            prompt: 'Approve and merge every ready pull request.',
          },
        ],
      },
    },
  }

  function mountWith(apps: unknown[]) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], apps)
    render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={vi.fn()} />
      </QueryClientProvider>,
    )
  }

  /** The section headers the list is rendering, top to bottom. */
  const renderedHeaders = () =>
    Array.from(document.querySelectorAll('div.uppercase.tracking-wide.text-muted')).map(
      el => el.textContent ?? '',
    )

  const rowIndex = (re: RegExp) =>
    screen.getAllByRole('option').findIndex(el => re.test(el.textContent ?? ''))

  it('puts the app the reader is spelling out above a command that only matched its subtitle', () => {
    // The reported bug. `dev fle` scores the Dev Fleet app 216 and this contributed
    // command 60 — the command's own title does not match at all, only its subtitle —
    // and the command was still shown first, because `commands` files before `apps`.
    mountWith([DEV_FLEET, PR_BULK_OPS])
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'dev fle' } })
    const app = rowIndex(/Dev Fleet/)
    const command = rowIndex(/Approve and merge all PRs/)
    // Both rows are on screen: the command still matches, it just ranks below.
    expect(app).toBeGreaterThanOrEqual(0)
    expect(command).toBeGreaterThan(app)
  })

  it('names a recent session in its own column once the headers are gone', () => {
    // The headers are what named a recent row, and `kindLabel` deliberately returns
    // nothing for one (its column belongs to live state). An IDLE session has no live
    // state either, so under a query the row would be a glyph and a title while every
    // row beside it still showed its word. The group's own name stands in there.
    storeState.dashboard = {
      slots: [{ key: 'chat-7', title: 'Fleet notes', messages: 4, running: false }],
      unreadSlots: [],
    }
    mountWith([DEV_FLEET, PR_BULK_OPS])
    const input = screen.getByRole('combobox')
    // Idle page: the header above the row says it, so the column stays empty.
    const idleRow = screen.getAllByRole('option').find(el => /Fleet notes/.test(el.textContent ?? ''))
    expect(idleRow).toBeTruthy()
    expect(renderedHeaders()).toContain('Recent sessions')
    expect(idleRow?.textContent).not.toContain('Recent sessions')
    // Under a query the header is gone and the row carries the name instead.
    fireEvent.change(input, { target: { value: 'fleet' } })
    expect(renderedHeaders()).toEqual([])
    const queriedRow = screen
      .getAllByRole('option')
      .find(el => /Fleet notes/.test(el.textContent ?? ''))
    expect(queriedRow).toBeTruthy()
    expect(queriedRow?.textContent).toContain('Recent sessions')
  })

  it('drops the group headers while a query ranks the list, and restores them when it clears', () => {
    mountWith([DEV_FLEET, PR_BULK_OPS])
    const input = screen.getByRole('combobox')
    expect(renderedHeaders()).toContain('Commands')
    fireEvent.change(input, { target: { value: 'dev fle' } })
    // One ranked list: a group can now appear, be left and appear again, so a header
    // per transition would print the same word twice over rows it does not describe.
    expect(renderedHeaders()).toEqual([])
    // The headers belong to the idle page, so clearing the query brings them back.
    fireEvent.change(input, { target: { value: '' } })
    expect(renderedHeaders()).toContain('Commands')
  })
})

describe('Crew Mode workers stay out of the Command Bar (edge review R1 X3)', () => {
  it('lists neither a waiting nor a recent worker, only the root and plain chats', () => {
    // A worker is its conductor's to show: the person's lists, this one included,
    // leave it out even while it waits on a question or was visited last.
    storeState.dashboard = {
      slots: [
        { key: 'root', title: 'Ship the review', crew_mode_root: true, messages: 3 },
        { key: 'w-ask', title: 'Approval: list empty workspace', crew_root: 'root', needs_input: true, messages: 2 },
        { key: 'w-done', title: 'Done: answer cat', crew_root: 'root', messages: 4, last_activity_ts: 9_000 },
        { key: 'plain', title: 'Fleet notes', pending_approval: true, messages: 2 },
      ],
      unreadSlots: ['w-done'],
    }
    mount()
    const text = screen.getAllByRole('option').map(r => r.textContent ?? '').join('\n')
    expect(text).toContain('Fleet notes')
    expect(text).toContain('Ship the review')
    expect(text).not.toContain('Approval: list empty workspace')
    expect(text).not.toContain('Done: answer cat')
  })

  it("lifts a root into Needs You with the Approve pill when one of its hidden workers waits on a tool approval", () => {
    // The worker is not a row, so what it owes the person rides on its root: the
    // same count the fleet page uses (`crewWaitingApprovals`). Red without the
    // root-row lift: an idle root has no pill of its own.
    storeState.dashboard = {
      slots: [
        { key: 'root', title: 'Ship the review', crew_mode_root: true, messages: 3 },
        { key: 'w-tool', title: 'Worker on a tool', crew_root: 'root', pending_approval: true, messages: 2 },
        { key: 'w-tool-2', title: 'Second worker', crew_root: 'root', pending_approval: true, messages: 2 },
        { key: 'quiet-root', title: 'Quiet crew', crew_mode_root: true, messages: 1 },
        { key: 'w-idle', title: 'Idle worker', crew_root: 'quiet-root', messages: 1 },
      ],
      unreadSlots: [],
    }
    mount()
    const options = screen.getAllByRole('option')
    const lifted = options.find(o => o.getAttribute('data-row-id') === 'attention:root' || o.id === 'attention:root' || (o.textContent ?? '').includes('Ship the review'))
    expect(lifted?.textContent).toContain('Approve')
    expect(lifted?.textContent).toContain('Crew members waiting for a tool decision: 2')
    const quiet = options.find(o => (o.textContent ?? '').includes('Quiet crew'))
    expect(quiet?.textContent ?? '').not.toContain('Approve')
    expect(options.map(o => o.textContent ?? '').join('\n')).not.toContain('Worker on a tool')
  })

  it('keeps a closed root\'s worker in Needs You with its own Approve pill', () => {
    // The root was closed, so no row carries the count and no Crew panel lists the
    // worker: the worker is a row again, and its approval still reaches the person.
    storeState.dashboard = {
      slots: [
        { key: 'w-orphan', title: 'Worker of a closed crew', crew_root: 'closed-root', pending_approval: true, messages: 2 },
      ],
      unreadSlots: [],
    }
    mount()
    const row = screen.getAllByRole('option').find(o => (o.textContent ?? '').includes('Worker of a closed crew'))
    expect(row?.textContent).toContain('Approve')
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
