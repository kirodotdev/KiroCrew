/**
 * Opening a session from the Cmd/Ctrl+K surface must leave the caret in that
 * session's chat composer (#15732) -- the same outcome as clicking the session in
 * the sidebar.
 *
 * The harness is the real surface (real providers, real store thunks) beside the
 * real ChatInput bound to `chat.activeSlot`, which is how ChatPage mounts it. Two
 * surfaces answer the gesture -- the Command Bar app, default-on, and the legacy
 * palette it falls back to -- and both are driven the way the reporter drives
 * them: open, arrow to a row, Enter.
 *
 * The composer's own autofocus (ChatInput's autoFocusKey effect) runs only when
 * the active slot key CHANGES and declines, without retrying, when an editable
 * element holds focus at that moment. Each row below is one way the surfaces
 * slip past it:
 *
 *  - the session that is ALREADY active: the key never changes, so the effect
 *    never runs -- on either surface, and through the search path's async
 *    `resumeFromHistory` as much as through a live row's `switchSlot`;
 *  - a different live session: `switchSlot` moves the key synchronously, while
 *    the surface closes later -- on the Command Bar only once the row's promise
 *    settles, so the bar's own input still holds focus when the key commits;
 *  - a history session: the key moves only once the resume round-trip fulfils.
 *
 * And the ordering on a live row: the caret moves only once that row's
 * `switchSlot` has LANDED. `pending` enters the target synchronously, so during
 * the gateway round trip the composer already answers to a slot the gateway may
 * still refuse -- a 404 unwinds the selection to the origin and evicts the gone
 * row, and the page files whatever was typed meanwhile under the evicted key.
 * The held-fetch cases below type into that window and read where the text
 * went.
 *
 * Only the backend is mocked. `isTouchDevice` is pinned false because every
 * composer-focus path skips touch devices by design (focusing there pops the
 * on-screen keyboard), and the shipped implementation reads `matchMedia`, which
 * the test DOM does not drive.
 */
import { useCallback, useEffect, useRef, useState, type ComponentType } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders, createTestStore } from './helpers'
import { useAppSelector } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import chatReducer, { switchSlot } from '../store/chatSlice'
import ChatInput from '../components/ChatInput'
import CommandPalette from '../components/CommandPalette'
import CommandBarOverlay from '../apps/command-bar/CommandBarOverlay'
import type { ChatSlot } from '../types'

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const apiMock = vi.hoisted(() => ({
  sessions: vi.fn(),
  crons: vi.fn(),
  chatFolders: vi.fn(),
  chatSlotDetail: vi.fn(),
  resumeChatSlot: vi.fn(),
  sessionsSearch: vi.fn(),
  instancesSearchSessions: vi.fn(),
  listInstances: vi.fn(),
}))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, ...apiMock } }
})

const ACTIVE = 'zzq-active'
const OTHER = 'zzq-other'
const HISTORY = 'zzq-history'

function slot(patch: Partial<ChatSlot> & { key: string }): ChatSlot {
  return { messages: 1, running: false, ...patch } as ChatSlot
}

type Surface = ComponentType<{ open: boolean; onClose: () => void }>
/** How to find a surface's query field: the Command Bar's is a `combobox`, the
 *  legacy palette's a plain `textbox`; both carry an accessible name. */
type Field = { role: 'combobox' | 'textbox'; name: string }

/** ChatPage's wiring in miniature: one composer bound to the active slot, the
 *  shell-owned open state of the quick-search surface beside it, and the page's
 *  draft hand-over (the `prevSlot` effect in `website/src/pages/ChatPage.tsx`): on a
 *  slot-key change the text in the composer is filed under the slot that owned it
 *  and the new slot's draft comes back. `drafts` is the test's window on where a
 *  keystroke ends up -- the data-loss class under test is text filed under a key
 *  no row lists any more. */
