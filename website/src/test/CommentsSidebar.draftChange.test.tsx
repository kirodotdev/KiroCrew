/**
 * CommentsSidebar `onDraftChange` (#13904): the sidebar reports whether ANY of
 * its composers holds unsaved text, so a host about to navigate away can ask
 * before discarding it. One case per composer it owns -- the doc-level add box,
 * an open reply, an in-place edit -- plus the clears (cancel, unchanged edit,
 * unmount).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CommentsSidebar } from '../components/CommentsSidebar'
import type { ArtifactComment } from '../types'

const comment: ArtifactComment = {
  id: 'c1', origin: 'local', scope: 'private', author: 'alex', is_agent: false,
  body: 'a comment', thread_id: 'c1', status: 'open', sync_state: 'local_only',
  created_at: '2026-06-10T00:00:00Z', updated_at: '2026-06-10T00:00:00Z',
}

function setup() {
  const onDraftChange = vi.fn()
  const utils = render(
    <CommentsSidebar
      comments={[comment]}
      onAdd={vi.fn()} onReply={vi.fn()} onResolve={vi.fn()} onMarkReview={vi.fn()}
      onDelete={vi.fn()} onRefresh={vi.fn()} onClose={vi.fn()} onEditComment={vi.fn()}
      onDraftChange={onDraftChange}
    />,
  )
  const last = () => onDraftChange.mock.calls.at(-1)?.[0]
  return { ...utils, onDraftChange, last }
}

describe('CommentsSidebar onDraftChange', () => {
  it('reports the doc-level add box, and clears on Cancel', () => {
    const { last } = setup()
    expect(last()).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: /add comment/i }))
    expect(last()).toBe(false) // an open, empty box holds nothing
    fireEvent.change(screen.getByPlaceholderText('Add a comment on the whole artifact…'), { target: { value: 'x' } })
    expect(last()).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(last()).toBe(false)
  })

  it('reports an open reply with text', () => {
    const { last } = setup()
    fireEvent.click(screen.getByTitle('Reply'))
    fireEvent.change(screen.getByPlaceholderText('Reply…'), { target: { value: 'my reply' } })
    expect(last()).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(last()).toBe(false)
  })

  it('reports an in-place edit only once it differs from the saved body', () => {
    const { last } = setup()
    fireEvent.click(screen.getByTitle('Edit comment'))
    expect(last()).toBe(false)
    fireEvent.change(screen.getByDisplayValue('a comment'), { target: { value: 'a comment, edited' } })
    expect(last()).toBe(true)
  })

  it('locks every composer when composersDisabled is set', () => {
    render(
      <CommentsSidebar
        comments={[comment]}
        onAdd={vi.fn()} onReply={vi.fn()} onResolve={vi.fn()} onMarkReview={vi.fn()}
        onDelete={vi.fn()} onRefresh={vi.fn()} onClose={vi.fn()} onEditComment={vi.fn()}
        composersDisabled
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /add comment/i }))
    expect(screen.getByPlaceholderText('Add a comment on the whole artifact…')).toBeDisabled()
    fireEvent.click(screen.getByTitle('Reply'))
    expect(screen.getByPlaceholderText('Reply…')).toBeDisabled()
    fireEvent.click(screen.getByTitle('Edit comment'))
    expect(screen.getByDisplayValue('a comment')).toBeDisabled()
  })

  it('reports false when the sidebar unmounts', () => {
    const { last, unmount } = setup()
    fireEvent.click(screen.getByRole('button', { name: /add comment/i }))
    fireEvent.change(screen.getByPlaceholderText('Add a comment on the whole artifact…'), { target: { value: 'x' } })
    expect(last()).toBe(true)
    unmount()
    expect(last()).toBe(false)
  })
})
