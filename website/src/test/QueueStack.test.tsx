import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import QueueStack, { SubagentDeliveryProgress, isSystemDelivery, isNonInteractiveQueued } from '../components/QueueStack'
import type { ChatMessage } from '../types'
import { buildOutgoingTurn } from '../chat-core/composer/outgoingTurn'
import { quoteBlock } from '../chat-core/composer/messageQuote'
import type { KnowledgeBlock } from '../pages/chat/useKnowledgeFetch'
import { findTokenRanges, formatToken, readStoredPaste, type PasteBlock } from '../utils/pasteTokens'

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

describe('QueueStack pasted entry', () => {
  // The queued row carries the WIRE text, in which the composer expanded the
  // `[ Paste #N ]` token in place, plus the blocks the store looked up for it
  // (`meta.pastes`, the same key a user row carries them under).
  const P1: PasteBlock = { id: 'p1', seq: 1, lines: 3, content: 'alpha\nbeta\ngamma' }
  const T1 = formatToken(P1)
  const KB = { totalTokens: 7, items: [{ id: 'k1', title: 'Doc', content: 'kb line one\n\nkb line two', tokens: 7 }] } as unknown as KnowledgeBlock

  function pasted(input: Parameters<typeof buildOutgoingTurn>[0], queueId = 'q1'): ChatMessage {
    const turn = buildOutgoingTurn(input, 'send')
    const { quote, files } = turn.meta
    // In token order, as `queueEntryPastes` reads them off the recorded display.
    const pastes = findTokenRanges(turn.bubble, turn.meta.pastes ?? []).sort((a, b) => a.start - b.start).map(r => r.block)
    return { role: 'queued', content: turn.wire, cls: 'msg msg-queued', ts: '', meta: { queueId, ...(files ? { files } : {}), ...(pastes.length ? { pastes } : {}), ...(quote ? { quote } : {}) } } as ChatMessage
  }

  beforeEach(() => { localStorage.clear() })

  it('shows the token on the card, never the pasted text', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    expect(row.content).toContain('alpha\nbeta\ngamma')
    const { container } = render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={() => {}} />)
    expect(container.textContent).toContain('Paste #1')
    expect(container.textContent).not.toContain('alpha')
    expect(screen.getByText(`look at ${T1} please`)).toBeInTheDocument()
  })

  it('shows the text as sent when the row carries no blocks', () => {
    const wire = buildOutgoingTurn({ text: `look at ${T1} please`, pastes: [P1] }, 'send').wire
    const { container } = render(<QueueStack messages={[queued(wire, 'q1')]} onCancel={() => {}} onEdit={() => {}} />)
    expect(container.textContent).toContain('alpha')
    expect(container.textContent).not.toContain('Paste #1')
  })

  it('opens the editor on the token and expands it again when the edit commits', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    expect(input.value).toBe(`look at ${T1} please`)
    fireEvent.change(input, { target: { value: `look at ${T1} now` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const expected = buildOutgoingTurn({ text: `look at ${T1} now`, pastes: [P1] }, 'send').wire
    expect(expected).toContain('alpha\nbeta\ngamma')
    expect(onEdit).toHaveBeenCalledWith('q1', expected, [P1])
    // The edit is on record under its new wire text, so the store finds the
    // blocks for the edited row and the sent bubble finds them after a reload.
    expect(readStoredPaste(expected.trim())).toMatchObject({ displayTxt: `look at ${T1} now`, pastes: [P1] })
  })

  it('records the edit only when it commits, never per keystroke, and without the attachment list', () => {
    const row = pasted({ text: `look at ${T1} please`, files: ['/tmp/a.txt'], pastes: [P1] })
    expect(row.meta?.files).toEqual(['/tmp/a.txt'])
    const onEditDraft = vi.fn()
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} onEditDraft={onEditDraft} />)
    const input = openEditor()
    // The editor holds the typed line AND the attachment marker line below it.
    expect(input.value).toBe(`look at ${T1} please\n[attached_file 1] /tmp/a.txt`)
    localStorage.clear()
    for (const word of ['n', 'no', 'now']) fireEvent.change(input, { target: { value: `look at ${T1} ${word}\n[attached_file 1] /tmp/a.txt` } })
    expect(input.value).toBe(`look at ${T1} now\n[attached_file 1] /tmp/a.txt`)
    // Each keystroke built a live draft for the side surface, so the surface
    // tracked what the edit would send, and the paste side table saw none.
    expect(onEditDraft).toHaveBeenCalledWith('q1', buildOutgoingTurn({ text: `look at ${T1} now`, files: ['/tmp/a.txt'], pastes: [P1] }, 'send').wire)
    expect(localStorage.getItem('mc-paste-store-v1')).toBeNull()
    fireEvent.keyDown(input, { key: 'Enter' })
    const wire = buildOutgoingTurn({ text: `look at ${T1} now`, files: ['/tmp/a.txt'], pastes: [P1] }, 'send').wire
    expect(onEdit).toHaveBeenCalledWith('q1', wire, [P1])
    const stored = readStoredPaste(wire.trim())
    expect(stored).toMatchObject({ pastes: [P1] })
    // The gateway prunes the entry's list to the markers the edit kept; a list
    // on the record would be put back over it on a reload.
    expect(stored?.files).toBeUndefined()
  })

  it('refuses a fold when a carried block has no body in the text, and edits the text as sent', () => {
    // The row carries two blocks but the text holds only one body (another
    // tab's edit took the other paste out): folding one and treating the
    // other as live would let a commit expand a token that is not there.
    const P2: PasteBlock = { id: 'p2', seq: 2, lines: 2, content: 'delta\nepsilon' }
    const wire = buildOutgoingTurn({ text: `look at ${T1}`, pastes: [P1] }, 'send').wire
    const row = { role: 'queued', content: wire, cls: '', ts: '', meta: { queueId: 'q1', pastes: [P1, P2] } } as ChatMessage
    const onEdit = vi.fn()
    const { container } = render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    expect(container.textContent).not.toContain('Paste #')
    expect(container.textContent).toContain('alpha')
    const input = openEditor()
    expect(input.value).toBe(wire)
    fireEvent.change(input, { target: { value: wire + ' ok' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // The row carried blocks, so the commit names what the edit kept: nothing.
    expect(onEdit).toHaveBeenCalledWith('q1', wire + ' ok', [])
  })

  it('attributes a shorter body followed by typed text that completes a longer sibling to its own token', () => {
    // #1 is a prefix of #2, and the typed text after #1 happens to complete
    // #2's body: the text alone reads as "#2 then #1" as well, so the fold
    // must follow the recorded token order, or deleting #1 would send part
    // of #2 and move the words across the separator.
    const P2: PasteBlock = { id: 'p2', seq: 2, lines: 4, content: 'alpha\nbeta\ngamma\ndelta' }
    const typed = `${T1}\ndelta\n---\n${formatToken(P2)}`
    const row = pasted({ text: typed, pastes: [P1, P2] })
    expect(row.meta?.pastes).toEqual([P1, P2])
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    expect(input.value).toBe(typed)
    fireEvent.change(input, { target: { value: `delta\n---\n${formatToken(P2)}` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', `delta\n---\n${P2.content}`, [P2])
  })

  it('shows the text as sent when the body also stands in it as plain words', () => {
    // A paste expanded in place in the composer, then pasted again collapsed
    // after "compare with": the token could sit on either copy, so the card
    // shows the text as sent and an edit sends the words as typed.
    const row = pasted({ text: `${P1.content}\ncompare with\n${T1}`, pastes: [P1] })
    const onEdit = vi.fn()
    const { container } = render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    expect(container.textContent).not.toContain('Paste #')
    const input = openEditor()
    expect(input.value).toBe(row.content)
    fireEvent.change(input, { target: { value: `compare with\n${P1.content}` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', `compare with\n${P1.content}`, [])
  })

  it('folds two pastes whose bodies overlap onto their own tokens', () => {
    const P2: PasteBlock = { id: 'p2', seq: 2, lines: 4, content: 'alpha\nbeta\ngamma\ndelta' }
    const row = pasted({ text: `${formatToken(P2)}\n${T1}`, pastes: [P1, P2] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    expect(input.value).toBe(`${formatToken(P2)}\n${T1}`)
    // Deleting the shorter token removes ITS body, not the start of the longer one.
    fireEvent.change(input, { target: { value: formatToken(P2) } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', P2.content, [P2])
  })

  it('treats the token as one unit in the editor, through the composer\'s own handlers', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    const start = input.value.indexOf('[ Paste')
    const end = start + T1.length
    // A selection that ends inside the token snaps to the token's edge.
    input.setSelectionRange(start - 3, start + 4)
    fireEvent.select(input)
    expect([input.selectionStart, input.selectionEnd]).toEqual([start - 3, start])
    // Backspace with the caret just past the token removes the whole token.
    input.setSelectionRange(end, end)
    fireEvent.keyDown(input, { key: 'Backspace' })
    expect(input.value).toBe('look at  please')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', 'look at  please', [])
  })

  it('Delete with the caret just before the token removes it whole', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={() => {}} />)
    const input = openEditor()
    const start = input.value.indexOf('[ Paste')
    input.setSelectionRange(start, start)
    fireEvent.keyDown(input, { key: 'Delete' })
    expect(input.value).toBe('look at  please')
  })

  it('does not commit a broken token: the editor stays open and nothing is sent', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    // A cut or a paste inside the token, which no key handler sees: the
    // closing bracket is gone, so the token no longer resolves to its block.
    fireEvent.change(input, { target: { value: `look at ${T1.slice(0, -1)} please` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.blur(input)
    expect(onEdit).not.toHaveBeenCalled()
    // The head cut away through both invisible fences, leaving only the tail.
    fireEvent.change(input, { target: { value: `look at ${T1.slice(T1.indexOf(' · '))} please` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Edit queued message' })).toBeInTheDocument()
    // Escape still leaves the edit.
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('textbox', { name: 'Edit queued message' })).toBeNull()
  })

  it('commits a literal "[ Paste #" typed beside an intact token', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: `look at ${T1} and search for "[ Paste #"` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', `look at ${P1.content} and search for "[ Paste #"`, [P1])
  })

  it('commits typed words that carry zero-width joiners of their own, beside an intact token', () => {
    // Persian: U+200C (ZWNJ) is an ordinary character of the word, and the
    // same code point is a bit of a token's invisible id run. Only a token's
    // fence (U+2063) or a `[ Paste #` fragment counts as a broken token.
    const persian = 'می\u200cخواهم'
    const row = pasted({ text: `${persian} ${T1}`, pastes: [P1] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: `${persian} ${T1} حالا` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', `${persian} ${P1.content} حالا`, [P1])
  })

  it('commits from the row the editor opened on when another tab rewrites the entry meanwhile', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    const onEdit = vi.fn()
    const { rerender } = render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    expect(input.value).toBe(`look at ${T1} please`)
    // Another tab removed the paste and saved plain words: the queue_edit
    // frame rewrites the live row, blocks gone, while this editor is open.
    rerender(<QueueStack messages={[{ ...row, content: 'plain words instead', meta: { queueId: 'q1' } } as ChatMessage]} onCancel={() => {}} onEdit={onEdit} />)
    fireEvent.change(input, { target: { value: `look at ${T1} now` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // The token the editor shows still expands: the commit reads the blocks
    // the editor opened with, never the literal token.
    expect(onEdit).toHaveBeenCalledWith('q1', `look at ${P1.content} now`, [P1])
  })

  it('a token the editor expanded inline is sent as the text shown, never expanded a second time', () => {
    // The pasted body itself holds a token-shaped literal with the same
    // number as the row's block. Expanded inline in the editor (double click),
    // the block is retired; the commit must send the text as shown and not
    // resolve that literal against the retired block.
    const PX: PasteBlock = { id: 'px', seq: 1, lines: 3, content: 'Example:\n[ Paste #1 · 3 lines ]\nEnd' }
    const row = pasted({ text: `see ${formatToken(PX)}`, pastes: [PX] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    const start = input.value.indexOf('[ Paste')
    input.setSelectionRange(start + 2, start + 2)
    fireEvent.click(input, { detail: 2 })
    expect(input.value).toBe(`see ${PX.content}`)
    fireEvent.change(input, { target: { value: `see ${PX.content} now` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', `see ${PX.content} now`, [])
  })

  it('a token a native undo puts back is expanded again on commit', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    // The browser deletes the selected first line (token included) as a
    // native edit, then Ctrl/Cmd+Z puts the text back: both reach the editor
    // as plain value changes, with no key the token handlers see.
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.change(input, { target: { value: `look at ${T1} please` } })
    fireEvent.change(input, { target: { value: `look at ${T1} now` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', `look at ${P1.content} now`, [P1])
  })

  it('an edit that removes the token drops its paste from the send', () => {
    const row = pasted({ text: `look at ${T1} please`, pastes: [P1] })
    const onEdit = vi.fn()
    render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: 'never mind, skip it' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', 'never mind, skip it', [])
    expect(readStoredPaste('never mind, skip it')).toBeNull()
  })

  it('folds the token back beside a quote and a knowledge envelope, and an edit restores all three', () => {
    const quote = { role: 'assistant' as const, text: 'the quoted reply', ts: 't0' }
    const row = pasted({ text: `compare ${T1}`, pastes: [P1], knowledge: KB, quote })
    const onEdit = vi.fn()
    const { container } = render(<QueueStack messages={[row]} onCancel={() => {}} onEdit={onEdit} />)
    expect(screen.getByText(`compare ${T1}`)).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/alpha|KNOWLEDGE CONTEXT|quoting an earlier message/)
    expect(screen.getByTestId('queue-card-knowledge')).toBeInTheDocument()
    const input = openEditor()
    expect(input.value).toBe(`compare ${T1}`)
    fireEvent.change(input, { target: { value: `contrast ${T1}` } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('q1', buildOutgoingTurn({ text: `contrast ${T1}`, pastes: [P1], knowledge: KB, quote }, 'send').wire, [P1])
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

  it('retires the live edit draft on commit', () => {
    const onEditDraft = vi.fn()
    render(<QueueStack messages={[queued('old text', 'q1')]} onEdit={vi.fn()} onEditDraft={onEditDraft} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: 'new text' } })
    expect(onEditDraft).toHaveBeenLastCalledWith('q1', 'new text')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEditDraft).toHaveBeenLastCalledWith('q1', undefined)
  })

  it('retires the live edit draft on cancel', () => {
    const onEditDraft = vi.fn()
    render(<QueueStack messages={[queued('old text', 'q1')]} onEdit={vi.fn()} onEditDraft={onEditDraft} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: 'discarded text' } })
    expect(onEditDraft).toHaveBeenLastCalledWith('q1', 'discarded text')
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onEditDraft).toHaveBeenLastCalledWith('q1', undefined)
  })

  it('retires the live edit draft when the editor unmounts', () => {
    const onEditDraft = vi.fn()
    const view = render(<QueueStack messages={[queued('old text', 'q1')]} onEdit={vi.fn()} onEditDraft={onEditDraft} />)
    const input = openEditor()
    fireEvent.change(input, { target: { value: 'abandoned text' } })
    expect(onEditDraft).toHaveBeenLastCalledWith('q1', 'abandoned text')
    view.unmount()
    expect(onEditDraft).toHaveBeenLastCalledWith('q1', undefined)
  })

  it('reports no live edit draft while the typed words are blank, though the card carries knowledge or a quote', () => {
    const kb = { totalTokens: 7, items: [{ id: 'k1', title: 'Doc', content: 'kb line one', tokens: 7 }] } as unknown as KnowledgeBlock
    const quote = { role: 'assistant' as const, text: 'the quoted reply', ts: 't0' }
    const cards: ChatMessage[] = [
      queued(buildOutgoingTurn({ text: 'summarize the doc', knowledge: kb }, 'send').wire, 'q1'),
      { role: 'queued', content: `${quoteBlock(quote)}\n\nwhy though?`, cls: '', ts: 't', meta: { queueId: 'q1', quote } } as ChatMessage,
    ]
    for (const card of cards) {
      const onEditDraft = vi.fn()
      const view = render(<QueueStack messages={[card]} onEdit={vi.fn()} onEditDraft={onEditDraft} />)
      const input = openEditor()
      fireEvent.change(input, { target: { value: '  ' } })
      expect(onEditDraft).toHaveBeenLastCalledWith('q1', undefined)
      fireEvent.change(input, { target: { value: 'new words' } })
      const reglued = card.meta?.quote
        ? `${quoteBlock(quote)}\n\nnew words`
        : buildOutgoingTurn({ text: 'new words', knowledge: kb }, 'send').wire
      expect(onEditDraft).toHaveBeenLastCalledWith('q1', reglued)
      view.unmount()
    }
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