function Harness({ surface: QuickSearch, drafts, mountWhileOpen }: { surface: Surface; drafts: Record<string, string>; mountWhileOpen: boolean }) {
  const [open, setOpen] = useState(false)
  const activeSlot = useAppSelector((s) => s.chat.activeSlot)
  const [input, setInput] = useState('')
  const typed = useRef('')
  const change = useCallback((value: string) => { typed.current = value; setInput(value) }, [])
  const prevSlot = useRef<string | null>(null)
  useEffect(() => {
    if (prevSlot.current) drafts[prevSlot.current] = typed.current
    prevSlot.current = activeSlot
    change(activeSlot ? drafts[activeSlot] ?? '' : '')
  }, [activeSlot, drafts, change])
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>open palette</button>
      <ChatInput value={input} onChange={change} onSend={() => {}} autoFocusKey={activeSlot} />
      {/* `QuickSearchSurface` mounts the Command Bar only while open -- so its focus
          trap captures the active element at open and restores it at close -- and
          keeps the legacy palette mounted with an `open` prop. Same here. */}
      {mountWhileOpen ? (open && <QuickSearch open onClose={() => setOpen(false)} />) : <QuickSearch open={open} onClose={() => setOpen(false)} />}
    </>
  )
}

const emptyTranscript = (key: string) => ({ key, messages: [], running: false, has_more: false, total: 0, next_before: 0 })

type HeldRead = { fulfil: () => void; refuse404: () => void }

/** Hold one slot's detail fetches open, one deferred per read in the order the
 *  gateway was asked, so a round trip becomes a window the test can type into
 *  and then settle either way -- and two overlapping reads of the same slot can
 *  land in either order. Every other slot keeps answering at once. */
function holdSlotDetail(key: string): HeldRead[] {
  const reads: HeldRead[] = []
  apiMock.chatSlotDetail.mockImplementation((k: string) => {
    if (k !== key) return Promise.resolve(emptyTranscript(k))
    return new Promise<ReturnType<typeof emptyTranscript>>((resolve, reject) => {
      reads.push({
        fulfil: () => resolve(emptyTranscript(key)),
        // The shape `api.chatSlotDetail` throws for a gone session: a numeric 404
        // is what `isMissingSlotError` classifies, so the real unwind runs.
        refuse404: () => reject(Object.assign(new Error('HTTP 404: not found'), { status: 404 })),
      })
    })
  })
  return reads
}

/** A keystroke goes to the element that holds focus: a textarea takes it as text,
 *  anything else drops it -- which is what the browser does with a key pressed
 *  while focus sits on `<body>`. */
function typeWhereFocusIs(text: string) {
  const target = document.activeElement
  if (target instanceof HTMLTextAreaElement) fireEvent.change(target, { target: { value: target.value + text } })
}

/** Let the frames the helpers schedule run out, without asserting anything. */
const settleFrames = async (count = 2) => {
  for (let i = 0; i < count; i++) {
    await new Promise<void>((r) => requestAnimationFrame(() => r()))
  }
  await Promise.resolve()
}

beforeEach(() => {
  apiMock.sessions.mockReset().mockResolvedValue({ sessions: [{ key: HISTORY, title: 'History session', modified: '2024-05-15T12:00:00Z' }] })
  apiMock.crons.mockReset().mockResolvedValue([])
  apiMock.chatFolders.mockReset().mockResolvedValue([])
  apiMock.chatSlotDetail.mockReset().mockImplementation((key: string) => Promise.resolve(emptyTranscript(key)))
  // An empty surface is the plain dashboard chat -- the one surface ChatPage
  // can display, so the resume actually enters the slot (see isChatPageSurface).
  apiMock.resumeChatSlot.mockReset().mockImplementation((key: string) =>
    Promise.resolve({ ok: true, key, mode: '', surface: '', messages: [], has_more: false, total: 0, next_before: 0 }))
  // The search path answers with the live session the reader already has open.
  apiMock.sessionsSearch.mockReset().mockResolvedValue({ sessions: [{ key: ACTIVE, title: 'Active session' }] })
  apiMock.instancesSearchSessions.mockReset().mockRejectedValue(new Error('instances off'))
  apiMock.listInstances.mockReset().mockResolvedValue({ instances: [] })
})

type OpenOptions = { mountWhileOpen: boolean; caretInComposer?: boolean }

