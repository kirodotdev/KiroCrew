/**
 * The streaming reveal edge must settle to full opacity when the stream goes
 * quiet.
 *
 * `rehypeStreamingReveal` gives each of the last REVEAL_FADE_CHARS characters a
 * POSITIONAL opacity (`--ft-o`, as low as REVEAL_MIN_OPACITY at the tip). Only
 * the tip advancing raises a character's opacity, so a stream that pauses
 * mid-turn -- the gap while the model composes tool arguments -- or one that has
 * finished leaves its trailing characters visibly dim with nothing to advance
 * the tip. `.ft-idle` is what ends that state; without it the tail stays faded.
 */
import { render, screen, act } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import MarkdownRenderer from '../components/MarkdownRenderer'

const LONG = 'The quick brown fox jumps over the lazy dog and keeps on running.'

describe('streaming reveal idle settle', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const root = () => screen.getByTestId('mdwrap').firstElementChild as HTMLElement

  function mount(content: string) {
    return render(
      <div data-testid="mdwrap">
        <MarkdownRenderer content={content} streaming smooth />
      </div>,
    )
  }

  it('does not settle while content is still arriving', () => {
    const { rerender } = mount(LONG)
    // Advance most of the way, then deliver another chunk: the timer must re-arm.
    act(() => void vi.advanceTimersByTime(400))
    rerender(
      <div data-testid="mdwrap">
        <MarkdownRenderer content={`${LONG} more text`} streaming smooth />
      </div>,
    )
    act(() => void vi.advanceTimersByTime(400))
    expect(root().className).not.toContain('ft-idle')
  })

  it('settles the edge after the idle window elapses', () => {
    mount(LONG)
    expect(root().className).not.toContain('ft-idle')
    act(() => void vi.advanceTimersByTime(500))
    expect(root().className).toContain('ft-idle')
  })

  it('keeps the smooth-mode class so the settle overrides --ft-o rather than dropping the reveal', () => {
    mount(LONG)
    act(() => void vi.advanceTimersByTime(500))
    const cls = root().className
    expect(cls).toContain('ft-anim-smooth')
    expect(cls).toContain('ft-idle')
  })

  it('keeps the settle latched when a chunk arrives after it settled', () => {
    // The monotonicity guarantee. Spans persist across chunks and `--ft-o` is
    // per-slot, so un-settling would transition the whole edge from 1 back down
    // to --ft-o -- an inverse fade. The settle is therefore one-way: once on, it
    // stays on for the life of the row. Re-introducing `setRevealIdle(false)`
    // into the effect fails this case.
    const { rerender } = mount(LONG)
    act(() => void vi.advanceTimersByTime(500))
    expect(root().className).toContain('ft-idle')
    rerender(
      <div data-testid="mdwrap">
        <MarkdownRenderer content={`${LONG} and then some more arrives`} streaming smooth />
      </div>,
    )
    expect(root().className).toContain('ft-idle')
    // Still latched after further chunks and further time.
    act(() => void vi.advanceTimersByTime(1000))
    expect(root().className).toContain('ft-idle')
  })

  it('never settles when smooth mode is off (no reveal edge to settle)', () => {
    render(
      <div data-testid="mdwrap">
        <MarkdownRenderer content={LONG} streaming />
      </div>,
    )
    act(() => void vi.advanceTimersByTime(2000))
    expect(root().className).not.toContain('ft-idle')
  })
})
