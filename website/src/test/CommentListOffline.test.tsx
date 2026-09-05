import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CommentList, type InlineComment } from '../components/CommentOverlay'

// The file panel's "Submit All" batch comment submit must be gated on the
// gateway connection flag, mirroring ChatInput's Send gating. Before this
// gate, clicking while offline composed the batch, cleared the pending
// comments, and the downstream send path silently dropped the message —
// destroying the user's comments with no error.

const COMMENTS: InlineComment[] = [
  { id: 'c1', anchor: 'first paragraph', text: 'tighten this' },
  { id: 'c2', anchor: 'second paragraph', text: 'add a citation' },
]

function renderList(props: Partial<React.ComponentProps<typeof CommentList>> = {}) {
  const onSubmitAll = vi.fn()
  render(
    <CommentList comments={COMMENTS} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={onSubmitAll} {...props} />,
  )
  return onSubmitAll
}

describe('CommentList offline gating', () => {
  it('submits normally when connected (default)', () => {
    const onSubmitAll = renderList()
    fireEvent.click(screen.getByText(/Submit All/))
    expect(onSubmitAll).toHaveBeenCalledTimes(1)
  })

  it('disables Submit All with the offline affordance when disconnected', () => {
    renderList({ connected: false })
    const btn = screen.getByLabelText('Submit All disabled — gateway offline')
    expect(btn).toBeDisabled()
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('title', 'Gateway offline — reconnect to submit comments')
  })

  it('does not fire onSubmitAll while disconnected', () => {
    const onSubmitAll = renderList({ connected: false })
    fireEvent.click(screen.getByText(/Submit All/))
    expect(onSubmitAll).not.toHaveBeenCalled()
  })

  it('carries no offline affordance when connected', () => {
    renderList({ connected: true })
    const btn = screen.getByText(/Submit All/).closest('button')!
    expect(btn).not.toBeDisabled()
    expect(btn).toHaveAttribute('aria-disabled', 'false')
  })
})
