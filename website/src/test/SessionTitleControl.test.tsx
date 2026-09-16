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
 *       one.
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

import SessionTitleControl from '../pages/chat/SessionTitleControl'
import { MOVE_UNDO_MS } from '../components/MoveUndoBar'
import { api } from '../api/client'

const SLOT = 'pane-a'
const TITLE = 'Alpha session'
const REGEN = 'Regenerate title with LLM — the current name can be restored with Undo'

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

function renderControl(opts: { onError?: (m: string, t: string) => void; memoryMode?: string } = {}) {
  const store = makeStore(opts.memoryMode)
  // Mirrors the real hosts: the title prop follows the store. `slot` is a
  // prop so a test can re-target the same instance like the main header does.
  const Host = ({ slot }: { slot: string }) => {
    const title = useSelector((s: RootState) => s.dashboard.slots.find((x) => x.key === slot)?.title ?? slot)
    return <SessionTitleControl slotKey={slot} title={title} compact onError={opts.onError} />
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (slot: string) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div className="group/header"><Host slot={slot} /></div>
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
    expect(screen.getByRole('button', { name: TITLE })).toBeTruthy()
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

  it('opens with the whole title selected, so a long name shows its start rather than a scrolled tail', () => {
    renderControl()
    const input = openEditor()
    // Caret parked at the end, as autofocus leaves it; the focus the browser
    // delivers on open is what the control selects on.
    act(() => { input.setSelectionRange(TITLE.length, TITLE.length) })
    act(() => { fireEvent.focus(input) })
    expect(input.selectionStart).toBe(0)
    expect(input.selectionEnd).toBe(TITLE.length)
    // Anchored backward: the selection FOCUS is at the start, which is what
    // makes the browser scroll the input to show the beginning of a long name.
    expect(input.selectionDirection).toBe('backward')
    expect(input.scrollLeft).toBe(0)
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
    expect(screen.queryByRole('status')).toBeNull()
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
    const titleBtn = screen.getByRole('button', { name: TITLE })
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
    expect(screen.getByRole('status').textContent).toBe('Auto-title…')
    await act(async () => { resolve({ title: 'Done' }) })
    // Settled: the status region is gone and the Undo offer stands in the button's place.
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
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

  it('keeps the memory-mode glyph in front of the title', () => {
    renderControl({ memoryMode: 'incognito' })
    expect(screen.getByTitle('Incognito — memory writes disabled')).toBeTruthy()
  })
})
