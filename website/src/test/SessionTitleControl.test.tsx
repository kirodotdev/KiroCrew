/**
 * SessionTitleControl — the shared header title control (#9727).
 *
 * Four contracts, pinned once at the component level so both hosts (the
 * single-session header and every split-view pane) inherit them:
 *   (a) click title -> inline editor; Enter commits via api.renameSlot and
 *       writes the store optimistically;
 *   (b) Escape closes without calling the API or touching the store;
 *   (c) Sparkles -> api.generateTitle(slot) and the returned title lands in
 *       the store;
 *   (d) an API failure is reported to the host's onError; a refused rename
 *       reverts the optimistic title to the server truth, or — when the
 *       recovery re-read fails too (#10203 double failure) — locally to the
 *       last confirmed title, while a stale attempt never overwrites a newer
 *       one;
 *   (e) closing the editor never strands focus on `document.body`: Enter and
 *       Escape return focus to the title trigger, the composer never gains
 *       focus from the committing Enter (its Enter SENDS), a pointer blur
 *       moves nothing, and a confirmed rename is announced through a polite
 *       status region.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider, useSelector } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { RootState } from '../store'
import { createTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    renameSlot: vi.fn().mockResolvedValue({}),
    generateTitle: vi.fn().mockResolvedValue({ title: 'Generated title' }),
    chatSlots: vi.fn().mockResolvedValue([]),
  },
}))

import SessionTitleControl, { ENTER_RELEASE_GRACE_MS } from '../pages/chat/SessionTitleControl'
import { MOVE_UNDO_MS } from '../components/MoveUndoBar'
import { api } from '../api/client'

const SLOT = 'pane-a'
const TITLE = 'Alpha session'
const REGEN = 'Regenerate title with LLM — the current name can be restored with Undo'
// The trigger's accessible name carries the ACTION and the title (WCAG 2.5.3
// keeps the visible title inside the name).
const RENAME = `${TITLE} (rename session)`

function makeStore(memoryMode?: string) {
  return createTestStore({
    dashboard: {
      status: null, connected: true, slotsLoaded: true,
      slots: [
        { key: SLOT, title: TITLE, messages: 0, running: false, mode: '', ...(memoryMode ? { memory_mode: memoryMode } : {}) },
        { key: 'other', title: 'Other session', messages: 0, running: false, mode: '' },
      ],
      unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
    } as unknown as RootState['dashboard'],
  })
}

function renderControl(opts: { onError?: (m: string, t: string) => void; memoryMode?: string; compact?: boolean } = {}) {
  const store = makeStore(opts.memoryMode)
  // Mirrors the real hosts: the title prop follows the store. `slot` is a
  // prop so a test can re-target the same instance like the main header does.
  // `compact` (the pane header's typography) is the default here; the main
  // header's variant is opted into where a contract must hold for both hosts.
  const Host = ({ slot }: { slot: string }) => {
    const title = useSelector((s: RootState) => s.dashboard.slots.find((x) => x.key === slot)?.title ?? slot)
    return <SessionTitleControl slotKey={slot} title={title} compact={opts.compact ?? true} onError={opts.onError} />
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (slot: string) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div className="group/header"><Host slot={slot} /></div>
        {/* The page's composer, with the `data-composer-input` hook production
            probes: the (e) tests assert it never gains focus from a rename. */}
        <textarea data-composer-input aria-label="Message input" />
        <button type="button" data-testid="elsewhere">Elsewhere</button>
      </Provider>
    </QueryClientProvider>
  )
  const utils = render(tree(SLOT))
  const rerenderWithSlot = (slot: string) => act(() => { utils.rerender(tree(slot)) })
  return { store, rerenderWithSlot, ...utils }
}

const storeTitle = (store: ReturnType<typeof makeStore>) =>
  store.getState().dashboard.slots.find((s) => s.key === SLOT)?.title

const openEditor = () => {
  act(() => { fireEvent.click(screen.getByText(TITLE)) })
  return screen.getByDisplayValue(TITLE) as HTMLInputElement
}
const openEditorFor = (title: string) => {
  act(() => { fireEvent.click(screen.getByText(title)) })
  return screen.getByDisplayValue(title) as HTMLInputElement
}

// What the server answers when the control re-pulls slots after a failure.
const serverSlots = (title: string) => [{ key: SLOT, title, messages: 0, running: false, mode: '' }]

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.renameSlot).mockResolvedValue({})
  vi.mocked(api.generateTitle).mockResolvedValue({ title: 'Generated title' })
  vi.mocked(api.chatSlots).mockResolvedValue(serverSlots(TITLE))
})

