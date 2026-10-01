import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import { CONFIRM_DEAD_TIME_MS, holdOverLimitSend, useOverLimitSendConfirm } from '../components/useOverLimitSendConfirm'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

// Counts the paste-chip expansion the hook performs. A plain counter rather
// than a `vi.fn`, so `restoreAllMocks` between tests cannot drop the wrapper.
const { expansions } = vi.hoisted(() => ({ expansions: { n: 0 } }))
vi.mock('../components/composerPromptLength', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/composerPromptLength')>()
  return {
    ...actual,
    sentPromptText: (value: string, blocks: readonly PasteBlock[]) => {
      expansions.n++
      return actual.sentPromptText(value, blocks)
    },
  }
})

// 100-token window: 480 ASCII characters is ~120 tokens, over the limit.
const WINDOW = 100
const OVER = 'a'.repeat(480)
const UNDER = 'a'.repeat(100)

describe('holdOverLimitSend', () => {
  it('holds a first send of an over-limit prompt', () => {
    expect(holdOverLimitSend(OVER, [], WINDOW, null)).toBe(true)
  })

  it('lets the repeat of the same draft through', () => {
    expect(holdOverLimitSend(OVER, [], WINDOW, OVER)).toBe(false)
  })

  it('holds again when the draft changed since the first attempt', () => {
    expect(holdOverLimitSend(OVER + 'b', [], WINDOW, OVER)).toBe(true)
  })

  it('never holds a prompt under the limit or when the window is unknown', () => {
    expect(holdOverLimitSend(UNDER, [], WINDOW, null)).toBe(false)
    expect(holdOverLimitSend('a'.repeat(390), [], WINDOW, null)).toBe(false) // near, not over
    expect(holdOverLimitSend(OVER, [], 0, null)).toBe(false)
    expect(holdOverLimitSend(OVER, [], undefined, null)).toBe(false)
  })

  it('counts expanded paste content', () => {
    const block: PasteBlock = { id: 'p', seq: 1, lines: 1, content: OVER }
    expect(holdOverLimitSend(formatToken(block), [block], WINDOW, null)).toBe(true)
  })
})

