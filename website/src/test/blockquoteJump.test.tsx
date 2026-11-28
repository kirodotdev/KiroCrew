import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { QuoteJumpCtx } from '../components/markdown/contexts'
import UserMessage from '../pages/chat/UserMessage'

const JUMP = 'Jump to the quoted message'

describe('blockquote jump button', () => {
  it('offers the jump the host resolves for the quote, beside Copy, and runs it on click', () => {
    const jump = vi.fn()
    const quoteJumpFor = vi.fn(() => jump)
    render(
      <QuoteJumpCtx.Provider value={quoteJumpFor}>
        <MarkdownRenderer content={'> Run the CLI\n> > nested\n\nwhy?'} />
      </QuoteJumpCtx.Provider>,
    )
    // One control per top-level quote, handed its markdown source.
    expect(quoteJumpFor).toHaveBeenCalledWith('> Run the CLI\n> > nested')
    expect(screen.getAllByRole('button', { name: JUMP })).toHaveLength(1)
    expect(screen.getAllByTestId('blockquote-copy')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: JUMP }))
    expect(jump).toHaveBeenCalledTimes(1)
  })

  it('draws no jump where the host resolves none, or offers none at all', () => {
    const { unmount } = render(
      <QuoteJumpCtx.Provider value={() => null}>
        <MarkdownRenderer content={'> pasted from elsewhere'} />
      </QuoteJumpCtx.Provider>,
    )
    expect(screen.queryByRole('button', { name: JUMP })).toBeNull()
    unmount()
    render(<MarkdownRenderer content={'> a reply quoting someone'} />)
    expect(screen.queryByRole('button', { name: JUMP })).toBeNull()
    expect(screen.getByTestId('blockquote-copy')).toBeTruthy()
  })

  it('is offered by a sent message for its own quotes, and a double click on it does not open Edit', () => {
    const jump = vi.fn()
    const onJumpToQuotedText = vi.fn(() => jump)
    const onEditResend = vi.fn(() => true)
    render(
      <UserMessage
        content={'> Run the CLI\n\nwhy?'}
        messageTs="t3"
        renderContent={c => <MarkdownRenderer content={c} />}
        canEdit
        doubleClickToEdit
        onEditResend={onEditResend}
        onJumpToQuotedText={onJumpToQuotedText}
      />,
    )
    expect(onJumpToQuotedText).toHaveBeenCalledWith('> Run the CLI', 't3')
    const button = screen.getByRole('button', { name: JUMP })
    fireEvent.click(button)
    fireEvent.doubleClick(button)
    expect(jump).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('looks again when earlier rows load, so a reply paged in after the row drew gets its jump', () => {
    const jump = vi.fn()
    const onJumpToQuotedText = vi.fn<(quoted: string, fromTs: string) => (() => void) | null>(() => null)
    // One function for both renders, as the transcript passes: the markdown below stays memo'd.
    const renderContent = (c: string) => <MarkdownRenderer content={c} />
    const row = (messageIndex: number) => (
      <UserMessage content={'> Run the CLI\n\nwhy?'} messageTs="t3" messageIndex={messageIndex} renderContent={renderContent} onJumpToQuotedText={onJumpToQuotedText} />
    )
    const { rerender } = render(row(0))
    expect(screen.queryByRole('button', { name: JUMP })).toBeNull()
    // Load earlier: the reply is in the transcript now, and the rows before this one moved its index.
    onJumpToQuotedText.mockReturnValue(jump)
    rerender(row(4))
    fireEvent.click(screen.getByRole('button', { name: JUMP }))
    expect(jump).toHaveBeenCalledTimes(1)
  })
})
