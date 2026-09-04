import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import QueueStack, { SubagentDeliveryProgress, isSystemDelivery, isNonInteractiveQueued } from '../components/QueueStack'
import type { ChatMessage } from '../types'

// QueueStack renders framer-motion cards; we only exercise the inline
// EditInput, which is plain DOM and needs no special test polyfill.

function queued(content: string, queueId: string): ChatMessage {
  return { role: 'queued', content, cls: 'msg msg-queued', ts: '', meta: { queueId } } as ChatMessage
}

/** Open the inline editor on the single queued card and return its input. */
function openEditor() {
  const pencil = screen.getByLabelText('Edit queued message')
  fireEvent.click(pencil)
  return screen.getByLabelText('Edit queued message') as HTMLTextAreaElement
}

describe('QueueStack inline edit', () => {
  it('commits a real change on Enter', () => {
    const onEdit = vi.fn()
    render(<QueueStack messages={[queued('old text', 'q1')]} onEdit={onEdit} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: 'new text' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledTimes(1)
    expect(onEdit).toHaveBeenCalledWith('q1', 'new text')
  })

  it('does NOT fire onEdit when the text is unchanged and the input blurs (no-op edit)', () => {
    const onEdit = vi.fn()
    render(<QueueStack messages={[queued('same text', 'q1')]} onEdit={onEdit} />)
    const input = openEditor()
    // User clicks in, clicks away without changing anything.
    fireEvent.blur(input)
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('does NOT fire onEdit when the input is cleared and blurred (empty no-op)', () => {
    const onEdit = vi.fn()
    render(<QueueStack messages={[queued('something', 'q1')]} onEdit={onEdit} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.blur(input)
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('does NOT fire onEdit on Escape (cancel), even after editing', () => {
    const onEdit = vi.fn()
    render(<QueueStack messages={[queued('old', 'q1')]} onEdit={onEdit} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: 'changed but cancelled' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('is a textarea that keeps the newlines between attachment markers through an edit', () => {
    // The serializer writes one `[attached_file N] path` marker per line. A
    // single-line <input> drops every newline from its value, which would
    // glue the markers together and make the queue edit prune all but the
    // last attachment (a card resolving to a path the agent never received).
    const onEdit = vi.fn()
    const twoFiles = 'caption\n[attached_file 1] /tmp/a.pdf\n[attached_file 2] /tmp/My Report.pdf'
    render(<QueueStack messages={[queued(twoFiles, 'q1')]} onEdit={onEdit} />)
    const editor = openEditor()
    expect(editor.tagName).toBe('TEXTAREA')
    expect(editor.value).toBe(twoFiles)
    // The card is a fixed-height stack slot, so the editor stays one visible row.
    expect(editor.getAttribute('rows')).toBe('1')
    // Only the caption line is selected: a retype replaces the caption, never
    // the marker lines hidden below the visible row.
    expect(editor.selectionStart).toBe(0)
    expect(editor.selectionEnd).toBe('caption'.length)
    // The hidden lines are named next to the editor so the value below the
    // fold is never a surprise -- and named as what they are: every hidden
    // line here is an attachment marker, so the cue says attachments.
    expect(screen.getByTestId('queue-edit-hidden-lines').textContent).toBe('+2 attachments')
    const edited = 'new caption\n[attached_file 1] /tmp/a.pdf\n[attached_file 2] /tmp/My Report.pdf'
    fireEvent.change(editor, { target: { value: edited } })
    fireEvent.keyDown(editor, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', edited)
  })

  it('counts hidden lines generically when they are not all attachment markers', () => {
    const mixed = 'first line\nsecond line of prose\n[attached_file 1] /tmp/a.pdf'
    render(<QueueStack messages={[queued(mixed, 'q1')]} onEdit={vi.fn()} />)
    openEditor()
    expect(screen.getByTestId('queue-edit-hidden-lines').textContent).toBe('+2 lines')
  })

  it('shows no hidden-line cue on a single-line entry and selects the whole value', () => {
    render(<QueueStack messages={[queued('one line', 'q1')]} onEdit={vi.fn()} />)
    const editor = openEditor()
    expect(screen.queryByTestId('queue-edit-hidden-lines')).toBeNull()
    expect(editor.selectionStart).toBe(0)
    expect(editor.selectionEnd).toBe('one line'.length)
  })

  it('commits only once when Enter is followed by the trailing blur', () => {
    const onEdit = vi.fn()
    render(<QueueStack messages={[queued('old', 'q1')]} onEdit={onEdit} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: 'edited' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.blur(input)  // committedRef guard must swallow this
    expect(onEdit).toHaveBeenCalledTimes(1)
  })
})

describe('system sub-agent delivery handling', () => {
  it('isSystemDelivery matches per-agent and batch completion announces only', () => {
    expect(isSystemDelivery(queued('[Subagent completion event]\nAgent `x` completed', 'q1'))).toBe(true)
    expect(isSystemDelivery(queued('[Subagent batch completion event]\nWave finished', 'q2'))).toBe(true)
    expect(isSystemDelivery(queued('please also check the docs', 'q3'))).toBe(false)
    expect(isSystemDelivery(queued('tell me about [Subagent completion event]', 'q4'))).toBe(false)
  })

  it('SubagentDeliveryProgress renders a non-interactive count line', () => {
    render(<SubagentDeliveryProgress count={42} />)
    const el = screen.getByTestId('subagent-delivery-progress')
    expect(el.textContent).toContain('42 sub-agent results ready')
    // Non-interactive: no buttons, no inputs — nothing to cancel or edit.
    expect(el.querySelector('button')).toBeNull()
    expect(el.querySelector('input')).toBeNull()
  })

  it('SubagentDeliveryProgress renders nothing at zero', () => {
    render(<SubagentDeliveryProgress count={0} />)
    expect(screen.queryByTestId('subagent-delivery-progress')).toBeNull()
  })
})

describe('isNonInteractiveQueued (composer QueueStack exclusion)', () => {
  it('excludes sub-agent completion deliveries', () => {
    expect(isNonInteractiveQueued(queued('[Subagent completion event]\nAgent `x` completed', 'q1'))).toBe(true)
    expect(isNonInteractiveQueued(queued('[Subagent batch completion event]\nWave finished', 'q2'))).toBe(true)
  })

  it('excludes synthetic turn-recovery injections (the tool-refusal composer leak)', () => {
    // Regression: a [Tool refusal — automatic recovery] injection was rendering
    // as an editable/cancellable user card in the composer QueueStack.
    expect(isNonInteractiveQueued(queued(
      '[Tool refusal — automatic recovery]\nOne or more tool calls in your previous turn were blocked.', 'q1',
    ))).toBe(true)
    expect(isNonInteractiveQueued(queued('[Stalled turn — automatic recovery]\n…', 'q2'))).toBe(true)
    expect(isNonInteractiveQueued(queued('[Tool stall — automatic recovery]\n…', 'q3'))).toBe(true)
    expect(isNonInteractiveQueued(queued('[Interrupted turn — automatic recovery]\n…', 'q4'))).toBe(true)
    expect(isNonInteractiveQueued(queued('[Empty response — automatic recovery]\n…', 'q5'))).toBe(true)
  })

  it('keeps real user-typed messages interactive', () => {
    expect(isNonInteractiveQueued(queued('please also check the docs', 'q1'))).toBe(false)
    // A user quoting the prefix mid-sentence is still a user message.
    expect(isNonInteractiveQueued(queued('why did I see [Tool refusal — automatic recovery]?', 'q2'))).toBe(false)
  })
})
