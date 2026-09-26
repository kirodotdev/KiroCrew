import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CommentList, formatCommentsMessage, type InlineComment } from '../src/components/CommentOverlay'

describe('CommentOverlay', () => {
  describe('CommentList', () => {
    const comments: InlineComment[] = [
      { id: '1', anchor: 'Option A: Sweep query', text: 'This should mention the P0 dashboard' },
      { id: '2', anchor: 'EBT: Cannot use AGC', text: 'Add LoonieToonie as alternative' },
    ]

    it('renders pending comment count', () => {
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      expect(screen.getByText('2 comments pending')).toBeInTheDocument()
    })

    it('calls onSubmitAll when Submit All is clicked', async () => {
      const user = userEvent.setup()
      const onSubmitAll = vi.fn()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={onSubmitAll} />)
      await user.click(screen.getByRole('button', { name: /submit all/i }))
      expect(onSubmitAll).toHaveBeenCalled()
    })

    it('calls onRemove when ✕ is clicked', async () => {
      const user = userEvent.setup()
      const onRemove = vi.fn()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={onRemove} onSubmitAll={vi.fn()} />)
      const removeButtons = screen.getAllByRole('button', { name: /remove/i })
      await user.click(removeButtons[0])
      expect(onRemove).toHaveBeenCalledWith('1')
    })

    it('enters edit mode when Edit button is clicked', async () => {
      const user = userEvent.setup()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      const editButtons = screen.getAllByRole('button', { name: /edit/i })
      await user.click(editButtons[0])
      expect(screen.getByDisplayValue('This should mention the P0 dashboard')).toBeInTheDocument()
    })

    it('calls onEdit with new text on Enter', async () => {
      const user = userEvent.setup()
      const onEdit = vi.fn()
      render(<CommentList comments={comments} onEdit={onEdit} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      await user.click(screen.getAllByRole('button', { name: /edit/i })[0])
      const input = screen.getByDisplayValue('This should mention the P0 dashboard')
      await user.clear(input)
      await user.type(input, 'Updated comment{enter}')
      expect(onEdit).toHaveBeenCalledWith('1', 'Updated comment')
    })

    it('cancels edit on Escape without calling onEdit', async () => {
      const user = userEvent.setup()
      const onEdit = vi.fn()
      render(<CommentList comments={comments} onEdit={onEdit} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      await user.click(screen.getAllByRole('button', { name: /edit/i })[0])
      await user.type(screen.getByDisplayValue('This should mention the P0 dashboard'), '{escape}')
      expect(onEdit).not.toHaveBeenCalled()
      expect(screen.getByText('This should mention the P0 dashboard')).toBeInTheDocument()
    })

    it('does not commit edit on Enter while IME is composing', async () => {
      const user = userEvent.setup()
      const onEdit = vi.fn()
      render(<CommentList comments={comments} onEdit={onEdit} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      await user.click(screen.getAllByRole('button', { name: /edit/i })[0])
      const input = screen.getByDisplayValue('This should mention the P0 dashboard') as HTMLInputElement
      fireEvent.compositionStart(input)
      fireEvent.change(input, { target: { value: '测试' } })
      // Enter mid-composition should NOT commit the edit
      fireEvent.keyDown(input, { key: 'Enter', keyCode: 13, isComposing: true })
      expect(onEdit).not.toHaveBeenCalled()
      // Input still editable so user can keep composing
      expect(screen.getByDisplayValue('测试')).toBeInTheDocument()
    })

    it('enters edit mode when comment text is clicked', async () => {
      const user = userEvent.setup()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      await user.click(screen.getByText('This should mention the P0 dashboard'))
      expect(screen.getByDisplayValue('This should mention the P0 dashboard')).toBeInTheDocument()
    })

    it('does not call onEdit twice on Enter (committedRef guard)', async () => {
      const user = userEvent.setup()
      const onEdit = vi.fn()
      render(<CommentList comments={comments} onEdit={onEdit} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      await user.click(screen.getAllByRole('button', { name: /edit/i })[0])
      const input = screen.getByDisplayValue('This should mention the P0 dashboard')
      await user.clear(input)
      await user.type(input, 'New text{enter}')
      expect(onEdit).toHaveBeenCalledTimes(1)
    })

    it('does not call onEdit when Remove is clicked during edit', async () => {
      const user = userEvent.setup()
      const onEdit = vi.fn()
      const onRemove = vi.fn()
      render(<CommentList comments={comments} onEdit={onEdit} onRemove={onRemove} onSubmitAll={vi.fn()} />)
      await user.click(screen.getAllByRole('button', { name: /edit/i })[0])
      const input = screen.getByDisplayValue('This should mention the P0 dashboard')
      await user.clear(input)
      await user.type(input, 'Unsaved edit')
      await user.click(screen.getAllByRole('button', { name: /remove/i })[0])
      expect(onRemove).toHaveBeenCalledWith('1')
      expect(onEdit).not.toHaveBeenCalled()
    })

    it('saves edit on blur (click away)', async () => {
      const user = userEvent.setup()
      const onEdit = vi.fn()
      render(<div><CommentList comments={comments} onEdit={onEdit} onRemove={vi.fn()} onSubmitAll={vi.fn()} /><button>Outside</button></div>)
      await user.click(screen.getAllByRole('button', { name: /edit/i })[0])
      const input = screen.getByDisplayValue('This should mention the P0 dashboard')
      await user.clear(input)
      await user.type(input, 'Blur saved text')
      await user.click(screen.getByText('Outside'))
      expect(onEdit).toHaveBeenCalledWith('1', 'Blur saved text')
      expect(onEdit).toHaveBeenCalledTimes(1)
    })

    it('renders nothing when comments array is empty', () => {
      const { container } = render(<CommentList comments={[]} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      expect(container.innerHTML).toBe('')
    })

    it('does not render the Additional prompt textarea by default', () => {
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      expect(screen.queryByLabelText('Additional prompt')).not.toBeInTheDocument()
    })

    it('does not render the "Add instruction" toggle when enableExtraPrompt is not set', () => {
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
      expect(screen.queryByRole('button', { name: /instruction|prompt/i })).not.toBeInTheDocument()
    })

    it('keeps the Additional prompt textarea hidden until the toggle is clicked when enableExtraPrompt is set', async () => {
      const user = userEvent.setup()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} enableExtraPrompt />)
      // Toggle present, textarea still hidden before the click.
      expect(screen.queryByLabelText('Additional prompt')).not.toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: /instruction|prompt/i }))
      expect(screen.getByLabelText('Additional prompt')).toBeInTheDocument()
    })

    it('calls onSubmitAll with the typed extra-prompt text after opening the toggle when enableExtraPrompt is set', async () => {
      const user = userEvent.setup()
      const onSubmitAll = vi.fn()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={onSubmitAll} enableExtraPrompt />)
      await user.click(screen.getByRole('button', { name: /instruction|prompt/i }))
      await user.type(screen.getByLabelText('Additional prompt'), 'Route to docs owner')
      await user.click(screen.getByRole('button', { name: /submit all/i }))
      expect(onSubmitAll).toHaveBeenCalledWith('Route to docs owner')
    })

    it('calls onSubmitAll with undefined when enableExtraPrompt is set but the toggle is never opened', async () => {
      const user = userEvent.setup()
      const onSubmitAll = vi.fn()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={onSubmitAll} enableExtraPrompt />)
      await user.click(screen.getByRole('button', { name: /submit all/i }))
      expect(onSubmitAll).toHaveBeenCalledWith(undefined)
    })

    it('calls onSubmitAll with undefined when enableExtraPrompt is not set', async () => {
      const user = userEvent.setup()
      const onSubmitAll = vi.fn()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={onSubmitAll} />)
      await user.click(screen.getByRole('button', { name: /submit all/i }))
      expect(onSubmitAll).toHaveBeenCalledWith(undefined)
    })

    it('collapses the extra-prompt textarea after Submit All so stale text is not re-sent', async () => {
      // CommentList returns null when comments empty but stays mounted, so its
      // textarea state must reset on submit — otherwise a later batch silently
      // reuses the previous instruction. After submit the box collapses (the
      // toggle resets), so the textarea is no longer in the document.
      const user = userEvent.setup()
      render(<CommentList comments={comments} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} enableExtraPrompt />)
      await user.click(screen.getByRole('button', { name: /instruction|prompt/i }))
      await user.type(screen.getByLabelText('Additional prompt'), 'one-time instruction')
      await user.click(screen.getByRole('button', { name: /submit all/i }))
      expect(screen.queryByLabelText('Additional prompt')).not.toBeInTheDocument()
    })
  })

  describe('formatCommentsMessage', () => {
    it('formats comments into structured message', () => {
      const comments: InlineComment[] = [
        { id: '1', anchor: 'Option A', text: 'Expand this' },
        { id: '2', anchor: 'Option B', text: 'Remove this' },
      ]
      const msg = formatCommentsMessage('/path/to/doc.md', comments)
      expect(msg).toContain('[Document feedback on /path/to/doc.md — 2 comments]')
      expect(msg).toContain('1. ("Option A"): "Expand this"')
      expect(msg).toContain('2. ("Option B"): "Remove this"')
    })

    it('handles single comment', () => {
      const comments: InlineComment[] = [
        { id: '1', anchor: 'text', text: 'note' },
      ]
      const msg = formatCommentsMessage('/doc.md', comments)
      expect(msg).toContain('1 comment]')
    })
  })
})
