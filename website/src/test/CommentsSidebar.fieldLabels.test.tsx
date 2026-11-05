import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { CommentsSidebar, EditBox, ReplyBox } from '../components/CommentsSidebar'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

// Each comment box has an accessible name. A placeholder is not one: it
// vanishes once text is typed, and assistive tech may skip it.

const zhSidebar = zh.components.commentsSidebar

function base() {
  return {
    onAdd: vi.fn(), onReply: vi.fn(), onResolve: vi.fn(), onMarkReview: vi.fn(),
    onDelete: vi.fn(), onRefresh: vi.fn(), onClose: vi.fn(),
  }
}

describe('comment box names', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('names the reply box', () => {
    render(<ReplyBox onSubmit={vi.fn()} onCancel={vi.fn()} />)
    expect(screen.getByRole('textbox', { name: 'Reply' })).toBeInTheDocument()
  })

  it('names the edit box', () => {
    render(<EditBox initial="a comment" onSubmit={vi.fn()} onCancel={vi.fn()} />)
    expect(screen.getByRole('textbox', { name: 'Edit comment' })).toHaveValue('a comment')
  })

  it('names the whole-artifact comment box once it opens', () => {
    render(<CommentsSidebar comments={[]} {...base()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Add comment' }))
    expect(screen.getByRole('textbox', { name: 'Add comment' })).toBeInTheDocument()
  })

  it('takes the names from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    render(<ReplyBox onSubmit={vi.fn()} onCancel={vi.fn()} />)
    expect(screen.getByRole('textbox', { name: zhSidebar.reply_2 })).toBeInTheDocument()
  })
})
