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
})
