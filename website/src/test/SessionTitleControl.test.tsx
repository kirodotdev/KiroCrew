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
import { api } from '../api/client'

const SLOT = 'pane-a'
const TITLE = 'Alpha session'
const REGEN = 'Regenerate title with LLM'

function makeStore(memoryMode?: string) {
  return createTestStore({
    dashboard: {
      status: null, connected: true, slotsLoaded: true,
      slots: [{ key: SLOT, title: TITLE, messages: 0, running: false, mode: '', ...(memoryMode ? { memory_mode: memoryMode } : {}) }],
      unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
    } as unknown as RootState['dashboard'],
  })
}

function renderControl(opts: { onError?: (m: string, t: string) => void; memoryMode?: string } = {}) {
  const store = makeStore(opts.memoryMode)
  // Mirrors the real hosts: the title prop follows the store.
  const Host = () => {
    const title = useSelector((s: RootState) => s.dashboard.slots.find((x) => x.key === SLOT)?.title ?? SLOT)
    return <SessionTitleControl slotKey={SLOT} title={title} compact onError={opts.onError} />
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div className="group/header"><Host /></div>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, ...utils }
}

const storeTitle = (store: ReturnType<typeof makeStore>) =>
  store.getState().dashboard.slots.find((s) => s.key === SLOT)?.title

const openEditor = () => {
  act(() => { fireEvent.click(screen.getByText(TITLE)) })
  return screen.getByDisplayValue(TITLE) as HTMLInputElement
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
    // Spinner gone, button back.
    await waitFor(() => expect(screen.getByRole('button', { name: REGEN })).toBeTruthy())
  })

  it('shows the spinner instead of the button while a title is generating', async () => {
    let resolve!: (v: { title: string }) => void
    vi.mocked(api.generateTitle).mockReturnValueOnce(new Promise((r) => { resolve = r }))
    renderControl()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: REGEN })) })
    expect(screen.queryByRole('button', { name: REGEN })).toBeNull()
    await act(async () => { resolve({ title: 'Done' }) })
    await waitFor(() => expect(screen.getByRole('button', { name: REGEN })).toBeTruthy())
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
    // Rename 1 now fails: the re-pull keeps the server's (newer) title.
    await act(async () => { rejectFirst(new Error('late')) })
    await waitFor(() => expect(onError).toHaveBeenCalledWith('late', "Couldn't rename the session"))
    await waitFor(() => expect(api.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(storeTitle(store)).toBe('Second'))
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
