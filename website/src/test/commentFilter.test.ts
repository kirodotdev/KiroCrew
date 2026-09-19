import { describe, it, expect } from 'vitest'
import { filterCommentsForForward } from '../lib/commentFilter'
import type { ArtifactComment } from '../types'

/** Helper to build a minimal ArtifactComment for testing. */
function mkComment(overrides: Partial<ArtifactComment> & { id: string }): ArtifactComment {
  return {
    origin: 'local',
    scope: 'private',
    author: 'tester',
    is_agent: false,
    body: 'some body',
    thread_id: overrides.id,
    status: 'open',
    sync_state: 'local_only',
    created_at: '2026-07-22T00:00:00Z',
    updated_at: '2026-07-22T00:00:00Z',
    ...overrides,
  }
}

describe('filterCommentsForForward', () => {
  it('excludes resolved root comments', () => {
    const comments = [
      mkComment({ id: 'r1', status: 'resolved' }),
      mkComment({ id: 'r2', status: 'open' }),
    ]
    expect(filterCommentsForForward(comments).map(c => c.id)).toEqual(['r2'])
  })

  it('excludes replies whose root is resolved', () => {
    const comments = [
      mkComment({ id: 'root', status: 'resolved' }),
      mkComment({ id: 'reply', parent_id: 'root', thread_id: 'root', status: 'open' }),
    ]
    expect(filterCommentsForForward(comments)).toHaveLength(0)
  })

  it('keeps a reply whose root is still open', () => {
    const comments = [
      mkComment({ id: 'root', status: 'open' }),
      mkComment({ id: 'reply', parent_id: 'root', thread_id: 'root', status: 'open' }),
    ]
    expect(filterCommentsForForward(comments)).toHaveLength(2)
  })

  it('keeps review status — addressed but not yet confirmed still forwards', () => {
    expect(
      filterCommentsForForward([mkComment({ id: 'rev', status: 'review' })]).map(c => c.id),
    ).toEqual(['rev'])
  })

  it('keeps comments anchored to an older version', () => {
    // An older anchor is NOT staleness: the quoted span usually still exists,
    // so this is live feedback. Dropping it would silently stop forwarding a
    // thread the sidebar still shows as open. `anchor_orphaned` marks the
    // genuinely stale case and the UI warns on it.
    const comments = [
      mkComment({ id: 'old', anchor: { quote: 'x', version_number: 1 } }),
      mkComment({ id: 'cur', anchor: { quote: 'y', version_number: 9 } }),
    ]
    expect(filterCommentsForForward(comments).map(c => c.id)).toEqual(['old', 'cur'])
  })

  it('still drops an old-version comment once it is resolved', () => {
    const comments = [
      mkComment({ id: 'old', status: 'resolved', anchor: { quote: 'x', version_number: 1 } }),
      mkComment({ id: 'cur', anchor: { quote: 'y', version_number: 9 } }),
    ]
    expect(filterCommentsForForward(comments).map(c => c.id)).toEqual(['cur'])
  })

  it('keeps orphaned anchors — the human decides, not the forwarding path', () => {
    const comments = [
      mkComment({ id: 'orph', anchor: { quote: 'gone' }, anchor_orphaned: true }),
    ]
    expect(filterCommentsForForward(comments).map(c => c.id)).toEqual(['orph'])
  })

  it('terminates on a parent cycle', () => {
    const comments = [
      mkComment({ id: 'a', parent_id: 'b' }),
      mkComment({ id: 'b', parent_id: 'a' }),
    ]
    expect(filterCommentsForForward(comments)).toHaveLength(2)
  })

  it('treats a comment with a missing parent as its own root', () => {
    const comments = [mkComment({ id: 'orphan', parent_id: 'gone' })]
    expect(filterCommentsForForward(comments).map(c => c.id)).toEqual(['orphan'])
  })

  it('returns an empty array for empty input', () => {
    expect(filterCommentsForForward([])).toEqual([])
  })
})
