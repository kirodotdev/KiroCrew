import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import PasteBlockChip, { pasteFirstLine, pasteSnippet } from '../composer/PasteBlockChip'

/**
 * The composer's inline paste pill. Presentational (no Lexical): renders the
 * i18n label + a FileText icon + a ✕, is a keyboard-operable role="button", and
 * keeps ✕ (remove) strictly separate from a body click (open).
 */
describe('PasteBlockChip', () => {
  it('renders the pluralized label and an accessible name', () => {
    render(<PasteBlockChip seq={2} lines={5} />)
    const chip = screen.getByRole('button', { name: 'Pasted text · 5 lines' })
    expect(chip).toBeTruthy()
    expect(chip.getAttribute('data-paste-seq')).toBe('2')
    expect(screen.getByText('Pasted text · 5 lines')).toBeTruthy()
  })

  it('uses the singular form for one line', () => {
    render(<PasteBlockChip seq={1} lines={1} />)
    expect(screen.getByText('Pasted text · 1 line')).toBeTruthy()
  })

  it('calls onOpen with the chip element on a body click', () => {
    const onOpen = vi.fn()
    render(<PasteBlockChip seq={1} lines={3} onOpen={onOpen} />)
    const chip = screen.getByRole('button', { name: /Pasted text/ })
    fireEvent.click(chip)
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(onOpen.mock.calls[0][0]).toBe(chip)
  })

  it('calls only onRemove (never onOpen) when the ✕ is clicked', () => {
    const onOpen = vi.fn()
    const onRemove = vi.fn()
    render(<PasteBlockChip seq={1} lines={3} onOpen={onOpen} onRemove={onRemove} />)
    fireEvent.click(screen.getByRole('button', { name: 'Remove pasted text' }))
    expect(onRemove).toHaveBeenCalledTimes(1)
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('opens on Enter and Space', () => {
    const onOpen = vi.fn()
    render(<PasteBlockChip seq={1} lines={3} onOpen={onOpen} />)
    const chip = screen.getByRole('button', { name: /Pasted text/ })
    fireEvent.keyDown(chip, { key: 'Enter' })
    fireEvent.keyDown(chip, { key: ' ' })
    expect(onOpen).toHaveBeenCalledTimes(2)
  })

  it('applies the dragging state class', () => {
    render(<PasteBlockChip seq={1} lines={3} state="dragging" />)
    const chip = screen.getByRole('button', { name: /Pasted text/ })
    expect(chip.className).toContain('opacity-40')
  })

  it('applies the selected state ring', () => {
    render(<PasteBlockChip seq={1} lines={3} state="selected" />)
    const chip = screen.getByRole('button', { name: /Pasted text/ })
    expect(chip.className).toContain('ring-accent')
  })

  it('passes through draggable', () => {
    render(<PasteBlockChip seq={1} lines={3} draggable />)
    const chip = screen.getByRole('button', { name: /Pasted text/ })
    expect(chip.getAttribute('draggable')).toBe('true')
  })
})

describe('pasteSnippet', () => {
  it('takes the first NON-BLANK line, collapses whitespace, caps the length', () => {
    expect(pasteSnippet('\n  \n  const  x =\t1;\nmore')).toBe('const x = 1;')
    expect(pasteSnippet('   \n\n')).toBe('')
    const long = 'a'.repeat(200)
    expect(pasteSnippet(long)).toHaveLength(81)
    expect(pasteSnippet(long).endsWith('…')).toBe(true)
  })
})

describe('PasteBlockChip snippet label', () => {
  it('shows the snippet + count visibly; the accessible name LEADS with the snippet (voice control says what it sees)', () => {
    render(<PasteBlockChip seq={1} lines={44} snippet="def main():" />)
    const chip = screen.getByRole('button', { name: 'def main(): · Pasted text · 44 lines' })
    expect(screen.getByTestId('paste-chip-snippet').textContent).toBe('def main():')
    expect(chip.textContent).toContain('44 lines')
    expect(screen.getByTestId('paste-chip-snippet').className).toContain('max-w-[140px]')
  })

  it('falls back to the generic label when there is no snippet', () => {
    render(<PasteBlockChip seq={1} lines={3} snippet="" />)
    expect(screen.getByRole('button', { name: 'Pasted text · 3 lines' }).textContent).toContain('Pasted text · 3 lines')
  })

  it('names the two hidden actions: hover title and accessible description say "Click to edit · drag to reorder"', () => {
    // UX review on #11100: a title that merely restated the snippet left the
    // preview and the reorder discoverable only by trying.
    render(<PasteBlockChip seq={1} lines={6} snippet="def main():" firstLine="def main():" />)
    const chip = screen.getByRole('button', { name: /^def main\(\): · Pasted text · 6 lines$/ })
    expect(chip.getAttribute('title')).toBe('def main():\nClick to edit · drag to reorder')
    expect(chip).toHaveAccessibleDescription('Click to edit · drag to reorder')
  })

  it('shows a decorative pencil glyph so editability is visible without hovering', () => {
    // UX review on #11100 (round 3): the title hint reaches only pointer users
    // who pause; keyboard, touch and non-hovering users need a visible cue. It
    // is decorative (aria-hidden) — the pill is the button and its accessible
    // description already names the action.
    render(<PasteBlockChip seq={1} lines={6} snippet="def main():" />)
    const glyph = screen.getByTestId('paste-chip-edit-glyph')
    expect(glyph.getAttribute('aria-hidden')).toBe('true')
    expect(screen.getByTestId('paste-token-1').contains(glyph)).toBe(true)
  })

  it('the hover title carries the WHOLE first line even when the visible snippet is capped', () => {
    // GPT review on #11100: the title reused the 80-char snippet, so "full first
    // line on hover" was untrue for long lines.
    const long = 'x'.repeat(200)
    render(<PasteBlockChip seq={1} lines={2} snippet={pasteSnippet(long)} firstLine={pasteFirstLine(long)} />)
    const chip = screen.getByTestId('paste-token-1')
    expect(screen.getByTestId('paste-chip-snippet').textContent).toHaveLength(81)
    expect(chip.getAttribute('title')).toBe(`${long}\nClick to edit · drag to reorder`)
  })
})

describe('pasteFirstLine', () => {
  it('is the uncapped, whitespace-normalized first non-blank line', () => {
    expect(pasteFirstLine('\n  \n  const  x =\t1;\nmore')).toBe('const x = 1;')
    expect(pasteFirstLine('   \n\n')).toBe('')
    expect(pasteFirstLine('y'.repeat(500))).toHaveLength(500)
  })
})
