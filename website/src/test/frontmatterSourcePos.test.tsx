// @vitest-environment happy-dom
//
// Regression for the blocking review finding on #22990: the file preview renders
// the frontmatter-stripped body with `sourcePos`, but the inline-comment
// resolver (`resolveSourcePos`) indexes the FULL document. If the body is not
// padded for the stripped block, every `data-sourcepos` line is short by the
// block's height, so a comment on body text anchors to the wrong source line —
// and a heading that repeats the frontmatter `title` resolves INTO the
// frontmatter. This test renders the real MarkdownRenderer over
// `splitFrontmatter(doc).body` and asserts the resolver lands on the true line,
// then proves the test is non-vacuous by showing the UNPADDED body lands on the
// wrong line.
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { resolveSourcePos } from '../components/markdownPanelSourcePos'
import { splitFrontmatter } from '../components/markdown/frontmatter'

// An OKF-style document whose body heading repeats the frontmatter `title`, the
// exact shape the reviewers called out: without the pad, the heading's selection
// can resolve into the `title:` line of the frontmatter.
const DOC = ['---', 'title: Hello', 'tags: [a, b]', '---', '# Hello', '', 'Body paragraph.'].join('\n')

/** Render a markdown string with source positions and return the element the
 *  preview emits for the first `<h1>`, plus a range over its text. */
function renderAndRangeH1(body: string): { root: HTMLElement; range: Range } {
  const { container } = render(<MarkdownRenderer content={body} sourcePos />)
  const h1 = container.querySelector('h1')
  if (!h1) throw new Error('no h1 rendered')
  const walker = document.createTreeWalker(h1, NodeFilter.SHOW_TEXT)
  const text = walker.nextNode() as Text
  const range = document.createRange()
  range.setStart(text, 0)
  range.setEnd(text, Math.min(1, text.data.length))
  return { root: container as HTMLElement, range }
}

describe('frontmatter preview preserves source-line mapping', () => {
  it('splits the body with a pad that keeps the heading on its true source line', () => {
    const r = splitFrontmatter(DOC)
    expect(r.hasFrontmatter).toBe(true)
    // `# Hello` is line 5 of the full document (1-based). The padded body must
    // keep it on line 5 so a resolver that indexes the full doc is correct.
    expect(r.body.split('\n')[4]).toBe('# Hello')
  })

  it('resolves a body heading to its TRUE source line through the real renderer', () => {
    const r = splitFrontmatter(DOC)
    const { root, range } = renderAndRangeH1(r.body)
    const coords = resolveSourcePos(range, root, DOC)
    // The heading is on line 5 of DOC; the resolver must say line 5, not line 2
    // (the `title: Hello` frontmatter line that repeats the same text).
    expect(coords).toBeDefined()
    expect(coords!.line).toBe(5)
  })

  it('is non-vacuous: an UNPADDED body does NOT resolve the heading to its true line', () => {
    // Reproduce the pre-fix behaviour by stripping the pad off the body. The
    // same heading now renders at body line 1, so `data-sourcepos` says line 1
    // while the resolver indexes the full DOC. It cannot align "Hello" against
    // the opening fence on DOC line 1, so it either returns a non-line-5
    // coordinate or undefined — and MarkdownPanel's `findCoords` fallback then
    // lands on "Hello" in the `title:` frontmatter line (line 2). Either way the
    // heading is NOT anchored to its true line 5, which is exactly the defect.
    const r = splitFrontmatter(DOC)
    const unpadded = r.body.replace(/^\n+/, '')
    const { root, range } = renderAndRangeH1(unpadded)
    const resolved = resolveSourcePos(range, root, DOC)
    // Model MarkdownPanel.tsx's resolve-then-fallback chain.
    const idx = DOC.indexOf('Hello')
    const fallbackLine = DOC.slice(0, idx).split('\n').length // 1-based
    const effectiveLine = resolved?.line ?? fallbackLine
    expect(effectiveLine).not.toBe(5)
  })
})
