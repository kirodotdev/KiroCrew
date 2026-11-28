import { describe, it, expect } from 'vitest'
import { parseSubagentQueuedReason, queuedWaitText } from '../pages/chat/subagentQueuedReason'

const UNANSWERED = 'host_read_unanswered'
const SHORTAGE = /not enough free memory|free up memory|memory stays short|free now/

describe('queued wait on a host memory read that did not answer', () => {
  it('keeps the cause on a low-memory label, and on no other kind', () => {
    const r = parseSubagentQueuedReason({ slot: 's', queued: 1, reason: 'low_memory', required_gb: 8.3, cause: UNANSWERED })
    expect(r).toEqual({ reason: 'low_memory', required_gb: 8.3, cause: UNANSWERED })
    expect(parseSubagentQueuedReason({ slot: 's', queued: 1, reason: 'memory_pressure', cause: UNANSWERED })?.cause)
      .toBeUndefined()
  })

  it('says the reading did not answer, never a shortage, for each wait-bound tail', () => {
    const base = { reason: 'low_memory' as const, cause: UNANSWERED as typeof UNANSWERED, required_gb: 8.3 }
    const cases: Array<[number | undefined, RegExp]> = [
      [undefined, /it will run again$/],
      [0, /they wait with no time limit$/],
      [1800, /they never start if no check answers for 30\sminutes$/],
    ]
    for (const [max_wait_secs, tail] of cases) {
      const text = queuedWaitText({ ...base, ...(max_wait_secs === undefined ? {} : { max_wait_secs }) })
      expect(text).toContain('the free-memory check did not answer in time')
      expect(text).toContain('it will run again')
      expect(text).toMatch(tail)
      expect(text).not.toMatch(SHORTAGE)
    }
  })

  it('lets the cause win over a figure the frame also carries', () => {
    const text = queuedWaitText({ reason: 'low_memory', cause: UNANSWERED, available_gb: 0, required_gb: 8.3 })
    expect(text).toContain('the free-memory check did not answer in time')
    expect(text).not.toMatch(SHORTAGE)
  })

  it('gives a frame with no cause the figure-less sentence', () => {
    const r = parseSubagentQueuedReason({ slot: 's', queued: 1, reason: 'low_memory', required_gb: 8.3 })
    expect(r).toEqual({ reason: 'low_memory', required_gb: 8.3 })
    const text = queuedWaitText(r)
    expect(text).toContain('not enough free memory')
    expect(text).not.toContain('did not answer')
  })

  it('leaves a measured wait on its figures and an unreadable cgroup on unknown headroom', () => {
    const measured = queuedWaitText({ reason: 'low_memory', available_gb: 1, required_gb: 4 })
    expect(measured).toContain('free now')
    expect(measured).not.toContain('did not answer')
    const unreadable = queuedWaitText({ reason: 'low_memory', cause: 'cgroup_usage_unreadable', required_gb: 4 })
    expect(unreadable).toContain('free memory is unknown')
    expect(unreadable).not.toContain('did not answer')
  })
})
