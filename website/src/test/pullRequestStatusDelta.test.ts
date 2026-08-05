import { describe, it, expect } from 'vitest'
import { applyStatusDelta, parseStatusDelta } from '../utils/pullRequestStatusDelta'
import type { PullRequestStatusBatch } from '../types'

const URL_A = 'https://github.com/acme/repo/pull/7'
const URL_B = 'https://github.com/acme/repo/pull/8'

describe('parseStatusDelta', () => {
  it('accepts a well-formed delta', () => {
    expect(parseStatusDelta({ url: URL_A, state: 'merged', ci: 'passed', origin: 'chip' })).toEqual({
      url: URL_A, state: 'merged', ci: 'passed', origin: 'chip',
    })
  })

  it('drops unknown vocabulary instead of trusting the wire', () => {
    // The websocket payload is untrusted input: a bogus state must not reach the
    // chip renderer, which switches on the exact lifecycle vocabulary.
    expect(parseStatusDelta({ url: URL_A, state: 'exploded', ci: 'sideways', origin: 'x' })).toEqual({
      url: URL_A,
    })
  })

  it('rejects payloads with no usable url', () => {
    expect(parseStatusDelta({ state: 'merged' })).toBeNull()
    expect(parseStatusDelta({ url: '' })).toBeNull()
    expect(parseStatusDelta(null)).toBeNull()
    expect(parseStatusDelta('nope')).toBeNull()
  })
})

describe('applyStatusDelta', () => {
  const batch: PullRequestStatusBatch = {
    statuses: { [URL_A]: { state: 'open', ci: 'running' }, [URL_B]: { state: 'open' } },
    refreshing: [URL_A, URL_B],
    ttlSecs: 60,
  }

  it('overwrites the delta url and leaves the rest of the batch alone', () => {
    const next = applyStatusDelta(batch, { url: URL_A, state: 'merged' })

    expect(next?.statuses[URL_A]).toEqual({ state: 'merged' })
    expect(next?.statuses[URL_B]).toEqual({ state: 'open' })
    expect(next?.ttlSecs).toBe(60)
  })

  it('clears the url from refreshing so the panel drops its fast follow-up poll', () => {
    const next = applyStatusDelta(batch, { url: URL_A, state: 'merged' })

    expect(next?.refreshing).toEqual([URL_B])
  })

  it('returns the same object when nothing changed', () => {
    // Identity is the signal react-query subscribers use to skip a re-render.
    expect(applyStatusDelta(batch, { url: URL_A, state: 'open', ci: 'running' })).toBe(batch)
  })

  it('records a url the batch has never seen', () => {
    const next = applyStatusDelta(batch, { url: 'https://github.com/acme/repo/pull/9', ci: 'failed' })

    expect(next?.statuses['https://github.com/acme/repo/pull/9']).toEqual({ ci: 'failed' })
  })

  it('leaves an unfetched batch alone', () => {
    expect(applyStatusDelta(undefined, { url: URL_A, state: 'merged' })).toBeUndefined()
  })

  it('ignores a delta whose fields were all stripped rather than blanking the entry', () => {
    // Version skew: a stale tab receives a delta whose state/ci the server grew
    // but this client does not recognize, so parseStatusDelta drops both. The
    // resulting field-less delta carries no information — writing it would blank
    // URL_A's known {state, ci} glyphs, which is worse than the stale value it
    // would replace. Return the same object so react-query skips the re-render.
    const stripped = parseStatusDelta({ url: URL_A, state: 'teleported', ci: 'quantum' })
    expect(stripped).toEqual({ url: URL_A })
    expect(applyStatusDelta(batch, stripped!)).toBe(batch)
  })

  // The merge pair rides the same event. A branch that starts conflicting while
  // the panel is open changes only these fields, so a delta carrying nothing else
  // still has to land -- otherwise the merge-blocker banner waits for a refresh.
  it('carries a merge-only change', () => {
    const delta = parseStatusDelta({ url: URL_A, mergeable: 'conflicting', mergeStateStatus: 'dirty' })

    expect(delta).toEqual({ url: URL_A, mergeable: 'conflicting', mergeStateStatus: 'dirty' })
    expect(applyStatusDelta(batch, delta!)?.statuses[URL_A])
      .toEqual({ mergeable: 'conflicting', mergeStateStatus: 'dirty' })
  })

  it('carries a GitLab detail-only answer, where mergeable never settles', () => {
    const delta = parseStatusDelta({ url: URL_A, state: 'open', mergeStateStatus: 'need_rebase' })

    expect(delta).toEqual({ url: URL_A, state: 'open', mergeStateStatus: 'need_rebase' })
  })

  it('rejects merge values that are not the shape of the normalized vocabulary', () => {
    const delta = parseStatusDelta({
      url: URL_A,
      state: 'open',
      mergeable: '<img src=x>',
      mergeStateStatus: 'x'.repeat(33),
    })

    expect(delta).toEqual({ url: URL_A, state: 'open' })
  })

  it('re-renders when only the merge pair moved', () => {
    const conflicting = applyStatusDelta(batch, {
      url: URL_A, state: 'open', ci: 'running', mergeable: 'conflicting',
    })
    expect(conflicting).not.toBe(batch)
    // ...and still skips the re-render when the whole entry is unchanged.
    expect(applyStatusDelta(conflicting, {
      url: URL_A, state: 'open', ci: 'running', mergeable: 'conflicting',
    })).toBe(conflicting)
  })
})