async function openSurfaceOnActiveSession(surface: Surface, field: Field, opts: OpenOptions) {
  // The slice's own initial state, read through the facade (the store's
  // internals are not imported from outside src/store), with one slot active.
  const store = createTestStore({ chat: { ...chatReducer(undefined, { type: '@@init' }), activeSlot: ACTIVE } })
  store.dispatch(sseSlots([
    slot({ key: ACTIVE, title: 'Active session', last_activity_ts: Date.UTC(2024, 4, 15, 12, 0, 0) }),
    slot({ key: OTHER, title: 'Other session', last_activity_ts: Date.UTC(2024, 4, 15, 11, 0, 0) }),
  ]))
  const drafts: Record<string, string> = {}
  renderWithProviders(<Harness surface={surface} drafts={drafts} mountWhileOpen={opts.mountWhileOpen} />, { store, route: '/chat' })
  const composer = screen.getByLabelText('Message input')
  if (opts.caretInComposer) {
    // The commonest entry: the reader is typing and reaches for the shortcut.
    composer.focus()
    expect(composer).toHaveFocus()
  } else {
    // The mount-time autofocus is not the thing under test: start from "focus
    // somewhere else", as a user who clicked into the transcript would.
    composer.blur()
    expect(composer).not.toHaveFocus()
  }

  fireEvent.click(screen.getByText('open palette'))
  const input = await screen.findByRole(field.role, { name: field.name })
  // The surface focuses its own input once mounted; the rows arrive once the
  // store-backed listing renders (and, on the palette, its fetches resolve).
  await waitFor(() => expect(input).toHaveFocus())
  await screen.findByText('Active session')
  await screen.findByText('Other session')
  return { store, composer, input, drafts }
}

/** Arrow down to the row carrying `title` (the selection starts at the top on
 *  open) and press Enter on it -- the reporter's keyboard path, through the
 *  surface's own key handling. */
function chooseRow(input: HTMLElement, title: string) {
  const rows = screen.getAllByRole('option')
  const index = rows.findIndex((row) => row.textContent?.includes(title))
  expect(index).toBeGreaterThanOrEqual(0)
  for (let i = 0; i < index; i++) fireEvent.keyDown(input, { key: 'ArrowDown' })
  expect(rows[index]).toHaveAttribute('aria-selected', 'true')
  fireEvent.keyDown(input, { key: 'Enter' })
}

async function expectClosedAndComposerFocused(composer: HTMLElement, field: Field) {
  await waitFor(() => expect(screen.queryByRole(field.role, { name: field.name })).toBeNull())
  // The focus lands on the frame after the store change commits, so "holds
  // focus" is a wait, not a property of the first frame.
  await waitFor(() => expect(composer).toHaveFocus())
}

