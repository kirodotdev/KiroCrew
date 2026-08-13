/**
 * The side panel's oversize-question refusal, end to end through the real component.
 *
 * The limit is enforced in UTF-8 bytes (a server contract), but the refusal must
 * speak in characters: a CJK user's characters cost 3 bytes each and emoji cost 4,
 * so a byte count gives them no actionable target. This drives the actual textarea
 * with an all-emoji question that exceeds the byte budget while sitting just past
 * the safe character floor, and asserts the message reports code points — and that
 * nothing was sent.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import reducer from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'

const sideTurn = vi.fn()
const sideOpen = vi.fn()

vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = prop === 'sideTurn'
        ? sideTurn
        : prop === 'sideOpen'
          ? sideOpen
          : vi.fn().mockResolvedValue(prop === 'sideClose' ? { ok: true, was_open: true } : {})
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

const SLOT = 'oversize-slot'

describe('SideChat oversize-question refusal', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  const render = (draft: string) => {
    const store = createTestStore({ chat: { ...initial, activeSlot: SLOT } })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    const box = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: draft } })
    return box
  }

  beforeEach(() => {
    sideTurn.mockReset()
    sideOpen.mockReset()
    sideTurn.mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 })
    sideOpen.mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' })
  })

  /** Drain the microtask queue the mutation chain runs on before a negative
   *  assertion, so "not called" means refused rather than not-yet-reached. */
  const settle = () => act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })

  it('refuses an all-emoji question over the byte budget with a character count', async () => {
    // 8,193 emoji = 32,772 UTF-8 bytes (over the 32,768 budget) but only 8,193 code
    // points — one past the 8,192-character floor the message tells the user to aim
    // under. `.length` would report 16,386 (surrogate pairs), so this also pins the
    // count to code points rather than UTF-16 units.
    const box = render('😀'.repeat(8_193))
    fireEvent.keyDown(box, { key: 'Enter' })
    await settle()
    expect(sideTurn).not.toHaveBeenCalled()
    expect(
      screen.getByText('Question too long — reduce to under ~8,192 characters (yours: 8,193)'),
    ).toBeInTheDocument()
  })

  it('mentions no byte count anywhere in the refusal', async () => {
    const box = render('😀'.repeat(8_193))
    fireEvent.keyDown(box, { key: 'Enter' })
    await settle()
    expect(screen.queryByText(/bytes?/i)).toBeNull()
  })

  it('refuses an ASCII question with the full byte budget as the character target', async () => {
    // ASCII costs exactly 1 UTF-8 byte per character, so the density-derived target
    // for a pure-ASCII overflow is the full byte budget — unlike the old fixed
    // 4-bytes/char floor, which told an ASCII user to cut to ~8,192 characters when
    // trimming a single character would already fit.
    const box = render('a'.repeat(32_769))
    fireEvent.keyDown(box, { key: 'Enter' })
    await settle()
    expect(sideTurn).not.toHaveBeenCalled()
    expect(
      screen.getByText('Question too long — reduce to under ~32,768 characters (yours: 32,769)'),
    ).toBeInTheDocument()
  })

  it('refuses a zh-CN question with a density-derived target between the ASCII and emoji floors', async () => {
    // CJK ideographs cost 3 UTF-8 bytes each — the density target sits between the
    // ASCII (1 byte/char, full budget) and emoji (4 bytes/char, ~8,192) cases,
    // which the old fixed floor could never report since it only knew the worst case.
    const box = render('中'.repeat(10_923))
    fireEvent.keyDown(box, { key: 'Enter' })
    await settle()
    expect(sideTurn).not.toHaveBeenCalled()
    expect(
      screen.getByText('Question too long — reduce to under ~10,922 characters (yours: 10,923)'),
    ).toBeInTheDocument()
  })

  it('still sends a question under the byte budget', async () => {
    // The control: proves this harness CAN reach `sideTurn`, so the negatives above
    // failing to are the guard working rather than the test never getting there.
    const box = render('a'.repeat(1_000))
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(sideTurn).toHaveBeenCalledTimes(1))
  })
})
