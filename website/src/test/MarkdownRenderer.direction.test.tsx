import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

describe('Markdown content direction', () => {
  it('gives independent prose blocks automatic direction', () => {
    const { container } = render(<MarkdownRenderer content={'שלום עולם.\n\nEnglish paragraph.\n\n## כותרת'} />)
    const prose = container.querySelectorAll('p, h2')
    expect(prose).toHaveLength(3)
    for (const block of prose) expect(block).toHaveAttribute('dir', 'auto')
  })

  it('preserves explicit direction and language on prose', () => {
    const { container } = render(<MarkdownRenderer content={'<p dir="rtl" lang="he">AWS זמין בישראל.</p><h2 dir="ltr" lang="en">English</h2>'} />)
    expect(container.querySelector('p')).toHaveAttribute('dir', 'rtl')
    expect(container.querySelector('p')).toHaveAttribute('lang', 'he')
    expect(container.querySelector('h2')).toHaveAttribute('dir', 'ltr')
    expect(container.querySelector('h2')).toHaveAttribute('lang', 'en')
  })

  it('isolates technical inline code from Hebrew prose without changing text', () => {
    const { container } = render(<MarkdownRenderer content={'הריצו `npm test` בשעה 09:00.'} />)
    const code = container.querySelector('code')
    expect(code).toHaveAttribute('dir', 'ltr')
    expect(code).toHaveTextContent('npm test')
    expect(container.querySelector('p')?.textContent).toBe('הריצו npm test בשעה 09:00.')
  })

  it('resolves list direction separately from each item and each table cell', () => {
    const { container } = render(<MarkdownRenderer content={'- שלום\n- English\n\n| שם | Value |\n| --- | --- |\n| ישראל | 123 |'} />)
    for (const block of container.querySelectorAll('li, th, td')) {
      expect(block).toHaveAttribute('dir', 'auto')
    }
    expect(container.querySelectorAll('th')[0]).toHaveTextContent('שם')
    expect(container.querySelectorAll('th')[1]).toHaveTextContent('Value')
  })

  it('honors explicit ancestor direction without resetting its paragraphs', () => {
    const { container } = render(<MarkdownRenderer content={'<div dir="rtl" lang="he"><p>AWS זמין בישראל.</p><p dir="ltr">English.</p></div>'} />)
    expect(container.querySelector('div[dir="rtl"]')).toHaveAttribute('lang', 'he')
    expect(container.querySelectorAll('p')[0]).not.toHaveAttribute('dir')
    expect(container.querySelectorAll('p')[1]).toHaveAttribute('dir', 'ltr')
  })

  it('honors case-insensitive HTML direction keywords', () => {
    const { container } = render(<MarkdownRenderer content={'<p dir="RTL">AWS זמין בישראל.</p>'} />)
    expect(container.querySelector('p')).toHaveAttribute('dir', 'rtl')
  })

  it.each(['auto', 'rtl', 'ltr'])('keeps links inside authored %s containers eligible for inheritance', direction => {
    const { container } = render(<MarkdownRenderer content={`<div dir="${direction}"><p><a href="https://example.com/">שלום מפורש</a></p></div>`} />)
    expect(container.querySelector('a')).not.toHaveAttribute('dir')
    expect(container.querySelector('p')).not.toHaveAttribute('dir')
  })

  it('isolates inline equations from RTL prose', () => {
    const { container } = render(<MarkdownRenderer content={'משוואה \\(x - y = 12\\) בעברית.'} />)
    expect(container.querySelector('.katex')).toHaveAttribute('dir', 'ltr')
  })

  it('keeps quote and loose-list direction fed by their prose', () => {
    const { container } = render(<MarkdownRenderer content={'> שלום.\n>\n> English.\n\n- שלום.\n\n  עוד משפט.\n\n- English.'} />)
    const quote = container.querySelector('blockquote')!
    expect(quote).toHaveAttribute('dir', 'auto')
    expect(quote.querySelectorAll('p')[0]).not.toHaveAttribute('dir')
    expect(quote.querySelectorAll('p')[1]).toHaveAttribute('dir', 'auto')
    const item = container.querySelector('li')!
    expect(item).toHaveAttribute('dir', 'auto')
    expect(item.querySelector('p')).not.toHaveAttribute('dir')
  })

  it('preserves explicit table alignment independently of content direction', () => {
    const { container } = render(<MarkdownRenderer content={'| שם | Value |\n| :---: | ---: |\n| ישראל | 123 |'} />)
    expect(container.querySelectorAll('th')[0]).toHaveStyle({ textAlign: 'center' })
    expect(container.querySelectorAll('th')[1]).toHaveStyle({ textAlign: 'right' })
    expect(container.querySelectorAll('td')[0]).toHaveStyle({ textAlign: 'center' })
  })

  it('keeps Hebrew direction during streaming and after completion', () => {
    const { container, rerender } = render(<MarkdownRenderer content={'שָׁלוֹם עולם.'} streaming glow />)
    expect(container.querySelector('p')).toHaveAttribute('dir', 'auto')
    expect(container.querySelector('.streaming-caret')?.closest('p')).toHaveAttribute('dir', 'auto')
    rerender(<MarkdownRenderer content={'שָׁלוֹם עולם.'} />)
    expect(container.querySelector('p')).toHaveAttribute('dir', 'auto')
    expect(container.querySelector('p')?.textContent).toBe('שָׁלוֹם עולם.')
  })
})
