import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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

  it('names a box on an image with no alt text and no file name by its spot, never by an empty quote', () => {
    // An inline data: image with no alt text leaves the anchor empty.
    const unnamed: InlineComment[] = [{ id: 'c1', anchor: '', text: 'blurry', image: { src: 'data:image/png;base64,AAAA', x: 0.34, y: 0.33, w: 0.2, h: 0.2 } }]
    render(<CommentList comments={unnamed} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
    expect(screen.getByText('Box 1 at 34%, 33%')).toBeInTheDocument()
    expect(screen.queryByText(/""/)).toBeNull()
  })
})
