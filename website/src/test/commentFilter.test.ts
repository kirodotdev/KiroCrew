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

  it('drops a nested reply whose middle parent is gone but whose root is resolved', () => {
    // Deleting the middle reply leaves the leaf with a parent_id that resolves
    // to nothing, so walking parents alone stops at the leaf and forwards it.
    // thread_id still names the root.
    const comments = [
      mkComment({ id: 'root', status: 'resolved' }),
      mkComment({ id: 'leaf', parent_id: 'middle-deleted', thread_id: 'root' }),
    ]
    expect(filterCommentsForForward(comments)).toHaveLength(0)
  })

  it('terminates on a parent cycle', () => {
    // thread_id: '' is the pre-thread_id shape that exercises the fallback walk.
    const comments = [
      mkComment({ id: 'a', parent_id: 'b', thread_id: '' }),
      mkComment({ id: 'b', parent_id: 'a', thread_id: '' }),
    ]
    expect(filterCommentsForForward(comments)).toHaveLength(2)
  })

  it('treats a comment with a missing parent as its own root', () => {
    const comments = [mkComment({ id: 'orphan', parent_id: 'gone', thread_id: '' })]
    expect(filterCommentsForForward(comments).map(c => c.id)).toEqual(['orphan'])
  })

  it('inherits root status through the fallback parent walk', () => {
    const comments = [
      mkComment({ id: 'root', status: 'resolved', thread_id: '' }),
      mkComment({ id: 'reply', parent_id: 'root', thread_id: '' }),
    ]
    expect(filterCommentsForForward(comments)).toHaveLength(0)
  })

  it('returns an empty array for empty input', () => {
    expect(filterCommentsForForward([])).toEqual([])
  })
})
