import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CommentsSidebar } from '../components/CommentsSidebar'
import type { ArtifactComment } from '../types'

function mk(over: Partial<ArtifactComment> = {}): ArtifactComment {
  return {
    id: 'c1', origin: 'local', scope: 'private', author: 'alex', is_agent: false,
    body: 'a comment', thread_id: 'c1', status: 'open', sync_state: 'local_only',
    created_at: '2026-06-10T00:00:00Z', updated_at: '2026-06-10T00:00:00Z',
    ...over,
  }
}

function base() {
  return {
    onAdd: vi.fn(), onReply: vi.fn(), onResolve: vi.fn(), onMarkReview: vi.fn(),
    onDelete: vi.fn(), onRefresh: vi.fn(), onClose: vi.fn(),
  }
}

describe('CommentsSidebar orphaned anchors', () => {
  it('shows the orphan warning and dims a thread whose anchor text is gone', () => {
    const root = mk({
      id: 'r', thread_id: 'r', body: 'orphaned thread',
      anchor: { quote: 'gone text' }, anchor_orphaned: true,
    })
    render(<CommentsSidebar comments={[root]} {...base()} />)
    expect(screen.getByLabelText('Anchor text no longer found in content')).toBeInTheDocument()
    const row = screen.getByText('orphaned thread').closest('.group')
    expect(row).not.toBeNull()
    expect(row!.className).toContain('opacity-60')
  })

  it('does not dim or warn on a healthy anchored thread', () => {
    const root = mk({
      id: 'r', thread_id: 'r', body: 'healthy thread',
      anchor: { quote: 'still here' }, anchor_orphaned: false,
    })
    render(<CommentsSidebar comments={[root]} {...base()} />)
    expect(screen.queryByLabelText('Anchor text no longer found in content')).toBeNull()
    const row = screen.getByText('healthy thread').closest('.group')
    expect(row!.className).not.toContain('opacity-60')
  })

  it('provider push warnings still render from sync_state', () => {
    const root = mk({ id: 'r', thread_id: 'r', body: 'push failed', sync_state: 'push_failed' })
    render(<CommentsSidebar comments={[root]} {...base()} />)
    expect(screen.getByLabelText('Failed to sync to provider')).toBeInTheDocument()
  })

  it('surfaces both warnings when a pending-push comment is also orphaned', () => {
    const root = mk({
      id: 'r', thread_id: 'r', body: 'both signals',
      sync_state: 'pending_push', anchor: { quote: 'gone' }, anchor_orphaned: true,
    })
    render(<CommentsSidebar comments={[root]} {...base()} />)
    expect(screen.getByLabelText(
      'Pending sync to provider · Anchor text no longer found in content',
    )).toBeInTheDocument()
  })
})
