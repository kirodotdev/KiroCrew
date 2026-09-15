/**
 * The side panel's Enter key, end to end through the real component.
 *
 * `useComposerDraft.test.ts` pins the hook's own behaviour, but the bug this closes
 * was a WIRING bug: the component had its own Enter branch and never consulted the
 * IME guard. A hook test cannot see that, so this drives the actual textarea and
 * asserts against the API call the surface would have made.
 *
 * Why it matters: an IME sends a final Enter to commit the candidate the user just
 * chose. Reading it as a submit sends a half-written question AND clears the box, so
 * there is nothing left to recover.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, waitFor, act } from '@testing-library/react'
import reducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { renderWithProviders, createTestStore, composerRoot, composerValue, setComposerValue, pressInComposer } from './helpers'

// The composer blocks sends while the gateway reads as offline, so every
// scene runs against a connected dashboard unless it tests the offline path.
const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }

const sideTurn = vi.fn()
const sideOpen = vi.fn()

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

const SLOT = 'ime-slot'

describe('SideChat Enter key', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  const render = async () => {
    const store = createTestStore({ dashboard: dashInitial, chat: { ...initial, activeSlot: SLOT } })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await waitFor(() =>
      expect(document.querySelector('[data-side-chat-input] [data-composer-input]')).not.toBeNull(),
    )
    await act(async () => {})
    const box = composerRoot(document.querySelector('[data-side-chat-input]') as HTMLElement)
    await setComposerValue('a question', box)
    return box
  }

  beforeEach(() => {
    sideTurn.mockReset()
    sideOpen.mockReset()
    sideTurn.mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 })
    sideOpen.mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' })
  })

  /** A submit reaches `sideTurn` only after `sideOpen` resolves, so a negative case
   *  that asserted synchronously would pass on timing alone. Drain the microtask queue
   *  the mutation chain runs on, then assert. The 'sends on a plain Enter' case above is
   *  the control: it proves this harness CAN reach `sideTurn`, so a negative below
   *  failing to is the guard working rather than the test never getting there. */
  const settle = () => act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })

  it('sends on a plain Enter', async () => {
    const box = await render()
    pressInComposer('Enter', {}, box)
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    expect(sideTurn.mock.calls[0][1]).toBe('a question')
  })

  it('does not send on Shift+Enter', async () => {
    const box = await render()
    pressInComposer('Enter', { shiftKey: true }, box)
    await settle()
    expect(sideTurn).not.toHaveBeenCalled()
  })

  it('does not send the Enter that commits an IME candidate', async () => {
    const box = await render()
    pressInComposer('Enter', { isComposing: true }, box)
    await settle()
    expect(sideTurn).not.toHaveBeenCalled()
    // The half-written text is still there, which is the whole point.
    expect(composerValue(box)).toBe('a question')
  })

  it('does not send while the browser reports the IME-processing keyCode', async () => {
    const box = await render()
    pressInComposer('Enter', { keyCode: 229 }, box)
    await settle()
    expect(sideTurn).not.toHaveBeenCalled()
  })

  it('does not send between compositionStart and the commit', async () => {
    // Neither browser signal is set here — only the tracked composition state knows,
    // and it only knows because the component spreads the guard's handlers onto the box.
    const box = await render()
    fireEvent.compositionStart(box)
    pressInComposer('Enter', {}, box)
    await settle()
    expect(sideTurn).not.toHaveBeenCalled()
  })

  /**
   * Recovery: a composition abandoned without a compositionEnd (focus moved away,
   * an OS-level IME cancel) must not latch the guard forever. Before the recovery
   * wiring, this panel could never send again until it remounted — the guard that
   * exists to save one message ate all of them.
   */
  it('sends again after a composition abandoned by focus leaving', async () => {
    const box = await render()
    fireEvent.compositionStart(box)
    // No compositionEnd — focus just leaves the box mid-composition. The shared
    // IME latch resets on the root's focusout, the bubbling event a real browser
    // fires when focus leaves (the composer root listens for focusout/focusin,
    // not the non-bubbling blur/focus the old textarea binding used).
    fireEvent.focusOut(box)
    pressInComposer('Enter', {}, box)
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    expect(sideTurn.mock.calls[0][1]).toBe('a question')
  })

  it('sends again after a composition abandoned by Escape', async () => {
    const box = await render()
    fireEvent.compositionStart(box)
    pressInComposer('Escape', {}, box)
    // A real browser fires compositionend when Escape cancels the candidate;
    // the guard then holds Enter for a further 50ms (the commit-Enter window)
    // before a genuinely separate Enter may submit again.
    fireEvent.compositionEnd(box)
    await new Promise(r => setTimeout(r, 60))
    pressInComposer('Enter', {}, box)
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
    expect(sideTurn.mock.calls[0][1]).toBe('a question')
  })
})
