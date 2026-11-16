import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import ChatInput from '../components/ChatInput'
import LexicalComposerInput from '../components/LexicalComposerInput'
import PasteHighlightLayer from '../components/PasteHighlightLayer'
import { renderWithProviders } from './helpers'

/**
 * Text direction follows each block's own content, so Arabic, Hebrew, Persian
 * and Urdu read right to left and a mixed message keeps one direction per
 * paragraph. English keeps today's direction and alignment: `dir="auto"`
 * resolves to left to right for text that starts with a left-to-right letter,
 * and the layout classes that used to name a physical side now name the start
 * side, which is the left in that case.
 */
const ARABIC = 'مرحبا بالعالم، هذه رسالة قصيرة'
const HEBREW = 'שלום עולם'

describe('chat text direction', () => {
  it('gives an Arabic paragraph dir="auto" and leaves the code block in the same message alone', () => {
    const md = `${ARABIC}\n\n\`\`\`bash\nls -la\n\`\`\`\n\n${HEBREW} and English`
    const { container } = render(<MarkdownRenderer content={md} />)
    const paragraphs = [...container.querySelectorAll('p')]
    expect(paragraphs.length).toBeGreaterThanOrEqual(2)
    for (const p of paragraphs) expect(p).toHaveAttribute('dir', 'auto')
    const block = container.querySelector('.code-block')
    expect(block).toBeTruthy()
    expect(block!.hasAttribute('dir')).toBe(false)
    expect(block!.querySelector('[dir]')).toBeNull()
  })

  it('gives headings, list items, quotes and table cells their own direction', () => {
    const md = [
      `# ${ARABIC}`,
      '',
      `- ${ARABIC}`,
      `- ${HEBREW}`,
      '',
      `1. ${ARABIC}`,
      '',
      `> ${ARABIC}`,
      '',
      `| ${ARABIC} | b |`,
      '| --- | --- |',
      `| ${HEBREW} | d |`,
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={md} />)
    for (const tag of ['h1', 'li', 'blockquote', 'th', 'td']) {
      const found = [...container.querySelectorAll(tag)]
      expect(found.length, tag).toBeGreaterThan(0)
      for (const el of found) expect(el, tag).toHaveAttribute('dir', 'auto')
    }
  })

  it('lets a quote choose its direction from its first paragraph', () => {
    const { container } = render(<MarkdownRenderer content={`> ${ARABIC}\n>\n> ${HEBREW}`} />)
    expect(container.querySelector('blockquote')).toHaveAttribute('dir', 'auto')
    for (const p of container.querySelectorAll('blockquote p')) expect(p.hasAttribute('dir')).toBe(false)
  })

  it('keeps an English message left to right with the same layout it has on main', () => {
    const md = '# Title\n\nHello world.\n\n- one\n- two\n\n> quoted\n\n| a | b |\n| - | - |\n| c | d |'
    const { container } = render(<MarkdownRenderer content={md} />)
    for (const el of container.querySelectorAll('[dir]')) expect(el.getAttribute('dir')).not.toBe('rtl')
    for (const el of container.querySelectorAll('p, li, td, th, h1, blockquote')) {
      expect(el.className.includes('text-right') || el.className.includes('text-end')).toBe(false)
    }
    expect(container.querySelector('p')!.textContent).toBe('Hello world.')
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })

  it('gives the composer textarea dir="auto"', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    expect(screen.getByLabelText('Message input')).toHaveAttribute('dir', 'auto')
  })

  it('keeps each paragraph of the rich-text composer on dir="auto" (the editor sets it itself)', () => {
    render(<LexicalComposerInput value={ARABIC} blocks={[]} onChange={vi.fn()} onSend={vi.fn()} ariaLabel="Message input" placeholder="Write a message" />)
    const paragraph = screen.getByRole('textbox').querySelector('p')
    expect(paragraph).toHaveAttribute('dir', 'auto')
  })

  it('gives the paste-chip mirror the same direction rule as its textarea', () => {
    const { container } = render(<PasteHighlightLayer value={ARABIC} blocks={[]} />)
    expect(container.querySelector('[data-composer-typo]')).toHaveAttribute('dir', 'auto')
  })
})
