import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * While a reply streams, the server has not yet decided which of its links render
 * live (its blocked-link records arrive with the reply's row). Until then no web
 * link in it is drawn as a link: its text stands in, so a link that carries data
 * out is never clickable for a moment. Once the reply has landed, the links render
 * by the records as usual.
 */
const REPLY = 'See [the report](https://collect.example.com/a?d=QUJDREVGR0hJSktMTU5PUFFSU1RVVldY) and https://docs.example.com/page for details.'

afterEach(cleanup)

describe('links in a streaming reply', () => {
  it('draws no web link while the reply streams', () => {
    const { container } = render(<MarkdownRenderer content={REPLY} streaming />)
    expect(container.querySelectorAll('a[href^="http"]')).toHaveLength(0)
    expect(container.textContent).toContain('the report')
    expect(container.textContent).toContain('https://docs.example.com/page')
  })

  it('draws the links once the reply has landed', () => {
    const { container, rerender } = render(<MarkdownRenderer content={REPLY} streaming />)
    rerender(<MarkdownRenderer content={REPLY} streaming={false} />)
    const hrefs = Array.from(container.querySelectorAll('a[href^="http"]')).map(a => a.getAttribute('href'))
    expect(hrefs).toContain('https://docs.example.com/page')
  })
})
