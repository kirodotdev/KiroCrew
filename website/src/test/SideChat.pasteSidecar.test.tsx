/**
 * SideChat's paste-block sidecar (#11337).
 *
 * `ChatInput` collapses a large paste into a `[ Paste #N · M lines ]` token
 * only when its host passes `onPasteBlocksChange`; the side panel rendered it
 * without that prop, so a big paste stayed raw text there. These scenes drive
 * the REAL panel and assert against the wire: the token in the composer, the
 * EXPANDED question in the API call, the composer clear — and the lifetime
 * case that makes a token safe here: the panel is unmounted by every host
 * control beside it, and its draft outlives that in the side-chat draft
 * store, so the blocks must come back with the text on remount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import reducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { renderWithProviders, createTestStore, awaitComposer, composerRoot, composerValue, pasteIntoComposer, pressInComposer, setComposerSelection, setComposerValue } from './helpers'
import { clearSideChatDrafts, readSideChatDraft } from '../chat-core/composer/sideChatDrafts'

const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }

const sideTurn = vi.fn()
const sideOpen = vi.fn()

// The panel mounts the rich composer on a fine pointer and keeps the textarea
// on touch (`lexicalComposer={!touchDevice}`). `useIsTouchDevice` reads its
// snapshot through this predicate, so one scene below can opt into the
// textarea path by flipping it.
const touchEnv = { touch: false }
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = prop === 'sideTurn'
        ? sideTurn
        : prop === 'sideOpen'
          ? sideOpen
          : prop === 'slashCommands'
          ? vi.fn().mockResolvedValue([])
          : vi.fn().mockResolvedValue(prop === 'sideClose' ? { ok: true, was_open: true } : {})
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

const SLOT = 'paste-slot'
const PASTED = 'alpha\nbeta\ngamma\ndelta\nepsilon' // >= PASTE_THRESHOLD_LINES
const TOKEN = /\[ Paste #1 · 5 lines \]/

const initial = reducer(undefined, { type: '@@INIT' })

const panel = () => document.querySelector('[data-side-chat-input]') as HTMLElement

async function mount() {
  const store = createTestStore({ dashboard: dashInitial, chat: { ...initial, activeSlot: SLOT } })
  const utils = renderWithProviders(<SideChat slot={SLOT} />, { store })
  await awaitComposer(panel())
  return utils
}

/** A side turn in flight: the composer shows the split button, whose default
 *  action is the steer branch of `send`. */
async function mountBusy() {
  const store = createTestStore({
    dashboard: dashInitial,
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'partial', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          pending: false,
          streaming: true,
          openedAtTurnCount: 0,
          createdAt: '2026-05-20T00:00:00Z',
        },
      },
    },
  })
  const utils = renderWithProviders(<SideChat slot={SLOT} />, { store })
  await awaitComposer(panel())
  return utils
}

/** The rich composer's editable root, scoped to the side panel. It has no
 *  `.value` and takes no `fireEvent.change`: read it through `value()`, write
 *  it through `type()`, send with `pressEnter()`. */
const box = () => composerRoot(panel())
const value = () => composerValue(box())
const type = (text: string) => setComposerValue(text, box())
const pressEnter = () => pressInComposer('Enter', {}, box())

/** Paste through the real handler: the composer's paste command collapses a
 *  big plain-text paste into a pill. A paste only ever lands on an editor
 *  with a caret in it, and the panel does not autofocus its composer the way
 *  the main chat does, so put the caret at the end first — through the same
 *  control SideChat's own seed nudge uses. */
const pasteInto = async (text: string) => {
  const len = value().length
  await setComposerSelection(len, len, box())
  await pasteIntoComposer(text, box())
}

beforeEach(() => {
  sideTurn.mockReset()
  sideOpen.mockReset()
  sideTurn.mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 })
  sideOpen.mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' })
  clearSideChatDrafts()
})

afterEach(() => { touchEnv.touch = false })

