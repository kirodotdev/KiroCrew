import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import CrewComposer from './CrewComposer'

/* A Bot's chat composer (the real one, not a stub) follows Settings -> Chat ->
 * Show Pasted Text in Full as the Sessions chat does. Its host, ChatPane,
 * passes no `showFullPastes` prop, so the composer's own read of the stored
 * choice is the only thing that can keep a long paste as text here. */

const pasteText = (textarea: HTMLElement, text: string) =>
  fireEvent.paste(textarea, { clipboardData: { types: ['text/plain'], items: [], getData: () => text } })

// Over both collapse thresholds (4 lines, >200 chars): only the setting decides.
const big = `${'x'.repeat(250)}\nsecond\nthird\nfourth`

beforeEach(() => {
  ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => false)
})

describe("a Bot's chat composer and Show Pasted Text in Full", () => {
  it('collapses a large paste into a chip with the setting off (the default)', () => {
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(<CrewComposer value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} />)
    pasteText(screen.getByRole('textbox'), big)
    expect(onPasteBlocksChange).toHaveBeenCalledWith([expect.objectContaining({ seq: 1, lines: 4, content: big })])
  })

  it('keeps a large paste as full text with the setting on', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ showFullPastes: true }))
    const onChange = vi.fn()
    const onPasteBlocksChange = vi.fn()
    renderWithProviders(<CrewComposer value="" onChange={onChange} onSend={vi.fn()} onPasteBlocksChange={onPasteBlocksChange} />)
    // Trailing blank lines make the paste handler insert the cleaned text itself,
    // so the full text is observable here rather than left to the browser.
    pasteText(screen.getByRole('textbox'), `${big}\n\n`)
    expect(onChange).toHaveBeenCalledWith(big)
    expect(onPasteBlocksChange).not.toHaveBeenCalled()
  })
})
