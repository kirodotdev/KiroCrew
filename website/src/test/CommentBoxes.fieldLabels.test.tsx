import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
import { CommentList, type InlineComment } from '../components/CommentOverlay'
import type { ArtifactComment } from '../types'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

// The thread popover's reply box and a pending comment's edit box each have an
// accessible name. A placeholder (or nothing) is not one.

const root: ArtifactComment = {
  id: 'r', origin: 'local', scope: 'private', author: 'alex', is_agent: false,
  body: 'a thread', thread_id: 'r', status: 'open', sync_state: 'local_only',
  created_at: '2026-06-10T00:00:00Z', updated_at: '2026-06-10T00:00:00Z',
}

const PENDING: InlineComment[] = [{ id: 'c1', anchor: 'first paragraph', text: 'tighten this' }]

function renderPopover() {
  render(
    <CommentThreadPopover
      comments={[root]} rootId="r" rect={{ x: 10, y: 10, w: 10, h: 10 }}
      onClose={vi.fn()} onReply={vi.fn()} onResolve={vi.fn()} onMarkReview={vi.fn()}
      onReopen={vi.fn()} onDelete={vi.fn()}
    />,
  )
}

describe('comment box names', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('names the thread popover reply box', () => {
    renderPopover()
    expect(screen.getByRole('textbox', { name: 'Reply' })).toBeInTheDocument()
  })

  it('names a pending comment edit box once editing starts', () => {
    render(<CommentList comments={PENDING} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByRole('textbox', { name: 'Edit comment' })).toHaveValue('tighten this')
  })

  it('takes the names from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    renderPopover()
    expect(screen.getByRole('textbox', { name: zh.components.commentThreadPopover.reply_2 })).toBeInTheDocument()
  })
})
