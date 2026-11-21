import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

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

  // jsdom lays nothing out, so these pin which element carries `dir` (the
  // container whose search must reach the text) and which carries
  // `unicode-bidi: plaintext` (a block that still picks its own direction).
  const PLAINTEXT = '[unicode-bidi:plaintext]'
  const ownsDir = (el: Element | null) => expect(el).toHaveAttribute('dir', 'auto')
  const followsOwnText = (el: Element) => {
    expect(el.hasAttribute('dir'), el.outerHTML).toBe(false)
    expect(el.className.split(' '), el.outerHTML).toContain(PLAINTEXT)
  }

  it('lets a loose list item read its paragraph, so the marker sits on the text side', () => {
    const { container } = render(<MarkdownRenderer content={`1. ${ARABIC}\n\n2. ${HEBREW}`} />)
    const items = [...container.querySelectorAll('li')]
    expect(items).toHaveLength(2)
    for (const li of items) {
      ownsDir(li)
      followsOwnText(li.querySelector('p')!)
    }
  })

  it('lets a quote that opens with a list or heading read that text', () => {
    const list = render(<MarkdownRenderer content={`> - ${ARABIC}\n>   - nested English\n> - ${HEBREW}`} />).container
    ownsDir(list.querySelector('blockquote'))
    const [first, nested, second] = [...list.querySelectorAll('li')]
    followsOwnText(first)
    followsOwnText(second)
    ownsDir(nested)
    const heading = render(<MarkdownRenderer content={`> # ${ARABIC}\n>\n> ${HEBREW}`} />).container
    ownsDir(heading.querySelector('blockquote'))
    followsOwnText(heading.querySelector('blockquote h1')!)
    const nestedQuote = render(<MarkdownRenderer content={`> > ${ARABIC}`} />).container
    const [outer, inner] = [...nestedQuote.querySelectorAll('blockquote')]
    ownsDir(outer)
    expect(inner.hasAttribute('dir')).toBe(false)
  })

  it('keeps an English paragraph in a right-to-left quote on its own direction', () => {
    const { container } = render(<MarkdownRenderer content={`> ${ARABIC}\n>\n> Hello world.`} />)
    ownsDir(container.querySelector('blockquote'))
    const paragraphs = [...container.querySelectorAll('blockquote p')]
    expect(paragraphs.map(p => p.textContent)).toEqual([ARABIC, 'Hello world.'])
    for (const p of paragraphs) followsOwnText(p)
  })

  it('lets a table cell read a paragraph inside it', () => {
    const { container } = render(<MarkdownRenderer content={`<table><tr><td><p>${ARABIC}</p></td></tr></table>`} />)
    ownsDir(container.querySelector('td'))
    followsOwnText(container.querySelector('td p')!)
  })

  it('keeps a top-level paragraph and heading on their own dir="auto" with no plaintext class', () => {
    const { container } = render(<MarkdownRenderer content={`# ${ARABIC}\n\n${HEBREW}`} />)
    for (const el of container.querySelectorAll('h1, p')) {
      ownsDir(el)
      expect(el.className).not.toContain(PLAINTEXT)
    }
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
})
