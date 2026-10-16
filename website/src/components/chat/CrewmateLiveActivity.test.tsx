import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import CrewmateLiveActivity from './CrewmateLiveActivity'
import { HOLD_BEFORE_THINKING_MS, HOLD_STEP_MS } from './useHeldActivity'
import { STREAM_IDLE_MS } from '../../pages/chat/ChatFooter'
import type { PillActivity } from '../../pages/members/pillActivity'

const tool = (title: string): PillActivity => ({ kind: 'tool', text: title, fullText: title })
const THINKING: PillActivity = { kind: 'thinking', text: 'Thinking' }
const line = () => screen.getByTestId('crewmate-live-activity')
const bubble = () => screen.getByTestId('crewmate-typing-bubble')
const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })

describe('CrewmateLiveActivity holds each step so the line stays calm', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('keeps a finished tool title on screen before falling back to Thinking', () => {
    const { rerender } = render(<CrewmateLiveActivity activity={tool('Read README.md')} />)
    rerender(<CrewmateLiveActivity activity={THINKING} />)
    expect(line()).toHaveTextContent('Read README.md')
    advance(HOLD_BEFORE_THINKING_MS - 1)
    expect(line()).toHaveTextContent('Read README.md')
    advance(1)
    expect(line()).toHaveTextContent('Thinking')
    expect(line()).toHaveAttribute('data-activity', 'thinking')
  })

  it('a burst of fast tools shows each title for the minimum, never Thinking in between', () => {
    const { rerender } = render(<CrewmateLiveActivity activity={tool('Step A')} />)
    rerender(<CrewmateLiveActivity activity={THINKING} />)
    advance(50)
    rerender(<CrewmateLiveActivity activity={tool('Step B')} />)
    expect(line()).toHaveTextContent('Step A')
    advance(HOLD_STEP_MS - 50)
    expect(line()).toHaveTextContent('Step B')
  })

  it('a tool that ended inside the hold still shows before Thinking', () => {
    const { rerender } = render(<CrewmateLiveActivity activity={tool('Step A')} />)
    rerender(<CrewmateLiveActivity activity={tool('Step B')} />)
    advance(100)
    rerender(<CrewmateLiveActivity activity={THINKING} />)
    advance(HOLD_STEP_MS - 100)
    expect(line()).toHaveTextContent('Step B')
    advance(HOLD_BEFORE_THINKING_MS)
    expect(line()).toHaveTextContent('Thinking')
  })

  it('a newer reading supersedes a pending swap: the old timer is cleared, never fires late', () => {
    const { rerender } = render(<CrewmateLiveActivity activity={tool('Step A')} />)
    // Thinking is pending for 1.5s; Step B arrives first and takes the line.
    rerender(<CrewmateLiveActivity activity={THINKING} />)
    advance(100)
    rerender(<CrewmateLiveActivity activity={tool('Step B')} />)
    advance(HOLD_STEP_MS - 100)
    expect(line()).toHaveTextContent('Step B')
    // Past the moment the stale Thinking timer would have fired: still Step B.
    advance(HOLD_BEFORE_THINKING_MS - HOLD_STEP_MS)
    expect(line()).toHaveTextContent('Step B')
  })

  it('swaps to stopping at once, the caption spells it (no footer under it does), and the dots fold', () => {
    const { rerender } = render(<CrewmateLiveActivity activity={tool('Step A')} />)
    expect(bubble().dataset.shown).toBe('true')
    rerender(<CrewmateLiveActivity activity={{ kind: 'stopping' }} />)
    advance(0)
    expect(line()).toHaveAttribute('data-activity', 'stopping')
    expect(line()).toHaveTextContent('Stopping…')
    // Nothing is being typed while a Stop lands: dots under "Stopping…" would contradict it.
    expect(bubble().dataset.shown).toBe('false')
    expect(line().querySelector('.animate-pulse')).not.toBeNull()
  })

  it('folds the dots while compacting too; the caption then carries the motion (pulse + hourglass)', () => {
    const { rerender } = render(<CrewmateLiveActivity activity={THINKING} />)
    expect(line().querySelector('.animate-pulse')).toBeNull()
    rerender(<CrewmateLiveActivity activity={{ kind: 'compacting' }} />)
    advance(0)
    expect(line()).toHaveTextContent('Compacting…')
    expect(bubble().dataset.shown).toBe('false')
    expect(line().querySelector('.animate-pulse')).not.toBeNull()
    expect(line().querySelector('.animate-pulse svg')).not.toBeNull()
  })
})

describe('CrewmateLiveActivity draws the typing bubble over the caption', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('three dots in the mate\'s own bubble, decorative; the caption is the live region', () => {
    render(<CrewmateLiveActivity activity={THINKING} />)
    const dots = bubble().querySelector('.message-bubble.crewmate-bubble .typing-dots')
    expect(dots?.children).toHaveLength(3)
    expect(bubble()).toHaveAttribute('aria-hidden', 'true')
    expect(bubble().dataset.shown).toBe('true')
    expect(line().querySelector('[role="status"]')).toHaveTextContent('Thinking')
  })

  it('folds the bubble while the reply\'s text is arriving and brings it back once the stream goes quiet', () => {
    const { rerender } = render(<CrewmateLiveActivity activity={{ kind: 'writing' }} streamingText streamTick={1} />)
    expect(bubble().dataset.shown).toBe('false')
    // Still folded while chunks keep landing: each tick restarts the quiet clock.
    advance(STREAM_IDLE_MS - 1)
    rerender(<CrewmateLiveActivity activity={{ kind: 'writing' }} streamingText streamTick={2} />)
    advance(STREAM_IDLE_MS - 1)
    expect(bubble().dataset.shown).toBe('false')
    advance(1)
    expect(bubble().dataset.shown).toBe('true')
    // The caption stays mounted throughout: it is the row that never moves.
    expect(line()).toHaveTextContent('Writing…')
  })
})
