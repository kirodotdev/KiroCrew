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
    addReviewComment(SLOT, { file: 'a.kt', side: 'new', line: 1, lineText: 'x', text: 'one' })
    const { rerender } = render(<ReviewCommentBar slotId={SLOT} />)
    expect(screen.getByText('1 review comment will be sent with your next typed message')).toBeInTheDocument()
    addReviewComment(SLOT, { file: 'a.kt', side: 'old', line: 2, lineText: 'y', text: 'two' })
    rerender(<ReviewCommentBar slotId={SLOT} />)
    expect(screen.getByText('2 review comments will be sent with your next typed message')).toBeInTheDocument()
  })
})