describe('opening a session from the Command Bar focuses the composer (#15732)', () => {
  const FIELD: Field = { role: 'combobox', name: 'Command Bar' }
  const open = (opts: Omit<OpenOptions, 'mountWhileOpen'> = {}) =>
    openSurfaceOnActiveSession(CommandBarOverlay, FIELD, { mountWhileOpen: true, ...opts })
  const closed = () => waitFor(() => expect(screen.queryByRole(FIELD.role, { name: FIELD.name })).toBeNull())

  it('when the chosen session is the one already active', async () => {
    const { store, composer, input } = await open()
    chooseRow(input, 'Active session')
    expect(store.getState().chat.activeSlot).toBe(ACTIVE)
    await expectClosedAndComposerFocused(composer, FIELD)
  })

  it('when the chosen session is another live session', async () => {
    const { store, composer, input } = await open()
    chooseRow(input, 'Other session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await expectClosedAndComposerFocused(composer, FIELD)
  })

  it('focuses only once the switch has landed, never during the gateway round trip', async () => {
    const { store, composer, input, drafts } = await open()
    const reads = holdSlotDetail(OTHER)
    chooseRow(input, 'Other session')
    // `switchSlot.pending` has entered the target and the bar has closed, but the
    // gateway has not answered: the composer already answers to a slot that may
    // yet be refused, so a caret there would route keystrokes to it.
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await closed()
    await settleFrames()
    expect(composer).not.toHaveFocus()
    typeWhereFocusIs('x')
    await waitFor(() => expect(reads).toHaveLength(1))
    reads[0].fulfil()
    await expectClosedAndComposerFocused(composer, FIELD)
    // The key pressed inside the round trip reached no draft.
    expect(drafts[OTHER] ?? '').toBe('')
    expect(composer).toHaveValue('')
  })

  it('keeps the caret out of the composer for the whole round trip when the bar was opened FROM the composer', async () => {
    // The commonest entry. The bar's focus trap captured the composer at open and
    // restores it at close -- which would put the caret into the in-flight target's
    // draft, the window this whole fix exists to close. Opening a session skips
    // that restore; the caret arrives once the switch has landed.
    const { store, composer, input, drafts } = await open({ caretInComposer: true })
    const reads = holdSlotDetail(OTHER)
    chooseRow(input, 'Other session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await closed()
    await settleFrames()
    expect(composer).not.toHaveFocus()
    typeWhereFocusIs('x')
    await waitFor(() => expect(reads).toHaveLength(1))
    reads[0].fulfil()
    await expectClosedAndComposerFocused(composer, FIELD)
    expect(drafts[OTHER] ?? '').toBe('')
    expect(composer).toHaveValue('')
  })

  it('routes no keystroke to the evicted slot when the switch is refused, and grants no focus meanwhile', async () => {
    const { store, composer, input, drafts } = await open()
    const reads = holdSlotDetail(OTHER)
    chooseRow(input, 'Other session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await closed()
    await settleFrames()
    // Typed inside the round trip. With the caret already in the composer this
    // text would belong to the target's draft -- and the 404 below unwinds the
    // selection to the origin and evicts the target row, so the page would file
    // it under a key no row lists again.
    const caretInComposerMidFlight = document.activeElement === composer
    typeWhereFocusIs('lost if routed')
    await waitFor(() => expect(reads).toHaveLength(1))
    reads[0].refuse404()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ACTIVE))
    await waitFor(() => expect(store.getState().dashboard.slots.map((s) => s.key)).not.toContain(OTHER))
    await settleFrames()
    expect(drafts[OTHER] ?? '').toBe('')
    expect(composer).toHaveValue('')
    expect(caretInComposerMidFlight).toBe(false)
    // Where focus sits NOW is the composer's own autofocus's call: the unwind is a
    // slot-key transition back to the origin, the sidebar's rule, untouched here.
  })

  it('defers to a newer same-key switch still in flight, and focuses once that one has landed', async () => {
    // Two reads of the same slot overlap: the row's, and a second one issued while
    // the first is still out (the chat page's mount-time `switchSlot(activeSlot)`
    // when the bar was used from another page). The older read lands first; the
    // newer one owns the claim, and the selection may yet unwind on its answer.
    const { store, composer, input, drafts } = await open()
    const reads = holdSlotDetail(OTHER)
    chooseRow(input, 'Other session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await closed()
    const newer = store.dispatch(switchSlot(OTHER))
    await waitFor(() => expect(reads).toHaveLength(2))
    reads[0].fulfil()
    await settleFrames(3)
    expect(composer).not.toHaveFocus()
    typeWhereFocusIs('x')
    reads[1].fulfil()
    await newer
    await expectClosedAndComposerFocused(composer, FIELD)
    expect(drafts[OTHER] ?? '').toBe('')
    expect(composer).toHaveValue('')
  })

  it('files nothing under the evicted slot when the newer same-key switch is refused after the older landed', async () => {
    const { store, composer, input, drafts } = await open()
    const reads = holdSlotDetail(OTHER)
    chooseRow(input, 'Other session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await closed()
    const newer = store.dispatch(switchSlot(OTHER))
    await waitFor(() => expect(reads).toHaveLength(2))
    reads[0].fulfil()
    await settleFrames(3)
    const caretInComposerMidFlight = document.activeElement === composer
    typeWhereFocusIs('lost if routed')
    // The session was deleted between the two reads: the live one 404s and the
    // selection unwinds to the origin.
    reads[1].refuse404()
    await newer
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ACTIVE))
    await settleFrames()
    expect(drafts[OTHER] ?? '').toBe('')
    expect(composer).toHaveValue('')
    expect(caretInComposerMidFlight).toBe(false)
  })

  it('focuses nothing for a switch the user moved past before it landed', async () => {
    const { store, composer, input } = await open()
    const reads = holdSlotDetail(OTHER)
    chooseRow(input, 'Other session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await closed()
    // A sidebar click back to the origin while the first switch is still out: its
    // own switch lands at once and the composer's autofocus takes the transition;
    // then the user clicks into the transcript.
    await store.dispatch(switchSlot({ key: ACTIVE, announceOnMissing: true }))
    await settleFrames()
    composer.blur()
    expect(composer).not.toHaveFocus()
    // The first switch lands late, for a slot that is not the active one: the
    // fulfilled reducer ignores it, and so does the focus.
    await waitFor(() => expect(reads).toHaveLength(1))
    reads[0].fulfil()
    await settleFrames(3)
    expect(store.getState().chat.activeSlot).toBe(ACTIVE)
    expect(composer).not.toHaveFocus()
  })
})

describe('opening a session from the legacy palette focuses the composer (#15732)', () => {
  const FIELD: Field = { role: 'textbox', name: 'Search everywhere' }
  const open = async () => {
    const opened = await openSurfaceOnActiveSession(CommandPalette, FIELD, { mountWhileOpen: false })
    await screen.findByText('History session')
    return opened
  }

  it('when the chosen session is the one already active', async () => {
    const { store, composer, input } = await open()
    chooseRow(input, 'Active session')
    expect(store.getState().chat.activeSlot).toBe(ACTIVE)
    await expectClosedAndComposerFocused(composer, FIELD)
  })

  it('when the chosen session is another live session', async () => {
    const { store, composer, input } = await open()
    chooseRow(input, 'Other session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await expectClosedAndComposerFocused(composer, FIELD)
  })

  it('routes no keystroke to the evicted slot when the switch is refused, and grants no focus meanwhile', async () => {
    const { store, composer, input, drafts } = await open()
    const reads = holdSlotDetail(OTHER)
    chooseRow(input, 'Other session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OTHER))
    await waitFor(() => expect(screen.queryByRole(FIELD.role, { name: FIELD.name })).toBeNull())
    await settleFrames()
    const caretInComposerMidFlight = document.activeElement === composer
    typeWhereFocusIs('lost if routed')
    await waitFor(() => expect(reads).toHaveLength(1))
    reads[0].refuse404()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ACTIVE))
    await waitFor(() => expect(store.getState().dashboard.slots.map((s) => s.key)).not.toContain(OTHER))
    await settleFrames()
    expect(drafts[OTHER] ?? '').toBe('')
    expect(composer).toHaveValue('')
    expect(caretInComposerMidFlight).toBe(false)
  })

  it('when the chosen session is resumed from history', async () => {
    const { store, composer, input } = await open()
    chooseRow(input, 'History session')
    await waitFor(() => expect(apiMock.resumeChatSlot).toHaveBeenCalledWith(HISTORY, 'History session'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(HISTORY))
    await expectClosedAndComposerFocused(composer, FIELD)
  })

  it('when the already-active session is chosen through the search path', async () => {
    const { store, composer, input } = await open()
    // Typing routes the palette to its search providers; the sessions provider
    // answers with the live session the reader is already in, and its row opens
    // through the async resume -- the key never changes.
    fireEvent.change(input, { target: { value: 'Active' } })
    // The search is debounced (SEARCH_DEBOUNCE_MS) and then fans out to the
    // providers; the row this test needs is the one the sessions provider owns.
    // Wait for the row the assertion is about, not merely a row with that text:
    // the recents listing shows the same title until the debounced search
    // (SEARCH_DEBOUNCE_MS) has been ISSUED for the typed query and answered.
    await waitFor(() => expect(apiMock.sessionsSearch.mock.calls.map((c) => c[0])).toContain('Active'), { timeout: 5000 })
    await waitFor(() => expect(screen.queryByText('Other session')).toBeNull(), { timeout: 5000 })
    chooseRow(input, 'Active session')
    await waitFor(() => expect(apiMock.resumeChatSlot).toHaveBeenCalledWith(ACTIVE, 'Active session'))
    expect(store.getState().chat.activeSlot).toBe(ACTIVE)
    await expectClosedAndComposerFocused(composer, FIELD)
  })
})
