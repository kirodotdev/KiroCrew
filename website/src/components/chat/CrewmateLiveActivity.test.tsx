import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import CrewmateLiveActivity from './CrewmateLiveActivity'
import { HOLD_BEFORE_THINKING_MS, HOLD_STEP_MS } from './useHeldActivity'
import type { PillActivity } from '../../pages/members/pillActivity'

const tool = (title: string): PillActivity => ({ kind: 'tool', text: title, fullText: title })
const THINKING: PillActivity = { kind: 'thinking', text: 'Thinking' }
const line = () => screen.getByTestId('crewmate-live-activity')
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

  it('swaps to stopping at once', () => {
    const { rerender } = render(<CrewmateLiveActivity activity={tool('Step A')} />)
    rerender(<CrewmateLiveActivity activity={{ kind: 'stopping' }} />)
    advance(0)
    expect(line()).toHaveAttribute('data-activity', 'stopping')
  })
})
