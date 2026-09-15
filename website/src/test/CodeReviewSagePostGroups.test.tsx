import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { sageApi } from '../apps/code-review-sage/api'

/**
 * A multi-change selection must go out as ONE request.
 *
 * `posting` is a per-RUN flag and only the background poster clears it, while the
 * POST endpoint returns as soon as it dispatches that poster. So a client sending
 * one request per change got `already_posting` (409) for every change after the
 * first, and the comments chosen on those changes were never published. Crucially,
 * sequencing the requests does NOT fix that — waiting for request N to resolve
 * still leaves the flag set, because resolution means "the poster started", not
 * "the poster finished". The fix is a single request carrying every group.
 *
 * This drives the real `sageApi.postCommentGroups` against a fetch stub rather than
 * a hand-rolled copy of the loop: an earlier version of this test mirrored the
 * implementation's shape, so it passed while the feature was still broken.
 */
describe('sageApi.postCommentGroups', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ ok: true, run_id: 'run-1', posting: true, pending: 4 }),
    }))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function bodyOf(call: number) {
    return JSON.parse((fetchMock.mock.calls[call][1] as { body: string }).body)
  }

  it('sends exactly one request for a selection spanning three changes', async () => {
    await sageApi.postCommentGroups('run-1', [
      { changeId: 'CR-1', keys: ['a'] },
      { changeId: 'CR-2', keys: ['b', 'c'] },
      { changeId: 'CR-3', keys: ['d'] },
    ])

    // One request: a second would be refused with `already_posting`.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/code-review-sage/runs/run-1/post')
    expect((fetchMock.mock.calls[0][1] as { method: string }).method).toBe('POST')
  })

  it('carries every group, each with its own keys, in that one request', async () => {
    await sageApi.postCommentGroups('run-1', [
      { changeId: 'CR-1', keys: ['a'] },
      { changeId: 'CR-2', keys: ['b', 'c'] },
    ])

    expect(bodyOf(0)).toEqual({
      groups: [
        { change_id: 'CR-1', keys: ['a'] },
        { change_id: 'CR-2', keys: ['b', 'c'] },
      ],
    })
  })

  it('omits keys for a group that posts everything still pending', async () => {
    await sageApi.postCommentGroups('run-1', [{ changeId: 'CR-1' }])

    // The backend reads an absent key list as "all pending for this change";
    // sending `keys: undefined` would serialise to a missing field anyway, but an
    // explicit empty array would mean "post nothing".
    expect(bodyOf(0)).toEqual({ groups: [{ change_id: 'CR-1' }] })
  })

  it('encodes the run id in the path', async () => {
    await sageApi.postCommentGroups('run/../1', [{ changeId: 'CR-1' }])
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/apps/code-review-sage/runs/run%2F..%2F1/post')
  })
})
