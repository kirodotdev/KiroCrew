import { describe, it, expect, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import ReviewCommentBar from '../components/ReviewCommentBar'
import { addReviewComment, clearReviewComments } from '../store/reviewComments'

const SLOT = 'bar-test-slot'
afterEach(() => clearReviewComments(SLOT))

describe('ReviewCommentBar', () => {
  it('renders nothing with no drafts', () => {
    const { container } = render(<ReviewCommentBar slotId={SLOT} />)
    expect(container.firstChild).toBeNull()
  })

  it('pluralizes the pending label with the draft count', () => {
    addReviewComment(SLOT, { blockId: 'db-1', fileIndex: 0, file: 'a.kt', side: 'new', line: 1, lineText: 'x', text: 'one' })
    const { rerender } = render(<ReviewCommentBar slotId={SLOT} />)
    expect(screen.getByText('1 review comment will be sent with your next message in this chat')).toBeInTheDocument()
    addReviewComment(SLOT, { blockId: 'db-1', fileIndex: 0, file: 'a.kt', side: 'old', line: 2, lineText: 'y', text: 'two' })
    rerender(<ReviewCommentBar slotId={SLOT} />)
    expect(screen.getByText('2 review comments will be sent with your next message in this chat')).toBeInTheDocument()
  })

  it('labels a deleted-line draft as old and a dragged range by its first and last line', () => {
    addReviewComment(SLOT, { blockId: 'db-1', fileIndex: 0, file: 'src/a.kt', side: 'old', line: 2, lineText: 'y', text: 'gone' })
    addReviewComment(SLOT, { blockId: 'db-1', fileIndex: 0, file: 'src/retry.ts', side: 'new', line: 13, endLine: 15, lineText: 'z', text: 'cap it' })
    render(<ReviewCommentBar slotId={SLOT} />)
    expect(screen.getByText('a.kt:2 (old)')).toBeInTheDocument()
    expect(screen.getByText('retry.ts:13-15')).toBeInTheDocument()
  })
})
