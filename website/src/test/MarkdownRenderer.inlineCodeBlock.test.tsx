import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * Regression tests for a `<div>` landing inside a `<p>` via the `code`
 * component override.
 *
 * `MD_COMPONENTS.code` renders a block-level component (CodeBlock,
 * MermaidBlock and ExcalidrawBlock are each rooted in a `<div>`) for a code
 * element carrying a class. Real fenced blocks never reach it — those are
 * segmented out of the source by `useBlockAssembler` — so the classed code
 * elements that do arrive come from raw HTML in prose. A bare
 * `<code class="language-js">` mid-sentence therefore rendered a `<div>` inside
 * the enclosing `<p>`. The browser hoists it out, React's VDOM keeps the stale
 * parent, and the next reconciliation throws:
 *   "Failed to execute 'removeChild' on 'Node': The node to be removed is not
 *    a child of this node."
 *
 * `rehypeUnwrapBlocks` cannot catch this because it reads block-ness off the
 * HAST tag name, and `code` is inline there. `rehypeMarkFencedCode` stamps
 * `data-fenced` on `<pre> > <code>` so the override renders a block for exactly
 * those and keeps everything else inline.
 */
describe('MarkdownRenderer inline code with a language class', () => {
  it('keeps an inline classed <code> inside the paragraph, with no block child', () => {
    const md = 'Use <code class="language-js">const x = 1</code> in your file.'
    const { container } = render(<MarkdownRenderer content={md} />)
    // The crash precondition: any <div> descendant of a <p>.
    expect(container.querySelectorAll('p div')).toHaveLength(0)
    const code = container.querySelector('p code')
    expect(code).toBeTruthy()
    expect(container.textContent).toContain('const x = 1')
    // The surrounding sentence is not split apart.
    expect(container.textContent).toContain('Use')
    expect(container.textContent).toContain('in your file.')
  })

  it('keeps an inline language-mermaid <code> inline instead of drawing a diagram', () => {
    const md = 'Diagram <code class="language-mermaid">graph TD; A--&gt;B</code> inline.'
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.querySelectorAll('p div')).toHaveLength(0)
    expect(container.querySelector('p code')).toBeTruthy()
  })

  it('still renders a raw <pre><code> as a block code block outside any <p>', () => {
    const md = 'Before\n\n<pre><code class="language-js">const a = 1</code></pre>\n\nAfter'
    const { container } = render(<MarkdownRenderer content={md} />)
    const block = container.querySelector('.code-block')
    expect(block).toBeTruthy()
    expect(block!.closest('p')).toBeNull()
    expect(container.textContent).toContain('const a = 1')
  })

  it('leaves a real fenced code block unaffected', () => {
    const { container } = render(<MarkdownRenderer content={'```js\nconst a = 1\n```'} />)
    expect(container.textContent).toContain('const a = 1')
    expect(container.querySelectorAll('p div')).toHaveLength(0)
  })

  it('ignores an author-supplied data-fenced marker on an inline <code>', () => {
    // `isAllowedAttr` admits every `data-*`, so raw HTML can carry its own
    // marker. If the override trusted it, an inline <code> could claim block
    // rendering and put a <div> back inside the <p>.
    const md = 'Use <code data-fenced class="language-js">const x = 1</code> here.'
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.querySelectorAll('p div')).toHaveLength(0)
    expect(container.querySelector('p code')).toBeTruthy()
    expect(container.querySelector('[data-fenced]')).toBeNull()
  })

  it('ignores an author-supplied camelCased dataFenced marker on an inline <code>', () => {
    const md = 'Use <code dataFenced class="language-js">const x = 1</code> here.'
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.querySelectorAll('p div')).toHaveLength(0)
    expect(container.querySelector('p code')).toBeTruthy()
  })

  it('does not leak the data-fenced marker into the DOM', () => {
    const md = 'text <code class="language-js">x</code> more\n\n<pre><code class="language-js">y</code></pre>'
    const { container } = render(<MarkdownRenderer content={md} />)
    expect(container.querySelector('[data-fenced]')).toBeNull()
  })

  it('survives a re-render that introduces an inline classed <code> (streaming)', () => {
    // The crash surfaced on reconciliation, not first paint, so the streaming
    // shape is the one that actually reproduced it in the dashboard.
    const { container, rerender } = render(<MarkdownRenderer content="Use " />)
    rerender(<MarkdownRenderer content={'Use <code class="language-js">const x = 1</code>'} />)
    rerender(<MarkdownRenderer content={'Use <code class="language-js">const x = 1</code> in your file.'} />)
    expect(container.querySelectorAll('p div')).toHaveLength(0)
    expect(container.textContent).toContain('in your file.')
  })
})
