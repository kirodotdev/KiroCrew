import { describe, it, expect, beforeEach } from 'vitest'
import {
  commitProfilingEnabled,
  recordCommit,
  clearCommits,
  getCommits,
  summarizeCommits,
  renderCommitSummary,
  withCommitProfiler,
  type CommitRecord,
} from '../lib/commitProfiler'

function rec(over: Partial<CommitRecord> = {}): CommitRecord {
  return {
    id: 'app',
    phase: 'update',
    actualDuration: 5,
    baseDuration: 8,
    commitTime: 100,
    ...over,
  }
}

describe('commitProfilingEnabled', () => {
  it('is off with no flag anywhere', () => {
    expect(commitProfilingEnabled('', { getItem: () => null })).toBe(false)
  })

  it('arms from the URL flag', () => {
    expect(commitProfilingEnabled('?profile=commits', { getItem: () => null })).toBe(true)
  })

  it('ignores a different profile value', () => {
    expect(commitProfilingEnabled('?profile=other', { getItem: () => null })).toBe(false)
  })

  it('arms from localStorage', () => {
    expect(commitProfilingEnabled('', { getItem: () => '1' })).toBe(true)
  })

  it('treats any other stored value as off', () => {
    expect(commitProfilingEnabled('', { getItem: () => '0' })).toBe(false)
    expect(commitProfilingEnabled('', { getItem: () => 'true' })).toBe(false)
  })

  it('survives storage access throwing, as it does in some privacy modes', () => {
    expect(
      commitProfilingEnabled('', {
        getItem: () => {
          throw new Error('blocked')
        },
      })
    ).toBe(false)
  })

  it('survives a malformed query string rather than breaking startup', () => {
    expect(commitProfilingEnabled('%%%', { getItem: () => null })).toBe(false)
  })
})

describe('summarizeCommits', () => {
  beforeEach(() => clearCommits())

  it('aggregates per id with mounts and updates split out', () => {
    const rows = summarizeCommits([
      rec({ id: 'a', phase: 'mount', actualDuration: 10, baseDuration: 10 }),
      rec({ id: 'a', actualDuration: 4, baseDuration: 9 }),
      rec({ id: 'b', actualDuration: 1, baseDuration: 1 }),
    ])
    const a = rows.find((r) => r.id === 'a')!
    expect(a.commits).toBe(2)
    expect(a.mounts).toBe(1)
    expect(a.updates).toBe(1)
    expect(a.totalMs).toBe(14)
    expect(a.maxMs).toBe(10)
    expect(a.meanMs).toBe(7)
    // baseDuration - actualDuration, floored at 0 per record: (0) + (5).
    expect(a.savedMs).toBe(5)
  })

  it('sorts heaviest total first', () => {
    const rows = summarizeCommits([
      rec({ id: 'light', actualDuration: 1 }),
      rec({ id: 'heavy', actualDuration: 50 }),
    ])
    expect(rows[0].id).toBe('heavy')
  })

  it('never reports negative savings when actual exceeds base', () => {
    const rows = summarizeCommits([rec({ actualDuration: 10, baseDuration: 2 })])
    expect(rows[0].savedMs).toBe(0)
  })

  it('ignores malformed records instead of producing NaN', () => {
    const rows = summarizeCommits([
      rec({ actualDuration: Number.NaN, baseDuration: Number.NaN }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      null as any,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { id: 42 } as any,
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].totalMs).toBe(0)
    expect(Number.isNaN(rows[0].meanMs)).toBe(false)
  })

  it('returns nothing for no input', () => {
    expect(summarizeCommits([])).toEqual([])
  })
})

describe('the record buffer', () => {
  beforeEach(() => clearCommits())

  it('is bounded so a long session cannot grow it without limit', () => {
    for (let i = 0; i < 2100; i++) recordCommit(rec({ commitTime: i }))
    const all = getCommits()
    expect(all.length).toBe(2000)
    // Oldest dropped first: the retained window is the most recent.
    expect(all[all.length - 1].commitTime).toBe(2099)
    expect(all[0].commitTime).toBe(100)
  })

  it('clears', () => {
    recordCommit(rec())
    clearCommits()
    expect(getCommits()).toHaveLength(0)
  })
})

describe('renderCommitSummary', () => {
  it('explains itself when empty rather than printing an empty table', () => {
    expect(renderCommitSummary([])).toContain('No commits recorded')
  })

  it('renders a row per id', () => {
    const out = renderCommitSummary(summarizeCommits([rec({ id: 'chat' })]))
    expect(out).toContain('chat')
    expect(out).toContain('TOTAL')
  })
})

describe('withCommitProfiler', () => {
  it('returns children untouched when disarmed, adding nothing to the tree', () => {
    const children = 'child-node'
    // No URL flag and no storage key in the happy-dom default environment.
    expect(withCommitProfiler('app', children)).toBe(children)
  })
})
