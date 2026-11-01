import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CommentList, type InlineComment } from '../components/CommentOverlay'

// A box comment's row carries the number drawn on its box over the image, so a
// reader with several boxes can match a box to its row by looking. The number
// is the comment's place in the pending list, the same one the image layer
// draws, so a text comment above it still counts.

const COMMENTS: InlineComment[] = [
  { id: 'c1', anchor: 'first paragraph', text: 'tighten this' },
  { id: 'c2', anchor: 'Schedule page', text: 'too tight', image: { src: 'a.png', x: 0.34, y: 0.33, w: 0.2, h: 0.2 } },
]

describe('CommentList image rows', () => {
  it('names the box by its number and the image, with no coordinates', () => {
    render(<CommentList comments={COMMENTS} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
    expect(screen.getByText('Box 2 on "Schedule page"')).toBeInTheDocument()
    expect(screen.queryByText(/34%/)).toBeNull()
  })

  it('cuts a long alt text short at a word, so the box number stays close to its comment', () => {
    const long: InlineComment[] = [{
      id: 'c1', anchor: 'Schedule page listing recurring jobs with status, next run, and per-job actions', text: 'make this column wider',
      image: { src: 'a.png', x: 0.34, y: 0.33, w: 0.2, h: 0.2 },
    }]
    render(<CommentList comments={long} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
    expect(screen.getByText('Box 1 on "Schedule page listing recurring…"')).toBeInTheDocument()
  })

  // A box comment's row is the same row as a text comment's, so an unfinished
  // edit of it survives the panel withholding the tab for a moment, and the
  // reopened row still names its box.
  it('keeps an unfinished edit of a box comment across an unmount, under its box label', () => {
    const draftKey = 'local:chat-r1:file:1:/workspace/project/README.md'
    const box: InlineComment[] = [COMMENTS[1]]
    const view = render(<CommentList comments={box} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} draftKey={draftKey} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByDisplayValue('too tight'), { target: { value: 'too tight on mobile' } })
    view.unmount()
    const onEdit = vi.fn()
    render(<CommentList comments={box} onEdit={onEdit} onRemove={vi.fn()} onSubmitAll={vi.fn()} draftKey={draftKey} />)
    expect(screen.getByText('Box 1 on "Schedule page"')).toBeInTheDocument()
    const input = screen.getByDisplayValue('too tight on mobile')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('c2', 'too tight on mobile')
  })
})
