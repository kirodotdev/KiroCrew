/**
 * The breathing overlay as rendered — the parts the pure timeline cannot cover.
 *
 * Time is driven by faking `requestAnimationFrame` and stepping it, so nothing here
 * waits out the real 79-second exercise.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { StrictMode } from 'react'
import { render, screen, act } from '@testing-library/react'
import BreathingOverlay from '../apps/crew-companion/BreathingOverlay'
import { READY_MS, TOTAL_MS } from '../apps/crew-companion/breathing'

/** Frame driver: hands the component whatever timestamp we choose. */
let frames: Array<(ts: number) => void> = []

beforeEach(() => {
  frames = []
  vi.stubGlobal('requestAnimationFrame', (cb: (ts: number) => void) => {
    frames.push(cb)
    return frames.length
  })
  vi.stubGlobal('cancelAnimationFrame', () => {})
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/** Advance to `ms` elapsed. The first frame establishes the start timestamp. */
function stepTo(ms: number) {
  act(() => {
    const pending = frames
    frames = []
    // The component records its start on the first frame it sees, so frame one is
    // always t=0 and later frames are offsets from it.
    for (const cb of pending) cb(ms)
  })
}

describe('BreathingOverlay', () => {
  it('opens in the lead-in, not mid-breath', () => {
    render(<BreathingOverlay onDone={vi.fn()} onEnd={vi.fn()} />)
    stepTo(0)
    // 3s lead-in counts 3, 2, 1 — never starting the first inhale mid-thought.
    expect(screen.getByText('3')).toBeTruthy()
  })

  it('reaches the first inhale after the lead-in', () => {
    render(<BreathingOverlay onDone={vi.fn()} onEnd={vi.fn()} />)
    stepTo(0)
    stepTo(READY_MS + 100)
    // The 4s inhale, so the count reads 4.
    expect(screen.getByText('4')).toBeTruthy()
  })

  it('calls onDone when the exercise completes', () => {
    const onDone = vi.fn()
    render(<BreathingOverlay onDone={onDone} onEnd={vi.fn()} />)
    stepTo(0)
    stepTo(TOTAL_MS)
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('calls onDone once even when two frame loops are running', () => {
    /**
     * The real double-fire hazard, and the only one the fire-once ref guards.
     *
     * A single loop cannot fire twice — it returns without scheduling another frame
     * — so asserting "once" on one loop proves nothing about the guard. StrictMode
     * mounts effects twice, which genuinely leaves two loops racing the same
     * completion, and a doubled call would double-count the session in Memories.
     *
     * Verified by reverting: removing `!doneFiredRef.current` makes this fail with
     * 2 calls, while the single-loop test above still passes.
     */
    const onDone = vi.fn()
    render(
      <StrictMode>
        <BreathingOverlay onDone={onDone} onEnd={vi.fn()} />
      </StrictMode>,
    )
    stepTo(0)
    stepTo(TOTAL_MS)
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('does not call onDone before the final cycle', () => {
    const onDone = vi.fn()
    render(<BreathingOverlay onDone={onDone} onEnd={vi.fn()} />)
    stepTo(0)
    stepTo(TOTAL_MS - 1)
    expect(onDone).not.toHaveBeenCalled()
  })

  it('ends early on Escape — a suggestion, never a commitment', () => {
    const onEnd = vi.fn()
    render(<BreathingOverlay onDone={vi.fn()} onEnd={onEnd} />)
    stepTo(0)
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    })
    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('ends early from the close control', () => {
    const onEnd = vi.fn()
    render(<BreathingOverlay onDone={vi.fn()} onEnd={onEnd} />)
    stepTo(0)
    screen.getByRole('button').click()
    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('shows one dot per cycle', () => {
    const { container } = render(<BreathingOverlay onDone={vi.fn()} onEnd={vi.fn()} />)
    stepTo(0)
    expect(container.querySelectorAll('.cc-breathe-dot')).toHaveLength(4)
  })

  it('fills a dot as each cycle completes', () => {
    const { container } = render(<BreathingOverlay onDone={vi.fn()} onEnd={vi.fn()} />)
    stepTo(0)
    expect(container.querySelectorAll('.cc-breathe-dot-on')).toHaveLength(0)
    stepTo(READY_MS + 100) // into cycle 1
    expect(container.querySelectorAll('.cc-breathe-dot-on')).toHaveLength(1)
  })

  it('styles the breathing route down without reordering the phrase', () => {
    /**
     * The route ("through your nose") is muted relative to the verb, but it is one
     * translated string per language because word order differs — English puts the
     * manner after the verb, Chinese before it. This pins that the route renders as
     * its own styled span rather than being concatenated in code.
     */
    const { container } = render(<BreathingOverlay onDone={vi.fn()} onEnd={vi.fn()} />)
    stepTo(0)
    stepTo(READY_MS + 100) // inhale
    const route = container.querySelector('.cc-breathe-route')
    expect(route).toBeTruthy()
    expect(route?.textContent?.trim()).toBeTruthy()
  })

  it('has no route span during the hold', () => {
    // The hold has no nose/mouth cue to give, so the label is the verb alone.
    const { container } = render(<BreathingOverlay onDone={vi.fn()} onEnd={vi.fn()} />)
    stepTo(0)
    stepTo(READY_MS + 5_000) // 4-11s is the hold
    expect(container.querySelector('.cc-breathe-route')).toBeNull()
  })

  it('is announced as a modal dialog', () => {
    render(<BreathingOverlay onDone={vi.fn()} onEnd={vi.fn()} />)
    stepTo(0)
    expect(screen.getByRole('dialog')).toBeTruthy()
  })
})
