import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
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

describe('CommentsSidebar unsaved-draft report', () => {
  it('reports the add box only while it holds text', () => {
    const onDraftDirtyChange = vi.fn()
    render(<CommentsSidebar comments={[]} {...base()} onDraftDirtyChange={onDraftDirtyChange} />)
    fireEvent.click(screen.getByRole('button', { name: /Add comment/ }))
    const box = screen.getByPlaceholderText('Add a comment on the whole artifact…')
    fireEvent.change(box, { target: { value: 'half a thought' } })
    expect(onDraftDirtyChange).toHaveBeenLastCalledWith(true)
    fireEvent.change(box, { target: { value: '' } })
    expect(onDraftDirtyChange).toHaveBeenLastCalledWith(false)
  })

  it('reports a reply typed inside a comment row', () => {
    const onDraftDirtyChange = vi.fn()
    render(<CommentsSidebar comments={[mk()]} {...base()} onDraftDirtyChange={onDraftDirtyChange} />)
    fireEvent.click(screen.getAllByRole('button', { name: 'Reply' })[0])
    fireEvent.change(screen.getByPlaceholderText('Reply…'), { target: { value: 'agreed' } })
    expect(onDraftDirtyChange).toHaveBeenLastCalledWith(true)
  })
})

describe('CommentsSidebar discardSignal', () => {
  it('a bumped signal closes the reply box and reports the panel clean', () => {
    const onDraftDirtyChange = vi.fn()
    const view = render(<CommentsSidebar comments={[mk()]} {...base()} onDraftDirtyChange={onDraftDirtyChange} discardSignal={0} />)
    fireEvent.click(screen.getAllByRole('button', { name: 'Reply' })[0])
    fireEvent.change(screen.getByPlaceholderText('Reply…'), { target: { value: 'agreed' } })
    expect(onDraftDirtyChange).toHaveBeenLastCalledWith(true)
    view.rerender(<CommentsSidebar comments={[mk()]} {...base()} onDraftDirtyChange={onDraftDirtyChange} discardSignal={1} />)
    expect(screen.queryByPlaceholderText('Reply…')).toBeNull()
    expect(onDraftDirtyChange).toHaveBeenLastCalledWith(false)
  })
})

describe('CommentsSidebar batch-submit markers', () => {
  const thread = () => [
    mk({ id: 'r', thread_id: 'r', body: 'pending root' }),
    mk({ id: 'rep', thread_id: 'r', parent_id: 'r', body: 'pending reply' }),
    mk({ id: 'a', thread_id: 'r', parent_id: 'r', body: 'agent reply', is_agent: true, author: 'kiro' }),
    mk({ id: 's', thread_id: 's', body: 'already sent' }),
  ]
  const markerIn = (body: string) =>
    screen.getByText(body).closest('.group')!.querySelector('[data-testid="comment-pending-submit"]')

  it('marks exactly the rows the batch counts, replies included', () => {
    render(<CommentsSidebar comments={thread()} pendingSubmitIds={new Set(['r', 'rep'])} {...base()} />)
    expect(screen.getAllByTestId('comment-pending-submit')).toHaveLength(2)
    expect(markerIn('pending root')).toHaveTextContent('To send')
    expect(markerIn('pending root')).toHaveAttribute('title', 'Not sent yet. Submit sends this comment to the chat.')
    expect(markerIn('pending reply')).not.toBeNull()
    expect(markerIn('agent reply')).toBeNull()
    expect(markerIn('already sent')).toBeNull()
  })

  it('marks no row when the host hands over no pending set', () => {
    render(<CommentsSidebar comments={thread()} {...base()} />)
    expect(screen.queryByTestId('comment-pending-submit')).toBeNull()
  })
})
