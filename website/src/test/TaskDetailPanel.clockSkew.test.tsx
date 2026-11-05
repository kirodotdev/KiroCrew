/**
 * A running task's duration under clock skew: `started_at` comes from the
 * gateway's clock (`time.time()` in the task runner), and a task still running
 * has no `finished_at`, so the panel subtracts it from the browser's
 * `Date.now()`. A gateway whose clock runs ahead would otherwise render a
 * negative duration; it must read zero until the browser catches up.
 *
 * The browser's clock is pinned, so each skew below is an exact offset from a
 * fixed instant and every assertion is an exact string rather than a window.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { TaskDetail } from '../types'
import TaskDetailPanel from '../pages/aidlc/TaskDetailPanel'

const NOW_MS = 1_700_000_000_000

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_MS)
})

afterEach(() => {
  vi.useRealTimers()
})

/** A running task stamped `skewSeconds` from the pinned browser clock. A
 *  positive value puts the start in the browser's future (gateway ahead). */
function runningTask(skewSeconds: number): TaskDetail {
  return {
    index: 0,
    title: 'Build the thing',
    description: '',
    status: 'running',
    error: '',
    result: '',
    attempts: 1,
    depends_on: [],
    requires_approval: false,
    started_at: NOW_MS / 1000 + skewSeconds,
  }
}

/** The Duration field's text, read from its own label's sibling. Narrow unit
 *  formatting joins a quantity with a non-breaking space, so it is normalised
 *  to a plain one for comparison. */
function durationText(): string {
  const label = screen.getByText('Duration')
  const text = label.parentElement?.textContent?.replace('Duration', '') ?? ''
  return text.replace(/\u00A0/g, ' ')
}

describe('aidlc TaskDetailPanel duration under clock skew', () => {
  it('reads 0s, not a negative duration, when started_at is 45s ahead of the browser clock', () => {
    render(<TaskDetailPanel task={runningTask(45)} onClose={() => {}} />)
    expect(durationText()).toBe('0s')
  })

  it('still reads 0s under a ten-minute skew rather than counting down', () => {
    render(<TaskDetailPanel task={runningTask(600)} onClose={() => {}} />)
    expect(durationText()).toBe('0s')
  })

  it('control: a start 90s in the past still reads 1m 30s', () => {
    render(<TaskDetailPanel task={runningTask(-90)} onClose={() => {}} />)
    expect(durationText()).toBe('1m 30s')
  })
})
