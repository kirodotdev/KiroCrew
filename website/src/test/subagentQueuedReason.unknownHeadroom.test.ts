import { describe, it, expect } from 'vitest'
import { parseSubagentQueuedReason, queuedWaitText } from '../pages/chat/subagentQueuedReason'

const UNREADABLE = 'cgroup_usage_unreadable'

describe('queued wait on an unreadable cgroup usage file', () => {
  it('keeps the cause on a low-memory label', () => {
    const r = parseSubagentQueuedReason({ slot: 's', queued: 1, reason: 'low_memory', required_gb: 4, cause: UNREADABLE })
    expect(r).toEqual({ reason: 'low_memory', required_gb: 4, cause: UNREADABLE })
  })

  it('drops a cause it does not render, and any cause on another kind', () => {
    expect(parseSubagentQueuedReason({ slot: 's', queued: 1, reason: 'low_memory', cause: 'host_read_unanswered' })?.cause)
      .toBeUndefined()
    expect(parseSubagentQueuedReason({ slot: 's', queued: 1, reason: 'memory_pressure', cause: UNREADABLE })?.cause)
      .toBeUndefined()
  })

  it('says headroom is unknown, never a zero figure, for each wait-bound tail', () => {
    const base = { reason: 'low_memory' as const, cause: UNREADABLE as typeof UNREADABLE, available_gb: 0, required_gb: 4 }
    const cases: Array<[number | undefined, RegExp]> = [
      [undefined, /give the gateway read access to that file to continue$/],
      [0, /they wait with no time limit$/],
      [1800, /they never start if it stays unknown for 30\sminutes$/],
    ]
    for (const [max_wait_secs, tail] of cases) {
      const text = queuedWaitText({ ...base, ...(max_wait_secs === undefined ? {} : { max_wait_secs }) })
      expect(text).toContain('free memory is unknown')
      expect(text).toMatch(tail)
      expect(text).not.toMatch(/free now|0 GB/)
    }
  })

  it('leaves a measured low-memory wait on its figures', () => {
    const text = queuedWaitText({ reason: 'low_memory', available_gb: 1, required_gb: 4 })
    expect(text).toContain('free now')
    expect(text).not.toContain('unknown')
  })
})
