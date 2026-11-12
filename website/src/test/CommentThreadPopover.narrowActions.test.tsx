/**
 * The thread popover's action row on a phone (#30348).
 *
 * The popover is 360px wide, capped at the viewport minus 16px, and clips its
 * content (overflow-hidden). At 390px a root comment shows Reply, Edit,
 * Review, Resolve and Delete on one row, which is wider than the card, so the
 * last action was clipped ("Delet"). The invariant: every action stays inside
 * the card, so the row wraps onto a new line instead of running past the edge.
 * jsdom does no layout, so the wrap is asserted on the row's class.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
import type { ArtifactComment } from '../types'
import { i18nT } from '../i18n/t'

function comment(id: string, parentId: string | null): ArtifactComment {
  return {
    id, origin: 'local', scope: 'private', author: 'alex', is_agent: false,
    body: id, thread_id: 'r', parent_id: parentId, status: 'open', sync_state: 'local_only',
    created_at: '2026-06-10T00:00:00Z', updated_at: '2026-06-10T00:00:00Z',
  } as ArtifactComment
}

describe('CommentThreadPopover action row at phone width', () => {
  it('wraps the root and reply action rows instead of overflowing the card', () => {
    render(
      <CommentThreadPopover
        comments={[comment('r', null), comment('r1', 'r')]}
        rootId="r"
        rect={{ x: 10, y: 10, w: 10, h: 10 }}
        onClose={vi.fn()}
        onReply={vi.fn()}
        onResolve={vi.fn()}
        onMarkReview={vi.fn()}
        onReopen={vi.fn()}
        onDelete={vi.fn()}
        onEditComment={vi.fn()}
      />,
    )

    const deletes = screen.getAllByRole('button', { name: i18nT('components.commentsSidebar.delete') })
    expect(deletes).toHaveLength(2)
    for (const del of deletes) {
      const row = del.parentElement as HTMLElement
      // The row is the one that holds this comment's other actions too.
      expect(within(row).getByRole('button', { name: i18nT('components.commentsSidebar.reply_2') })).toBeTruthy()
      expect(row.className).toContain('flex-wrap')
      // Delete continues its own row rather than being pushed to the right
      // edge, where a wrapped root Delete sat above the reply and read as its.
      expect(del.className).not.toContain('ml-auto')
    }
  })
})
