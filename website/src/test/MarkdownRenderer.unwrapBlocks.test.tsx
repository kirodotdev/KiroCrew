import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * Tests for the rehypeUnwrapBlocks plugin.
 *
 * When raw HTML block elements (div, details, section, etc.) appear in markdown
 * that react-markdown wraps in <p> tags, the browser's HTML parser auto-closes
 * the <p> before the block element. React's VDOM doesn't know about this DOM
 * mutation, so on the next reconciliation it tries to `removeChild` from <p>
 * and crashes with:
 *   "Failed to execute 'removeChild' on 'Node': The node to be removed is not
 *    a child of this node."
 *
 * The rehypeUnwrapBlocks plugin fixes this by splitting <p> elements that
 * contain block-level children at the HAST level, before React ever renders.
 */
describe('MarkdownRenderer block-in-paragraph unwrap', () => {
  it('renders a <div> inside markdown paragraph without crashing', () => {
    // This markdown produces a paragraph containing a raw <div> — the exact
    // pattern that triggers the removeChild crash without the fix.
    const md = 'Hello\n\n<div>block content</div>\n\nWorld'
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.textContent).toContain('block content')
    expect(container.textContent).toContain('Hello')
    expect(container.textContent).toContain('World')
    // The <div> must NOT be inside a <p>
    const div = container.querySelector('div div')
    expect(div).toBeTruthy()
    expect(div!.closest('p')).toBeNull()
  })

  it('renders <details> inside markdown without crashing', () => {
    const md = 'Before\n\n<details><summary>Click</summary>Hidden</details>\n\nAfter'
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.querySelector('details')).toBeTruthy()
    expect(container.querySelector('details')!.closest('p')).toBeNull()
    expect(container.textContent).toContain('Click')
    expect(container.textContent).toContain('Hidden')
  })

  it('renders inline HTML (span, strong) inside <p> normally', () => {
    // Inline elements should remain inside <p> — not unwrapped
    const md = 'Hello <span>inline</span> world'
    const { container } = render(<MarkdownRenderer content={md} />)
    const span = container.querySelector('span')
    expect(span).toBeTruthy()
    // The span should be inside a <p>
    expect(span!.closest('p')).toBeTruthy()
  })

  it('splits text around a block element into separate paragraphs', () => {
    // Mixed inline text and block element in same paragraph context
    const md = 'before <div>middle</div> after'
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.textContent).toContain('before')
    expect(container.textContent).toContain('middle')
    expect(container.textContent).toContain('after')
    // The div must not be inside a <p>
    const divs = container.querySelectorAll('div div')
    const blockDiv = Array.from(divs).find(d => d.textContent === 'middle')
    expect(blockDiv).toBeTruthy()
    expect(blockDiv!.closest('p')).toBeNull()
  })

  it('survives re-render with changing content (streaming simulation)', () => {
    // Simulates the re-render pattern that occurs during streaming: first render
    // partial content, then update with block element. The crash happened when
    // React tried to reconcile the DOM after a block element appeared.
    const { container, rerender } = render(<MarkdownRenderer content="Start typing" />)
    expect(container.textContent).toContain('Start typing')

    // Now content updates with a block element
    rerender(<MarkdownRenderer content={"Start typing\n\n<div>streamed block</div>"} />)
    expect(container.textContent).toContain('streamed block')

    // Another update
    rerender(<MarkdownRenderer content={"Start typing\n\n<div>streamed block</div>\n\nDone"} />)
    expect(container.textContent).toContain('Done')
  })

  it('handles nested block elements', () => {
    const md = '<div><section>nested</section></div>'
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.querySelector('section')).toBeTruthy()
    expect(container.querySelector('section')!.closest('p')).toBeNull()
  })
})
