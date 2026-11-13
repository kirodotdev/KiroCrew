/**
 * Reply, resolve and delete paths of the floating thread popover that the other
 * popover suites never reach: typing into the reply box, Enter to send, resolving
 * and deleting the thread root (which also closes the card), deleting one reply,
 * and the order replies are listed in (oldest first, whatever order they arrive).
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
import type { ArtifactComment } from '../types'
import { i18nT } from '../i18n/t'

function comment(id: string, body: string, parentId: string | null, createdAt: string): ArtifactComment {
  return {
    id, origin: 'local', scope: 'private', author: 'alex', is_agent: false,
    body, thread_id: 'r', parent_id: parentId, status: 'open', sync_state: 'local_only',
    created_at: createdAt, updated_at: createdAt,
  } as ArtifactComment
}

function renderPopover(comments: ArtifactComment[]) {
  const handlers = {
    onClose: vi.fn(),
    onReply: vi.fn(),
    onResolve: vi.fn(),
    onMarkReview: vi.fn(),
    onReopen: vi.fn(),
    onDelete: vi.fn(),
  }
  render(
    <CommentThreadPopover
      comments={comments}
      rootId="r"
      rect={{ x: 10, y: 10, w: 10, h: 10 }}
      {...handlers}
    />,
  )
  return handlers
}

describe('CommentThreadPopover reply, resolve and delete', () => {
  it('sends the typed reply on Enter and empties the reply box', () => {
    const { onReply } = renderPopover([comment('r', 'the root', null, '2026-06-10T00:00:00Z')])
    const box = screen.getByPlaceholderText(i18nT('components.commentThreadPopover.reply'))

    fireEvent.change(box, { target: { value: 'thanks' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    expect(onReply).toHaveBeenCalledWith('r', 'thanks')
    expect(box).toHaveValue('')
  })

  it('resolving the root reports the resolve and closes the card', () => {
    const { onResolve, onClose } = renderPopover([comment('r', 'the root', null, '2026-06-10T00:00:00Z')])

    fireEvent.click(screen.getByRole('button', { name: i18nT('components.commentsSidebar.resolve') }))

    expect(onResolve).toHaveBeenCalledWith('r')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('deleting the root reports the delete and closes the card', () => {
    const { onDelete, onClose } = renderPopover([comment('r', 'the root', null, '2026-06-10T00:00:00Z')])

    fireEvent.click(screen.getByRole('button', { name: i18nT('components.commentsSidebar.delete') }))

    expect(onDelete).toHaveBeenCalledWith('r')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('lists replies oldest first and deletes only the chosen reply', () => {
    const { onDelete, onClose } = renderPopover([
      comment('r', 'the root', null, '2026-06-10T00:00:00Z'),
      comment('r2', 'second reply', 'r', '2026-06-10T00:02:00Z'),
      comment('r1', 'first reply', 'r', '2026-06-10T00:01:00Z'),
    ])

    const bodies = (screen.getAllByText(/^(first|second) reply$/)).map(n => n.textContent)
    expect(bodies).toEqual(['first reply', 'second reply'])

    const deletes = screen.getAllByRole('button', { name: i18nT('components.commentsSidebar.delete') })
    expect(deletes).toHaveLength(3)
    fireEvent.click(deletes[2])

    expect(onDelete).toHaveBeenCalledTimes(1)
    expect(onDelete).toHaveBeenCalledWith('r2')
    expect(onClose).not.toHaveBeenCalled()
  })
})
