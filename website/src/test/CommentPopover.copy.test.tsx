import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { CommentPopover } from '../components/CommentOverlay'
import { copyToClipboard } from '../utils/clipboard'

// The artifact pages open this popover straight off mouseup, and focusing its
// textarea collapses the document selection — so ⌘/Ctrl+C after selecting
// copied nothing, and the box offered only "Add comment". The host now hands
// over the selected text and the popover exposes it via a Copy button and the
// copy shortcut (while the input is still empty).

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

function renderPopover(props: Partial<React.ComponentProps<typeof CommentPopover>> = {}) {
  const onSubmit = vi.fn()
  const onCancel = vi.fn()
  render(<CommentPopover x={10} y={20} onSubmit={onSubmit} onCancel={onCancel} copyText="the selected passage" {...props} />)
  return { onSubmit, onCancel }
}

describe('CommentPopover copy affordance', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.mocked(copyToClipboard).mockClear()
    vi.mocked(copyToClipboard).mockResolvedValue(true)
  })
  afterEach(() => { vi.useRealTimers() })

  it('offers a Copy button that copies the selection, not the draft', async () => {
    renderPopover()
    const btn = screen.getByLabelText('Copy')
    expect(btn).toHaveAttribute('title', 'Copies the selected text — not your comment — to the clipboard')
    fireEvent.change(screen.getByLabelText('Add a comment'), { target: { value: 'my draft' } })
    fireEvent.click(btn)
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('the selected passage'))
    // The checkmark is truthful: it appears only once the clipboard took the text.
    await waitFor(() => expect(btn.querySelector('.text-ok')).not.toBeNull())
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(btn.querySelector('.text-ok')).toBeNull()
  })

  it('hides the Copy button when the host has no selection to offer', () => {
    renderPopover({ copyText: undefined })
    expect(screen.queryByLabelText('Copy')).toBeNull()
  })

  it('copies the selection on ⌘/Ctrl+C while the input is empty', async () => {
    renderPopover()
    const input = screen.getByLabelText('Add a comment')
    fireEvent.keyDown(input, { key: 'c', ctrlKey: true })
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('the selected passage'))
    fireEvent.keyDown(input, { key: 'c', metaKey: true })
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledTimes(2))
  })

  it('leaves ⌘/Ctrl+C to the textarea once a draft exists', () => {
    renderPopover()
    const input = screen.getByLabelText('Add a comment')
    fireEvent.change(input, { target: { value: 'typed' } })
    fireEvent.keyDown(input, { key: 'c', ctrlKey: true })
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('reports a refused clipboard write instead of flashing success', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    renderPopover()
    const btn = screen.getByLabelText('Copy')
    fireEvent.click(btn)
    await waitFor(() => expect(screen.getByText(/Copy failed/)).toBeInTheDocument())
    expect(btn.querySelector('.text-ok')).toBeNull()
  })

  it('keeps submit and close working beside the Copy button', () => {
    const { onSubmit, onCancel } = renderPopover()
    fireEvent.change(screen.getByLabelText('Add a comment'), { target: { value: 'note' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    expect(onSubmit).toHaveBeenCalledWith('note')
    fireEvent.click(screen.getByLabelText('Close'))
    expect(onCancel).toHaveBeenCalled()
  })
})