describe('useOverLimitSendConfirm', () => {
  // Each Date.now() read moves the clock a second on, so two sends in a row are
  // never inside the confirm dead time unless a test pins the clock.
  let clock = 0
  beforeEach(() => { clock = 0; vi.spyOn(Date, 'now').mockImplementation(() => (clock += 1_000)) })
  afterEach(() => { vi.restoreAllMocks() })

  it('keeps holding a confirm that lands inside the dead time', () => {
    vi.spyOn(Date, 'now').mockReturnValue(5_000)
    const { result } = renderHook(() => useOverLimitSendConfirm(OVER, [], WINDOW))
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
    const swallowed = 5_000 + CONFIRM_DEAD_TIME_MS - 1
    vi.spyOn(Date, 'now').mockReturnValue(swallowed)
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
    expect(result.current.pending).toBe(true)
    // The window is measured from the send it just swallowed, not from the
    // first one, so the boundary moved on with it.
    vi.spyOn(Date, 'now').mockReturnValue(swallowed + CONFIRM_DEAD_TIME_MS - 1)
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
    vi.spyOn(Date, 'now').mockReturnValue(swallowed + CONFIRM_DEAD_TIME_MS - 1 + CONFIRM_DEAD_TIME_MS)
    act(() => { held = result.current.intercept() })
    expect(held).toBe(false)
  })

  // A focused Send button is re-activated by the browser on a held key's
  // auto-repeat, and that arrives as a plain click with no repeat flag.
  it('holds the first auto-repeat of a held key, which lands ~500 ms in', () => {
    vi.spyOn(Date, 'now').mockReturnValue(5_000)
    const { result } = renderHook(() => useOverLimitSendConfirm(OVER, [], WINDOW))
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
    vi.spyOn(Date, 'now').mockReturnValue(5_500)
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
    expect(result.current.pending).toBe(true)
  })

  it('holds a two-second repeat train and sends a deliberate press after it', () => {
    let t = 5_000
    vi.spyOn(Date, 'now').mockImplementation(() => t)
    const { result } = renderHook(() => useOverLimitSendConfirm(OVER, [], WINDOW))
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
    t = 5_500 // the first auto-repeat
    for (; t <= 7_500; t += 30) {
      act(() => { held = result.current.intercept() })
      expect(held).toBe(true)
    }
    const lastRepeat = t - 30
    // Key released, then pressed again past the window.
    t = lastRepeat + CONFIRM_DEAD_TIME_MS
    act(() => { held = result.current.intercept() })
    expect(held).toBe(false)
  })

  it('holds the first send, arms, and passes the second', () => {
    const { result } = renderHook(() => useOverLimitSendConfirm(OVER, [], WINDOW))
    expect(result.current.pending).toBe(false)
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
    expect(result.current.pending).toBe(true)
    act(() => { held = result.current.intercept() })
    expect(held).toBe(false)
    expect(result.current.pending).toBe(false)
  })

  it('disarms when the draft is edited', () => {
    const { result, rerender } = renderHook(({ v }) => useOverLimitSendConfirm(v, [], WINDOW), { initialProps: { v: OVER } })
    act(() => { result.current.intercept() })
    expect(result.current.pending).toBe(true)
    rerender({ v: OVER + 'x' })
    expect(result.current.pending).toBe(false)
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
  })

  it('disarms when the context window changes', () => {
    const { result, rerender } = renderHook(
      ({ window }) => useOverLimitSendConfirm(OVER, [], window),
      { initialProps: { window: WINDOW } },
    )
    act(() => { result.current.intercept() })
    expect(result.current.pending).toBe(true)
    rerender({ window: WINDOW + 1 })
    expect(result.current.pending).toBe(false)
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
  })

  it('disarms when the session changes', () => {
    const { result, rerender } = renderHook(
      ({ session }) => useOverLimitSendConfirm(OVER, [], WINDOW, session),
      { initialProps: { session: 'slot-a' } },
    )
    act(() => { result.current.intercept() })
    expect(result.current.pending).toBe(true)
    rerender({ session: 'slot-b' })
    expect(result.current.pending).toBe(false)
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
  })

  it('never holds an ordinary prompt', () => {
    const { result } = renderHook(() => useOverLimitSendConfirm(UNDER, [], WINDOW))
    let held = true
    act(() => { held = result.current.intercept() })
    expect(held).toBe(false)
    expect(result.current.pending).toBe(false)
  })

  it('holds again after an edit is made and then undone', () => {
    const { result, rerender } = renderHook(({ v }) => useOverLimitSendConfirm(v, [], WINDOW), { initialProps: { v: OVER } })
    act(() => { result.current.intercept() })
    rerender({ v: OVER + 'x' })
    rerender({ v: OVER })
    expect(result.current.pending).toBe(false)
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
  })

  it('disarms when a paste block changes but its chip text does not', () => {
    const a: PasteBlock = { id: 'p', seq: 1, lines: 1, content: OVER }
    const b: PasteBlock = { ...a, content: OVER + 'y' }
    const chip = formatToken(a)
    expect(formatToken(b)).toBe(chip)
    const { result, rerender } = renderHook(({ bl }) => useOverLimitSendConfirm(chip, bl, WINDOW), { initialProps: { bl: [a] } })
    act(() => { result.current.intercept() })
    expect(result.current.pending).toBe(true)
    rerender({ bl: [b] })
    expect(result.current.pending).toBe(false)
    let held = false
    act(() => { held = result.current.intercept() })
    expect(held).toBe(true)
  })

  it('keeps one intercept identity across renders', () => {
    const { result, rerender } = renderHook(({ v }) => useOverLimitSendConfirm(v, [], WINDOW), { initialProps: { v: 'a' } })
    const first = result.current.intercept
    rerender({ v: 'ab' })
    expect(result.current.intercept).toBe(first)
  })

  it('expands nothing on a keystroke while nothing is armed', () => {
    const block: PasteBlock = { id: 'p', seq: 1, lines: 1, content: OVER }
    const chip = formatToken(block)
    const { rerender } = renderHook(
      ({ v }) => useOverLimitSendConfirm(v, [block], WINDOW),
      { initialProps: { v: chip } },
    )
    expansions.n = 0
    for (const v of ['a', 'ab', 'abc', 'abcd']) rerender({ v: chip + v })
    expect(expansions.n).toBe(0)
  })

  it('expands once per keystroke only while armed, and stops after the disarm', () => {
    const { result, rerender } = renderHook(
      ({ v }) => useOverLimitSendConfirm(v, [], WINDOW),
      { initialProps: { v: OVER } },
    )
    act(() => { result.current.intercept() })
    expect(result.current.pending).toBe(true)
    expansions.n = 0
    rerender({ v: OVER + 'x' })
    expect(result.current.pending).toBe(false)
    const whileArmed = expansions.n
    expect(whileArmed).toBeGreaterThan(0)
    rerender({ v: OVER + 'xy' })
    rerender({ v: OVER + 'xyz' })
    expect(expansions.n).toBe(whileArmed)
  })
})
