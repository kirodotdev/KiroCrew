import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import QueueStack, { SubagentDeliveryProgress, isSystemDelivery, isNonInteractiveQueued } from '../components/QueueStack'
import type { ChatMessage } from '../types'
import { buildOutgoingTurn } from '../chat-core/composer/outgoingTurn'
import { quoteBlock } from '../chat-core/composer/messageQuote'
import type { KnowledgeBlock } from '../pages/chat/useKnowledgeFetch'

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

describe('QueueStack quoting entry', () => {
  it('previews and edits what the user typed; the quote block goes back on the head when the edit commits', () => {
    const quote = { role: 'assistant' as const, text: 'the quoted reply', ts: 't0' }
    const block = '> the quoted reply\n> — quoting an earlier message from the assistant'
    const onEdit = vi.fn()
    render(<QueueStack messages={[{ role: 'queued', content: `${block}\n\nwhy though?`, cls: '', ts: 't', meta: { queueId: 'q1', quote } }]} onCancel={() => {}} onEdit={onEdit} />)
    expect(screen.getByText('why though?')).toBeInTheDocument()
    expect(screen.queryByText(/quoting an earlier message/)).not.toBeInTheDocument()
    const input = openEditor()
    expect(input.value).toBe('why though?')
    fireEvent.change(input, { target: { value: 'why not?' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', `${block}\n\nwhy not?`)
  })
})

describe('QueueStack quote-only entry', () => {
  it('previews the quoted excerpt instead of a blank line, but the editor opens empty and an edit keeps the block', () => {
    const quote = { role: 'assistant' as const, text: 'the quoted reply', ts: 't0' }
    const block = '> the quoted reply\n> — quoting an earlier message from the assistant'
    const onEdit = vi.fn()
    render(<QueueStack messages={[{ role: 'queued', content: block, cls: '', ts: 't', meta: { queueId: 'q1', quote } }]} onCancel={() => {}} onEdit={onEdit} />)
    expect(screen.getByText('the quoted reply')).toBeInTheDocument()
    const input = openEditor()
    expect(input.value).toBe('')
    fireEvent.change(input, { target: { value: 'now with words' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', `${block}\n\nnow with words`)
  })
})

describe('QueueStack knowledge-context entry', () => {
  // The queued row carries the WIRE text a real send produced, so these tests
  // build it with the composer's own turn builder instead of hand-writing it.
  const KB = { totalTokens: 7, items: [{ id: 'k1', title: 'Doc', content: 'kb line one\n\nkb line two', tokens: 7 }] } as unknown as KnowledgeBlock

  it('shows only what the user typed on the card, never the envelope', () => {
    const wire = buildOutgoingTurn({ text: 'summarize the doc', knowledge: KB }, 'send').wire
    expect(wire).toContain('[KNOWLEDGE CONTEXT')
    const { container } = render(<QueueStack messages={[queued(wire, 'q1')]} onCancel={() => {}} onEdit={() => {}} />)
    expect(screen.getByText('summarize the doc')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/KNOWLEDGE CONTEXT|kb line one/)
  })

  it('opens the editor on the typed words and sends the envelope back with the edit', () => {
    const wire = buildOutgoingTurn({ text: 'summarize the doc', knowledge: KB }, 'send').wire
    const onEdit = vi.fn()
    render(<QueueStack messages={[queued(wire, 'q1')]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    expect(input.value).toBe('summarize the doc')
    fireEvent.change(input, { target: { value: 'summarize it briefly' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const expected = buildOutgoingTurn({ text: 'summarize it briefly', knowledge: KB }, 'send').wire
    expect(onEdit).toHaveBeenCalledWith('q1', expected)
  })

  it('handles a quote plus knowledge: card shows the typed words, edit restores quote then envelope', () => {
    const quote = { role: 'assistant' as const, text: 'the quoted reply', ts: 't0' }
    const wire = buildOutgoingTurn({ text: 'why though?', knowledge: KB, quote }, 'send').wire
    expect(wire.startsWith(quoteBlock(quote))).toBe(true)
    const onEdit = vi.fn()
    const { container } = render(<QueueStack messages={[{ role: 'queued', content: wire, cls: '', ts: 't', meta: { queueId: 'q1', quote } }]} onCancel={() => {}} onEdit={onEdit} />)
    expect(screen.getByText('why though?')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/KNOWLEDGE CONTEXT|kb line one|quoting an earlier message/)
    const input = openEditor()
    expect(input.value).toBe('why though?')
    fireEvent.change(input, { target: { value: 'why not?' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', buildOutgoingTurn({ text: 'why not?', knowledge: KB, quote }, 'send').wire)
  })

  it('an entry sent with a quote, knowledge and no typed words edits into a well-formed send', () => {
    const quote = { role: 'assistant' as const, text: 'the quoted reply', ts: 't0' }
    const wire = buildOutgoingTurn({ text: '', knowledge: KB, quote }, 'send').wire
    const onEdit = vi.fn()
    render(<QueueStack messages={[{ role: 'queued', content: wire, cls: '', ts: 't', meta: { queueId: 'q1', quote } }]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    expect(input.value).toBe('')
    fireEvent.change(input, { target: { value: 'why?' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', buildOutgoingTurn({ text: 'why?', knowledge: KB, quote }, 'send').wire)
  })

  it('keeps the whole text when a knowledge item holds the close marker on its own line', () => {
    // Two close-marker lines: which one ends the envelope cannot be told, so
    // the card and editor keep the text whole rather than show document text
    // as the user's words and let an edit overwrite it.
    const kb = { totalTokens: 7, items: [{ id: 'k1', title: 'Doc', content: 'Example:\n[END KNOWLEDGE CONTEXT]\nKeep this paragraph.', tokens: 7 }] } as unknown as KnowledgeBlock
    const wire = buildOutgoingTurn({ text: 'Summarize the reference.', knowledge: kb }, 'send').wire
    const onEdit = vi.fn()
    render(<QueueStack messages={[queued(wire, 'q1')]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    expect(input.value).toBe(wire)
    expect(input.value).not.toMatch(/^Keep this paragraph/)
  })

  it('marks the card when it hides a knowledge envelope, and only then', () => {
    const withKb = buildOutgoingTurn({ text: 'summarize the doc', knowledge: KB }, 'send').wire
    const { unmount } = render(<QueueStack messages={[queued(withKb, 'q1')]} onCancel={() => {}} onEdit={() => {}} />)
    const chip = screen.getByTestId('queue-card-knowledge')
    expect(chip).toHaveTextContent('knowledge attached')
    expect(chip).toHaveAttribute('title', expect.stringMatching(/attached/))
    unmount()
    render(<QueueStack messages={[queued('summarize the doc', 'q2')]} onCancel={() => {}} onEdit={() => {}} />)
    expect(screen.queryByTestId('queue-card-knowledge')).toBeNull()
  })

  it('drops the chip label to screen-reader-only on a narrow card, never removing it', () => {
    // jsdom evaluates no container query; this pins the rule that does it
    // (the capture script asserts the real narrow layout).
    const wire = buildOutgoingTurn({ text: 'summarize the doc', knowledge: KB }, 'send').wire
    render(<QueueStack messages={[queued(wire, 'q1')]} onCancel={() => {}} onEdit={() => {}} />)
    const label = screen.getByTestId('queue-card-knowledge-label')
    expect(label).toHaveTextContent('knowledge attached')
    expect(label.className).toContain('/queuecard:sr-only')
    expect(label.className).toContain('truncate')
    expect(screen.getAllByTestId('queue-card')[0].className).toContain('@container/queuecard')
    // The preview keeps a floor beside the chip, so the chip gives way first.
    expect(screen.getByText('summarize the doc').className).toContain('min-w-16')
  })

  it('keeps the knowledge mark beside the edit box while editing', () => {
    const wire = buildOutgoingTurn({ text: 'summarize the doc', knowledge: KB }, 'send').wire
    render(<QueueStack messages={[queued(wire, 'q1')]} onCancel={() => {}} onEdit={() => {}} />)
    openEditor()
    expect(screen.getByTestId('queue-card-knowledge')).toBeInTheDocument()
  })

  it('shows no knowledge mark when the split refuses and the card already shows the envelope', () => {
    const kb = { totalTokens: 7, items: [{ id: 'k1', title: 'Doc', content: 'Example:\n[END KNOWLEDGE CONTEXT]\nKeep this paragraph.', tokens: 7 }] } as unknown as KnowledgeBlock
    const wire = buildOutgoingTurn({ text: 'Summarize the reference.', knowledge: kb }, 'send').wire
    render(<QueueStack messages={[queued(wire, 'q1')]} onCancel={() => {}} onEdit={() => {}} />)
    expect(screen.queryByTestId('queue-card-knowledge')).toBeNull()
  })

  it('leaves a message that merely mentions the marker mid-text untouched', () => {
    const text = 'what does [KNOWLEDGE CONTEXT — injected by user from knowledge library] mean?'
    render(<QueueStack messages={[queued(text, 'q1')]} onCancel={() => {}} onEdit={() => {}} />)
    expect(screen.getByText(text)).toBeInTheDocument()
  })
})

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