describe('SessionTitleControl', () => {
  it('renders the title as a button with the regenerate action beside it', () => {
    renderControl()
    expect(screen.getByRole('button', { name: RENAME })).toBeTruthy()
    expect(screen.getByRole('button', { name: REGEN })).toBeTruthy()
  })

  it('(a) click -> editor seeded with the title; Enter commits through renameSlot and the store', async () => {
    const { store } = renderControl()
    const input = openEditor()
    expect(input.value).toBe(TITLE)
    act(() => { fireEvent.change(input, { target: { value: '  Renamed alpha  ' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    // Enter blurs; the commit rides the blur (same path as tapping away).
    act(() => { fireEvent.blur(input) })
    expect(api.renameSlot).toHaveBeenCalledWith(SLOT, 'Renamed alpha')
    expect(storeTitle(store)).toBe('Renamed alpha')
    await waitFor(() => expect(screen.queryByDisplayValue('  Renamed alpha  ')).toBeNull())
  })

  // Three nightly GUI testers read the open editor as "highlighted, not
  // editable" (#13050, #12772, #13325): the input used to strip every piece of the
  // shared Input chrome and sit inside the same hover pill as the read-only
  // title, with the whole name selected -- and a range selection paints no
  // caret in any engine. These pin the affordance the fix adds. Static
  // contracts on the class strings: happy-dom lays nothing out and computes
  // no `:focus-visible`, so the measured heights live in the PR's captures.
  const READ_ONLY_PILL = 'cursor-text flex min-w-0 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md group-hover/header:bg-bg-hover focus-within:bg-bg-hover transition-colors'
  const INPUT_CHROME = ['border', 'border-accent', 'bg-bg-elevated', 'focus-ring']
  // The overrides that used to strip the Input's chrome at this call site.
  const STRIPPED_CHROME = ['bg-transparent', 'border-0', 'rounded-none', 'p-0', 'focus:!shadow-none', 'focus-visible:border-b', 'focus-visible:border-accent']
  const classes = (el: Element) => el.className.split(/\s+/).filter(Boolean)

  it('opens as an unmistakable text input: the shared Input border, background and focus ring, with a caret at the end', () => {
    renderControl()
    const input = openEditor()
    // Some engines leave a programmatic focus with the selection at the
    // start (and the field scrolled); the focus the browser delivers on open
    // is what the control places the caret on.
    act(() => { input.setSelectionRange(0, 0); input.scrollLeft = 40 })
    act(() => { fireEvent.focus(input) })
    // A collapsed selection at the end: a caret the user can see, nothing
    // selected. (Select-all would replace the title on the first keystroke,
    // but a range selection suppresses the caret everywhere, and the caret is
    // the cue the testers missed.) The field itself opens scrolled to its
    // start, so a name wider than the box still shows its beginning.
    expect(input.selectionStart).toBe(TITLE.length)
    expect(input.selectionEnd).toBe(TITLE.length)
    expect(input.scrollLeft).toBe(0)
    for (const c of INPUT_CHROME) expect(classes(input)).toContain(c)
    for (const c of STRIPPED_CHROME) expect(classes(input)).not.toContain(c)
    // The editor IS the box: no hover pill painted behind it any more.
    expect(classes(input.parentElement!)).not.toContain('bg-bg-hover')
  })

  it.each([
    ['split-view pane header (compact)', true],
    ['single-session header', false],
  ])('%s: the editor keeps the read-only title\'s type and box, so the header does not move when editing starts or ends', (_host, compact) => {
    renderControl({ compact })
    const pill = screen.getByRole('button', { name: RENAME }).parentElement!
    expect(pill.className).toBe(READ_ONLY_PILL)
    const label = screen.getByText(TITLE)
    const typeClasses = classes(label).filter((c) => /^(text-|font-|session-header-title)/.test(c))
    // Coherence check: the read-only title's size, weight and colour are what gets mirrored.
    expect(typeClasses).toEqual(expect.arrayContaining(['font-semibold', compact ? 'text-[13px]' : 'text-sm']))
    const input = openEditor()
    for (const c of typeClasses) expect(classes(input)).toContain(c)
    for (const c of INPUT_CHROME) expect(classes(input)).toContain(c)
    // The pill's padding (py-0.5 = 2px, px-1.5 = 6px) becomes the editor's
    // 1px border + 1px / 5px padding, and the editing wrapper adds none of
    // its own -- the box the title sits in keeps its size in both states.
    expect(classes(input)).toEqual(expect.arrayContaining(['py-px', 'px-[5px]', 'rounded-l-[2px]', 'rounded-r-md']))
    expect(classes(input.parentElement!).some((c) => /^-?p[xytrbl]?-/.test(c))).toBe(false)
    // Same width rule as the read-only title on the main header.
    expect(classes(input).includes('md:max-w-[50vw]')).toBe(!compact)
  })

  it('caps the draft at the 200 characters the rename route keeps, so no typed tail is cut server-side', () => {
    renderControl()
    const input = openEditor()
    // chat_title.py slices `title.strip()[:200]`; a longer draft would show
    // whole in the optimistic write and lose its tail on the next re-read.
    expect(input.maxLength).toBe(200)
  })

  it('an unchanged or blank draft commits nothing', () => {
    const { store } = renderControl()
    let input = openEditor()
    act(() => { fireEvent.blur(input) })
    expect(api.renameSlot).not.toHaveBeenCalled()
    input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: '   ' } }) })
    act(() => { fireEvent.blur(input) })
    expect(api.renameSlot).not.toHaveBeenCalled()
    expect(storeTitle(store)).toBe(TITLE)
  })

  it('an untouched editor never writes, even after the title moved on while it was open', () => {
    const { store } = renderControl()
    openEditor()
    // A generated / remote rename lands while the editor is open.
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: SLOT, title: 'Renamed elsewhere' } }) })
    act(() => { fireEvent.blur(screen.getByDisplayValue(TITLE)) })
    expect(api.renameSlot).not.toHaveBeenCalled()
    expect(storeTitle(store)).toBe('Renamed elsewhere')
  })

  it('(b) Escape restores the title without calling the API', () => {
    const { store } = renderControl()
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Abandoned' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    // A browser may still blur the input as it unmounts; that blur must not commit.
    act(() => { fireEvent.blur(input) })
    expect(api.renameSlot).not.toHaveBeenCalled()
    expect(storeTitle(store)).toBe(TITLE)
    expect(screen.getByText(TITLE)).toBeTruthy()
  })

  it('(c) Sparkles calls generateTitle(slot) and applies the returned title', async () => {
    const { store } = renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    expect(api.generateTitle).toHaveBeenCalledWith(SLOT)
    await waitFor(() => expect(storeTitle(store)).toBe('Generated title'))
    // Spinner gone; the Auto-title button's place is taken by the Undo offer.
    await waitFor(() => expect(screen.getByRole('button', { name: `Undo: ${TITLE}` })).toBeTruthy())
    expect(screen.queryByText('Auto-title…')).toBeNull()
  })

  it('offers a one-shot Undo after a generated title lands, which restores the previous name through the rename path', async () => {
    const { store } = renderControl()
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    await waitFor(() => expect(storeTitle(store)).toBe('Generated title'))
    // The way back is visible without hovering and names what it restores --
    // and it takes the Auto-title button's place, so the row keeps two actions.
    const undo = await screen.findByRole('button', { name: `Undo: ${TITLE}` })
    expect(screen.queryByRole('button', { name: REGEN })).toBeNull()
    await act(async () => { fireEvent.click(undo) })
    expect(storeTitle(store)).toBe(TITLE)
    expect(api.renameSlot).toHaveBeenCalledWith(SLOT, TITLE)
    // One shot: used, it is gone.
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
  })

  it('the Undo offer ends when the title moves on or the user starts another attempt', async () => {
    const { store } = renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    await screen.findByRole('button', { name: `Undo: ${TITLE}` })
    // A remote / SSE rename lands: the generated title is no longer on screen.
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: SLOT, title: 'Renamed elsewhere' } }) })
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
    // ...and the offer is GONE, not hidden: the title bouncing back to the
    // generated string must not resurrect an Undo that would overwrite the
    // intervening rename with the pre-generation name.
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: SLOT, title: 'Generated title' } }) })
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: SLOT, title: 'Renamed elsewhere' } }) })
    // A second generate offers the NEW previous name, not the old one.
    vi.mocked(api.generateTitle).mockResolvedValueOnce({ title: 'Second generated' })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    await screen.findByRole('button', { name: 'Undo: Renamed elsewhere' })
    // A manual rename ends the offer.
    const input = openEditorFor('Second generated')
    act(() => { fireEvent.change(input, { target: { value: 'Typed by hand' } }) })
    act(() => { fireEvent.blur(input) })
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
  })

  it('the Undo baseline is the title on screen when the generated one lands, not the one at click time', async () => {
    let resolve!: (v: { title: string }) => void
    vi.mocked(api.generateTitle).mockReturnValueOnce(new Promise((r) => { resolve = r }))
    const { store } = renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    // A rename from elsewhere (another pane / client via SSE) lands while generating.
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: SLOT, title: 'Renamed meanwhile' } }) })
    await act(async () => { resolve({ title: 'Generated title' }) })
    // Undo brings back what the generated title actually replaced.
    await screen.findByRole('button', { name: 'Undo: Renamed meanwhile' })
    expect(screen.queryByRole('button', { name: `Undo: ${TITLE}` })).toBeNull()
  })

  it('retargeting the control to another slot ends the offer, and a late completion for the old slot creates none', async () => {
    let resolve!: (v: { title: string }) => void
    vi.mocked(api.generateTitle).mockReturnValueOnce(new Promise((r) => { resolve = r }))
    const { rerenderWithSlot } = renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    // The main header switches sessions while the old slot is still generating.
    rerenderWithSlot('other')
    await act(async () => { resolve({ title: 'Generated title' }) })
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
    // Back to the original slot: still no offer -- the user never saw the title land.
    rerenderWithSlot(SLOT)
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
  })

  it('offers Undo even when the server pushed the generated title over the slots stream before the POST resolved', async () => {
    let resolve!: (v: { title: string }) => void
    vi.mocked(api.generateTitle).mockReturnValueOnce(new Promise((r) => { resolve = r }))
    const { store } = renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    // The stream lands first: the store already shows the generated title.
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: SLOT, title: 'Generated title' } }) })
    await act(async () => { resolve({ title: 'Generated title' }) })
    // The baseline falls back to the title at click time, not the (already generated) store title.
    await screen.findByRole('button', { name: `Undo: ${TITLE}` })
  })

  it('the Undo offer is a window: it closes on its own and when the editor is opened', async () => {
    const { store } = renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    await screen.findByRole('button', { name: `Undo: ${TITLE}` })
    // Opening the editor ("I have moved on") ends the offer; Escape leaves the generated title.
    const input = openEditorFor('Generated title')
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    expect(storeTitle(store)).toBe('Generated title')
    // Escape hands focus back to the title trigger, which is INSIDE the row and
    // so holds the window open (the focus hold below). Move focus out first, as
    // a user who has moved on would, so the timed close is what is measured.
    await act(async () => { await new Promise(requestAnimationFrame) })
    act(() => { (document.activeElement as HTMLElement | null)?.blur() })
    // Time-based close.
    vi.useFakeTimers()
    try {
      vi.mocked(api.generateTitle).mockResolvedValueOnce({ title: 'Second generated' })
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
      await act(async () => { await Promise.resolve() })
      expect(screen.getByRole('button', { name: 'Undo: Generated title' })).toBeTruthy()
      act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS - 1) })
      expect(screen.getByRole('button', { name: 'Undo: Generated title' })).toBeTruthy()
      act(() => { vi.advanceTimersByTime(2) })
      expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
      // The Auto-title button is back in its place.
      expect(screen.getByRole('button', { name: REGEN })).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('the Undo window is held open while the pointer is over the row or focus is inside it, and resumes where it stopped', async () => {
    const { container } = renderControl()
    const row = () => screen.getByRole('button', { name: /Undo|Regenerate/ }).closest('div.cursor-text') as HTMLElement
    vi.useFakeTimers()
    try {
      // The pointer is on the row when the offer lands -- it is on the button
      // that was just pressed -- so the offer holds a FULL window until it lifts.
      act(() => { fireEvent.mouseEnter(row()) })
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
      await act(async () => { await Promise.resolve() })
      expect(screen.getByRole('button', { name: `Undo: ${TITLE}` })).toBeTruthy()
      act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 3) })
      expect(screen.getByRole('button', { name: `Undo: ${TITLE}` })).toBeTruthy()
      act(() => { fireEvent.mouseLeave(row()) })
      // Part of the window runs, then a hold freezes the remainder...
      act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS - 1000) })
      act(() => { fireEvent.focus(screen.getByRole('button', { name: `Undo: ${TITLE}` })) })
      act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 3) })
      expect(screen.getByRole('button', { name: `Undo: ${TITLE}` })).toBeTruthy()
      // A pointer that enters and leaves meanwhile does not release the hold
      // focus still owns: the two are tracked apart and either one holds.
      act(() => { fireEvent.mouseEnter(row()) })
      act(() => { fireEvent.mouseLeave(row()) })
      act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 3) })
      expect(screen.getByRole('button', { name: `Undo: ${TITLE}` })).toBeTruthy()
      // ...and the remainder, not a fresh window, is what runs after the hold lifts.
      act(() => { fireEvent.blur(screen.getByRole('button', { name: `Undo: ${TITLE}` })) })
      act(() => { vi.advanceTimersByTime(999) })
      expect(screen.getByRole('button', { name: `Undo: ${TITLE}` })).toBeTruthy()
      act(() => { vi.advanceTimersByTime(2) })
      expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
      expect(container.querySelector('div.cursor-text')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keyboard focus reveals what hover reveals: the Pen on a focus-visible title, the Auto-title button on its own focus', () => {
    renderControl()
    // Static contract on the class strings -- happy-dom does not compute :focus-visible.
    const titleBtn = screen.getByRole('button', { name: RENAME })
    expect(titleBtn.className).toContain('group/title')
    const pen = titleBtn.querySelector('svg.lucide-pen') as SVGElement
    expect(pen.getAttribute('class')).toContain('group-focus-visible/title:opacity-60')
    const regen = screen.getByRole('button', { name: REGEN })
    expect(regen.className).toContain('focus-visible:opacity-100')
    // Both are reachable by Tab (not tabIndex -1).
    expect(titleBtn.getAttribute('tabindex')).toBe('0')
    expect(regen.getAttribute('tabindex')).not.toBe('-1')
  })

  it('no Undo is offered when the generated title equals the current one', async () => {
    vi.mocked(api.generateTitle).mockResolvedValueOnce({ title: TITLE })
    renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    await waitFor(() => expect(screen.getByRole('button', { name: REGEN })).toBeTruthy())
    expect(screen.queryByRole('button', { name: /^Undo/ })).toBeNull()
  })

  it('shows the spinner instead of the button while a title is generating', async () => {
    let resolve!: (v: { title: string }) => void
    vi.mocked(api.generateTitle).mockReturnValueOnce(new Promise((r) => { resolve = r }))
    renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    expect(screen.queryByRole('button', { name: REGEN })).toBeNull()
    // The busy state is named, not just drawn: the label stays beside the spinner.
    expect(screen.getByText('Auto-title…').closest('[role="status"]')).not.toBeNull()
    await act(async () => { resolve({ title: 'Done' }) })
    // Settled: the busy status is gone and the Undo offer stands in the button's place.
    await waitFor(() => expect(screen.queryByText('Auto-title…')).toBeNull())
    expect(screen.getByRole('button', { name: `Undo: ${TITLE}` })).toBeTruthy()
  })

  it('(d) a rename failure reaches onError and re-pulls the authoritative title', async () => {
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('boom'))
    const onError = vi.fn()
    const { store } = renderControl({ onError })
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Will fail' } }) })
    act(() => { fireEvent.blur(input) })
    // Optimistic first...
    expect(storeTitle(store)).toBe('Will fail')
    await waitFor(() => expect(onError).toHaveBeenCalledWith('boom', "Couldn't rename the session"))
    // ...then whatever the server holds (the refused rename never landed there).
    await waitFor(() => expect(api.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(storeTitle(store)).toBe(TITLE))
    expect(screen.getByText(TITLE)).toBeTruthy()
  })

  it('a slow failure of an OLDER rename does not undo a NEWER rename that landed', async () => {
    let rejectFirst!: (e: Error) => void
    vi.mocked(api.renameSlot)
      .mockReturnValueOnce(new Promise((_r, rej) => { rejectFirst = rej }))
      .mockResolvedValueOnce({})
    // The server accepted the second rename, so that is what a re-pull returns.
    vi.mocked(api.chatSlots).mockResolvedValue(serverSlots('Second'))
    const onError = vi.fn()
    const { store } = renderControl({ onError })
    // Rename 1 — hangs.
    let input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'First' } }) })
    act(() => { fireEvent.blur(input) })
    expect(storeTitle(store)).toBe('First')
    // Rename 2 — succeeds while 1 is still in flight.
    act(() => { fireEvent.click(screen.getByText('First')) })
    input = screen.getByDisplayValue('First') as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Second' } }) })
    act(() => { fireEvent.blur(input) })
    expect(storeTitle(store)).toBe('Second')
    // Rename 1 now fails: the re-pull keeps the server's (newer) title, and
    // the superseded attempt's refusal is NOT reported -- the user already
    // renamed again, and a late notice would undo the clearing the newer
    // attempt's start performed on the host.
    await act(async () => { rejectFirst(new Error('late')) })
    await waitFor(() => expect(api.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(storeTitle(store)).toBe('Second'))
    expect(onError).not.toHaveBeenCalled()
  })

  it('(d) when the recovery re-read fails too, the title reverts locally and the host is still notified', async () => {
    // Transport / auth failures take renameSlot and chatSlots down together
    // (#10203): there is no server truth to re-read, so the control must fall
    // back to the last confirmed title on its own — never leave the refused
    // title on screen — and the host still gets exactly one failure notice.
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('gateway down'))
    vi.mocked(api.chatSlots).mockRejectedValueOnce(new Error('gateway down'))
    const onError = vi.fn()
    const { store } = renderControl({ onError })
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Refused offline' } }) })
    act(() => { fireEvent.blur(input) })
    expect(storeTitle(store)).toBe('Refused offline')
    await waitFor(() => expect(api.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(storeTitle(store)).toBe(TITLE))
    expect(screen.getByText(TITLE)).toBeTruthy()
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith('gateway down', "Couldn't rename the session")
  })

  it('a delayed recovery never overwrites a newer confirmed rename to the same title', async () => {
    // Attempt 1 renames to X and is refused; its re-read is held pending. A
    // newer title lands, then attempt 2 renames to the IDENTICAL X and succeeds.
    // Title equality cannot tell the confirmed X from attempt 1's stale
    // optimistic X — the attempt generation can, so the stale snapshot applies
    // nothing when it finally resolves.
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('refused')).mockResolvedValueOnce({})
    let releaseSlots!: (v: unknown) => void
    vi.mocked(api.chatSlots).mockReturnValueOnce(new Promise((r) => { releaseSlots = r }))
    const { store } = renderControl({ onError: vi.fn() })
    let input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Title X' } }) })
    act(() => { fireEvent.blur(input) })
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: SLOT, title: 'Title Y' } }) })
    act(() => { fireEvent.click(screen.getByText('Title Y')) })
    input = screen.getByDisplayValue('Title Y') as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Title X' } }) })
    act(() => { fireEvent.blur(input) })
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    expect(storeTitle(store)).toBe('Title X')
    act(() => { releaseSlots(serverSlots(TITLE)) })
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    expect(storeTitle(store)).toBe('Title X')
  })

  it('a later failed rename falls back to the last CONFIRMED write, never past it', async () => {
    // A -> X is in flight when X -> Y is committed on top of it. X then
    // succeeds (the server holds X); Y is refused and its recovery re-read
    // fails too. The local fallback must restore X -- the newest title the
    // server confirmed -- not the pre-X baseline A the server no longer holds.
    let resolveX!: (v: unknown) => void
    let rejectY!: (e: unknown) => void
    vi.mocked(api.renameSlot)
      .mockReturnValueOnce(new Promise((r) => { resolveX = r }))
      .mockReturnValueOnce(new Promise((_r, rej) => { rejectY = rej }))
    vi.mocked(api.chatSlots).mockRejectedValue(new Error('gateway down'))
    const onError = vi.fn()
    const { store } = renderControl({ onError })
    let input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Title X' } }) })
    act(() => { fireEvent.blur(input) })
    expect(storeTitle(store)).toBe('Title X')
    act(() => { fireEvent.click(screen.getByText('Title X')) })
    input = screen.getByDisplayValue('Title X') as HTMLInputElement
    act(() => { fireEvent.change(input, { target: { value: 'Title Y' } }) })
    act(() => { fireEvent.blur(input) })
    expect(storeTitle(store)).toBe('Title Y')
    await act(async () => { resolveX({}) })
    await act(async () => { rejectY(new Error('refused')) })
    await waitFor(() => expect(onError).toHaveBeenCalledWith('refused', "Couldn't rename the session"))
    await waitFor(() => expect(storeTitle(store)).toBe('Title X'))
  })

  it('(d) a generate failure reaches onError and leaves the title unchanged', async () => {
    vi.mocked(api.generateTitle).mockRejectedValueOnce(new Error('llm down'))
    const onError = vi.fn()
    const { store } = renderControl({ onError })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    await waitFor(() => expect(onError).toHaveBeenCalledWith('llm down', "Couldn't generate a title"))
    expect(storeTitle(store)).toBe(TITLE)
    expect(screen.getByText(TITLE)).toBeTruthy()
  })

  // (e) Closing the editor never leaves focus on document.body.
  const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })
  // A real, single Enter press: keydown commits (the input blurs), then the
  // key is released.
  const pressEnter = (input: HTMLElement) => {
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    act(() => { fireEvent.keyUp(window, { key: 'Enter' }) })
  }
  const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
  const status = () => screen.getByTestId('rename-status')
  const triggerNamed = (title: string) => screen.getByRole('button', { name: `${title} (rename session)` })

  it('(e) the title trigger is a tabbable button whose name says it renames and carries the title', () => {
    renderControl()
    const trigger = screen.getByRole('button', { name: RENAME })
    expect(trigger.getAttribute('tabindex')).toBe('0')
    expect(trigger.getAttribute('aria-label')).toMatch(/rename/i)
    expect(trigger.getAttribute('aria-label')).toContain(TITLE)
    // The polite region is mounted BEFORE any rename, empty, so its later text
    // change is what gets announced.
    expect(status().getAttribute('role')).toBe('status')
    expect(status().getAttribute('aria-live')).toBe('polite')
    expect(status().textContent).toBe('')
  })

  it('(e) activating the trigger opens the editor with focus in the input', async () => {
    renderControl()
    act(() => { fireEvent.keyDown(screen.getByRole('button', { name: RENAME }), { key: 'Enter' }) })
    const input = screen.getByDisplayValue(TITLE) as HTMLInputElement
    await waitFor(() => expect(document.activeElement).toBe(input))
  })

  it('(e) Enter commits, returns focus to the title trigger (now named for the new title), and announces the confirmed rename', async () => {
    renderControl()
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: '  Renamed alpha  ' } }) })
    pressEnter(input)
    expect(api.renameSlot).toHaveBeenCalledWith(SLOT, 'Renamed alpha')
    // The status region survived the editor's unmount (same element, not a remount).
    const region = status()
    await nextFrame()
    await nextFrame()
    expect(document.activeElement).toBe(triggerNamed('Renamed alpha'))
    expect(document.activeElement).not.toBe(document.body)
    expect(document.activeElement).not.toBe(composer())
    await waitFor(() => expect(status().textContent).toBe('Session renamed to Renamed alpha'))
    expect(status()).toBe(region)
  })

  it('(e) an unchanged draft closed with Enter still returns focus to the trigger, and announces nothing', async () => {
    renderControl()
    const input = openEditor()
    pressEnter(input)
    expect(api.renameSlot).not.toHaveBeenCalled()
    await nextFrame()
    await nextFrame()
    expect(document.activeElement).toBe(triggerNamed(TITLE))
    expect(status().textContent).toBe('')
  })

  it('(e) a refused rename never announces success', async () => {
    vi.mocked(api.renameSlot).mockRejectedValueOnce(new Error('nope'))
    const onError = vi.fn()
    renderControl({ onError })
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Refused' } }) })
    pressEnter(input)
    await waitFor(() => expect(onError).toHaveBeenCalled())
    await nextFrame()
    expect(status().textContent).toBe('')
  })

  it('(e) Escape returns focus to the title trigger with the unchanged title in its name', async () => {
    renderControl()
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Abandoned' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    act(() => { fireEvent.blur(input) })
    expect(api.renameSlot).not.toHaveBeenCalled()
    await nextFrame()
    await nextFrame()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: RENAME }))
    expect(document.activeElement).not.toBe(composer())
  })

  it('(e) a pointer blur commits but redirects focus nowhere', async () => {
    renderControl()
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Tapped away' } }) })
    // The user clicked another control: focus is theirs, on that target.
    const target = screen.getByTestId('elsewhere')
    act(() => { target.focus() })
    act(() => { fireEvent.blur(input) })
    expect(api.renameSlot).toHaveBeenCalledWith(SLOT, 'Tapped away')
    await nextFrame()
    await nextFrame()
    expect(document.activeElement).not.toBe(composer())
    expect(document.activeElement).toBe(target)
  })

  it('(e) the composer never gains focus or sees a key from the Enter that committed the rename', async () => {
    // The composer sends on a plain Enter keydown and a posted turn has no
    // undo. Whatever the user does with the Enter key around the commit (holds
    // it, taps it twice, pauses), the composer must not become the target.
    renderControl()
    const composerKeys = vi.fn()
    composer().addEventListener('keydown', composerKeys)
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Twice' } }) })
    pressEnter(input)
    await nextFrame()
    const trigger = triggerNamed('Twice')
    expect(document.activeElement).toBe(trigger)
    // Second tap.
    act(() => { fireEvent.keyDown(document.activeElement as Element, { key: 'Enter' }) })
    act(() => { fireEvent.keyUp(window, { key: 'Enter' }) })
    await act(async () => { await new Promise((r) => setTimeout(r, 50)) })
    expect(document.activeElement).not.toBe(composer())
    expect(composerKeys).not.toHaveBeenCalled()
  })

  it('(e) while Enter is still held, auto-repeat on the newly focused trigger does not reopen the editor', async () => {
    // OS auto-repeat: more keydowns before any keyup. The trigger activates on
    // Enter keydown, so without the guard the rename would reopen at once.
    renderControl()
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Held' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    await nextFrame()
    const trigger = triggerNamed('Held')
    expect(document.activeElement).toBe(trigger)
    act(() => { fireEvent.keyDown(trigger, { key: 'Enter', repeat: true }) })
    act(() => { fireEvent.keyDown(trigger, { key: 'Enter', repeat: true }) })
    expect(screen.queryByDisplayValue('Held')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    // Released: a fresh Enter is a real request to rename again.
    act(() => { fireEvent.keyUp(window, { key: 'Enter' }) })
    act(() => { fireEvent.keyDown(trigger, { key: 'Enter' }) })
    expect(screen.getByDisplayValue('Held')).toBeTruthy()
  })

  it('(e) if the Enter release never arrives, the trigger accepts Enter again after the grace period', async () => {
    vi.useFakeTimers()
    try {
      renderControl()
      const input = openEditor()
      act(() => { fireEvent.change(input, { target: { value: 'Lost' } }) })
      act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
      act(() => { fireEvent.blur(input) })
      act(() => { vi.advanceTimersByTime(20) })
      await act(async () => { await Promise.resolve() })
      const trigger = triggerNamed('Lost')
      expect(document.activeElement).toBe(trigger)
      act(() => { fireEvent.keyDown(trigger, { key: 'Enter', repeat: true }) })
      expect(screen.queryByDisplayValue('Lost')).toBeNull()
      act(() => { vi.advanceTimersByTime(ENTER_RELEASE_GRACE_MS + 10) })
      act(() => { fireEvent.keyDown(trigger, { key: 'Enter' }) })
      expect(screen.getByDisplayValue('Lost')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('(e) a pointer click on the trigger right after the commit still opens the editor', async () => {
    // The held-key guard is about KEYBOARD activation only; a mouse user who
    // commits with Enter and clicks the title again gets the editor.
    renderControl()
    const input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Clicked' } }) })
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    act(() => { fireEvent.blur(input) })
    await nextFrame()
    act(() => { fireEvent.click(triggerNamed('Clicked')) })
    expect(screen.getByDisplayValue('Clicked')).toBeTruthy()
  })

  it('(e) Auto-title announces the generated name through the same region, and only once it lands', async () => {
    let resolve: (v: { title: string }) => void = () => {}
    vi.mocked(api.generateTitle).mockReturnValueOnce(new Promise((r) => { resolve = r }))
    renderControl()
    act(() => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    expect(status().textContent).toBe('')
    await act(async () => { resolve({ title: 'Generated title' }) })
    await waitFor(() => expect(status().textContent).toBe('Session renamed to Generated title'))
  })

  it('(e) an Auto-title that comes back identical to the current name announces nothing', async () => {
    vi.mocked(api.generateTitle).mockResolvedValueOnce({ title: TITLE })
    renderControl()
    act(() => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    await waitFor(() => expect(api.generateTitle).toHaveBeenCalled())
    await nextFrame()
    expect(status().textContent).toBe('')
  })

  it('(e) a confirmed rename back to an already-announced name is announced again', async () => {
    // A live region announces text CHANGES only, so the region is emptied when
    // an attempt starts; otherwise Alpha -> Beta, Auto-title, Undo (Beta again)
    // would write the identical string and say nothing.
    renderControl()
    let input = openEditor()
    act(() => { fireEvent.change(input, { target: { value: 'Beta' } }) })
    await pressEnter(input)
    await waitFor(() => expect(status().textContent).toBe('Session renamed to Beta'))
    input = openEditorFor('Beta')
    act(() => { fireEvent.change(input, { target: { value: 'Gamma' } }) })
    await pressEnter(input)
    await waitFor(() => expect(status().textContent).toBe('Session renamed to Gamma'))
    let released: (v: unknown) => void = () => {}
    vi.mocked(api.renameSlot).mockReturnValueOnce(new Promise((r) => { released = r }))
    input = openEditorFor('Gamma')
    act(() => { fireEvent.change(input, { target: { value: 'Beta' } }) })
    await pressEnter(input)
    // Emptied while the write is in flight, so the confirmation is a change.
    await waitFor(() => expect(status().textContent).toBe(''))
    await act(async () => { released({}) })
    await waitFor(() => expect(status().textContent).toBe('Session renamed to Beta'))
  })

  it('keeps the memory-mode glyph inside the trigger, its text in the name, and clickable to rename', () => {
    renderControl({ memoryMode: 'incognito' })
    const glyph = screen.getByTestId('memory-mode-glyph')
    // The trigger's `aria-label` prunes its subtree from the accessible name,
    // so the glyph's tooltip is folded into that label after the title (WCAG
    // 2.5.3 keeps the visible text first) instead of being lost.
    const trigger = screen.getByRole('button', { name: `${RENAME}, Incognito — memory writes disabled` })
    expect(trigger.contains(glyph)).toBe(true)
    expect(glyph.getAttribute('title')).toBe('Incognito — memory writes disabled')
    // A click on the glyph itself opens the editor, as it did before the label.
    fireEvent.click(glyph)
    expect(screen.getByDisplayValue(TITLE)).toBeTruthy()
    // Beside the editor the glyph names itself.
    expect(screen.getByRole('img', { name: 'Incognito — memory writes disabled' })).toBeTruthy()
  })
})