describe('SideChat paste sidecar', () => {
  it('collapses a large paste to a token, sends it expanded and clears the composer', async () => {
    await mount()
    await type('what does this mean ')
    await pasteInto(PASTED)
    // The pill: the composer holds the token, not the pasted lines.
    await waitFor(() => expect(value()).toMatch(TOKEN))
    expect(value()).not.toContain('gamma')

    pressEnter()
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    const question = sideTurn.mock.calls[0][1] as string
    // The model gets the CONTENT, never the token string.
    expect(question).toContain(PASTED)
    expect(question).not.toMatch(TOKEN)
    expect(question.startsWith('what does this mean')).toBe(true)
    // Composer and draft store are clear, and so are the blocks: the next
    // paste numbers from #1 again (a surviving block would make it #2).
    await waitFor(() => expect(value()).toBe(''))
    expect(readSideChatDraft(SLOT)).toBe('')
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
  })

  it('expands on the steer branch too', async () => {
    await mountBusy()
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
    fireEvent.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    const [, question, opts] = sideTurn.mock.calls[0] as [string, string, { steer?: boolean } | undefined]
    expect(opts).toEqual({ steer: true })
    expect(question).toContain(PASTED)
    expect(question).not.toMatch(TOKEN)
    await waitFor(() => expect(value()).toBe(''))
  })

  it('keeps the blocks with the draft across a remount, so the token still expands', async () => {
    const first = await mount()
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
    // Every host control beside the composer unmounts the panel; the draft
    // store is what survives it.
    first.unmount()
    expect(readSideChatDraft(SLOT)).toMatch(TOKEN)

    await mount()
    await waitFor(() => expect(value()).toMatch(TOKEN))
    pressEnter()
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    const question = sideTurn.mock.calls[0][1] as string
    expect(question).toContain(PASTED)
    expect(question).not.toMatch(TOKEN)
  })

  it('says the collapsed paste is counted when the expanded question is too long', async () => {
    // The composer shows one line and a pill; the byte limit is measured on
    // what the server would receive, so the message has to name the paste or
    // "yours: 32,769" reads as nonsense against a nearly empty box.
    await mount()
    await type('x ')
    await pasteInto(Array.from({ length: 33 }, () => 'a'.repeat(1_000)).join('\n'))
    await waitFor(() => expect(value()).toMatch(/\[ Paste #1 · 33 lines \]/))
    pressEnter()
    await waitFor(() => expect(screen.getByText(/Question too long — reduce to under ~32,768 characters \(yours: 33,03\d, counting the collapsed paste\)/)).toBeInTheDocument())
    expect(sideTurn).not.toHaveBeenCalled()
    // Nothing was consumed: the pill is still there for the user to remove.
    expect(value()).toMatch(/\[ Paste #1 · 33 lines \]/)
    // A validation hint beside the composer (role=status), not an ErrorNotice:
    // nothing failed, the question was never sent. Editing the draft clears it.
    expect(screen.getByTestId('side-chat-length-hint')).toHaveAttribute('role', 'status')
    expect(screen.queryByRole('alert')).toBeNull()
    await type('shorter')
    await waitFor(() => expect(screen.queryByTestId('side-chat-length-hint')).toBeNull())
  })

  it('a refused submit hands the pill back as a pill, block and all', async () => {
    sideTurn.mockRejectedValueOnce(new Error('refused'))
    await mount()
    await type('why ')
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
    pressEnter()
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    // The composer cleared on submit and the refusal put the TOKEN text back —
    // not the expanded lines — with its block, so the retry sends the content.
    await waitFor(() => expect(value()).toMatch(TOKEN))
    expect(value()).not.toContain('gamma')
    pressEnter()
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(2))
    const retry = sideTurn.mock.calls[1][1] as string
    expect(retry).toContain(PASTED)
    expect(retry).not.toMatch(TOKEN)
  })

  it('a refusal landing after an undo restored the pill does not add a second one', async () => {
    // Undo-after-send is the TEXTAREA's explicit snapshot history (a
    // programmatic clear is its own restorable boundary); the rich composer
    // clears its history on a controlled sync, so the scene runs on the
    // textarea path — the one touch devices keep — and drives it directly.
    touchEnv.touch = true
    let refuse!: (e: Error) => void
    sideTurn.mockImplementationOnce(() => new Promise((_r, reject) => { refuse = reject }))
    const store = createTestStore({ dashboard: dashInitial, chat: { ...initial, activeSlot: SLOT } })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    const ta = () => screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    const pasteIntoTextarea = (text: string) => act(async () => {
      fireEvent.paste(ta(), { clipboardData: { items: [], getData: (t: string) => (t === 'text' ? text : '') } })
    })
    // Typed words AND a pill: the whole payload must dedupe, not just the token.
    fireEvent.change(ta(), { target: { value: 'why does this fail? ' } })
    await pasteIntoTextarea(PASTED)
    await waitFor(() => expect(ta().value).toMatch(TOKEN))
    const before = ta().value
    expect(before.startsWith('why does this fail?')).toBe(true)
    fireEvent.keyDown(ta(), { key: 'Enter' })
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(ta().value).toBe(''))
    // Undo puts the token AND its block back while the request is in flight.
    fireEvent.keyDown(ta(), { key: 'z', ctrlKey: true })
    await waitFor(() => expect(ta().value).toBe(before))
    // The refusal arrives: the payload is already in the composer, so nothing
    // is appended and the retry sends the paste exactly once.
    await act(async () => { refuse(new Error('refused')) })
    await waitFor(() => expect(ta().value).toBe(before))
    expect(ta().value.match(/\[ Paste #/g)).toHaveLength(1)
    expect(ta().value.match(/why does this fail/g)).toHaveLength(1)
    fireEvent.keyDown(ta(), { key: 'Enter' })
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(2))
    const retry = sideTurn.mock.calls[1][1] as string
    expect(retry.split(PASTED).length - 1).toBe(1)
  })

  it('drops a block whose token the user deleted as text', async () => {
    await mount()
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
    await type('typed over it ')
    // The block went with its token: a fresh paste numbers from #1 again (a
    // surviving block would make it #2), and the send carries only this one.
    await pasteInto('one\ntwo\nthree\nfour')
    await waitFor(() => expect(value()).toMatch(/\[ Paste #1 · 4 lines \]/))
    pressEnter()
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    // The rich composer's pill sits inline at the caret, so the expansion
    // splices the content exactly where the pill was (the `<textarea>` path put
    // its token on its own line, hence a newline there).
    expect(sideTurn.mock.calls[0][1]).toBe('typed over it one\ntwo\nthree\nfour')
  })
})
