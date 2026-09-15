// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { MAX_INDENT_COLS, MAX_TREE_DEPTH, capWhitespaceRuns, remarkBoundDepth } from '../utils/markdownDepthBound'

// A chat message is input-controlled markdown. Deeply nested constructs parse
// into a tree whose depth equals the nesting count, and the recursive layers
// downstream of the parser -- remark-gfm's post-parse transform, remark-rehype,
// rehype-raw's hast conversion, react-markdown, this module's walkers -- then
// recurse to that depth. Past the engine's call-stack limit a single message
// throws `RangeError: Maximum call stack size exceeded`.
//
// These tests pin the guarantee that depth is bounded ON THE PARSED TREE, using
// the real parsers as the oracle: rendering completes, content survives, and
// below the bound nothing changes. Every vector here crashed the unbounded
// pipeline; the spellings are the ones review found against the text-rewriting
// approach this replaces, so a regression to grammar-modeling would show here.
//
// Fixture sizes: every vector is far past where the unbounded pipeline throws
// (a few thousand levels), and far under what the full concurrent suite can
// parse inside CI's 15s per-test budget -- micromark's per-line container
// scan makes a 50k blockquote parse alone ~12s, ~5x slower under suite load.

const DEEP = 3_000

function renders(content: string): HTMLElement {
  const { container } = render(<MarkdownRenderer content={content} />)
  return container
}

describe('markdown depth bound: CommonMark containers', () => {
  it('renders nesting below the bound as real structure', () => {
    const depth = Math.floor(MAX_TREE_DEPTH / 2)
    const c = renders('>'.repeat(depth) + ' payload-text')
    expect(c.textContent).toContain('payload-text')
    expect(c.querySelectorAll('blockquote').length).toBeGreaterThanOrEqual(depth)
  })

  it('survives a 10,000-deep blockquote run', () => {
    expect(renders('>'.repeat(10_000) + ' payload-text').textContent).toContain('payload-text')
  })

  it('survives interleaved quote/list containers (`> - > - …`)', () => {
    expect(renders('> - '.repeat(1_500) + 'payload-text').textContent).toContain('payload-text')
  })

  it('survives blockquotes re-opened through the 0-3 space indent (`>  >  > …`)', () => {
    expect(renders('>  '.repeat(DEEP) + 'payload-text').textContent).toContain('payload-text')
  })

  // Deep lists: the tree bound keeps these from overflowing; the whitespace
  // cap keeps them cheap (micromark's per-line container scan is O(depth):
  // 600 uncapped levels parse in ~13s, 1,200 in minutes). The cap's own
  // contract is pinned by the assertion-level tests below, not by timing, so
  // these fixtures are sized for the concurrent CI suite (~5x slower).
  it('survives a deep nested list', () => {
    const lines: string[] = []
    for (let i = 0; i < 400; i++) lines.push(' '.repeat(i * 2) + '- item')
    expect(renders(lines.join('\n')).textContent).toContain('item')
  })

  it('survives a deep nested list of marker-only (empty) items', () => {
    const lines: string[] = []
    for (let i = 0; i < 400; i++) lines.push(' '.repeat(i * 2) + '-')
    expect(renders(lines.join('\n')).querySelector('li')).not.toBeNull()
  })

  it('keeps an image\'s alt text when its subtree is folded', () => {
    // An image node carries its words in `alt`, not `value`; folding must not
    // drop them.
    const c = renders('>'.repeat(MAX_TREE_DEPTH + 5) + ' ![alt-words-survive](https://example.com/x.png)')
    expect(c.textContent).toContain('alt-words-survive')
  })

  it('keeps the words of a folded subtree on screen', () => {
    const c = renders('>'.repeat(DEEP) + ' deep-words-survive')
    expect(c.textContent).toContain('deep-words-survive')
  })
})

describe('markdown depth bound: raw HTML through rehype-raw', () => {
  it('survives a deep <div> run as an HTML block', () => {
    expect(renders('<div>\n' + '<div>'.repeat(DEEP) + 'payload-text').textContent).toContain('payload-text')
  })

  it('survives a deep <div> run spread across paragraphs', () => {
    expect(renders('x <div>\n\n'.repeat(3_000) + 'payload-text').textContent).toContain('payload-text')
  })

  it('survives parse5-shaped tag names and tokenizer whitespace (`<x:y>`, `<span\\f>`, NUL)', () => {
    for (const tag of ['<x:y>', '<span\f>', '<span\0>']) {
      expect(renders('<div>\n' + tag.repeat(DEEP) + 'payload-text').textContent).toContain('payload-text')
    }
  })

  it('survives HTML-only void elements nested inside foreign content (`<svg>` + `<base>`)', () => {
    expect(renders('<svg>\n' + '<base>'.repeat(DEEP) + 'payload-text').textContent).toContain('payload-text')
    expect(renders('x <svg>' + '<base>'.repeat(DEEP) + 'payload-text').textContent).toContain('payload-text')
  })

  it('survives a deep run inside <noscript> (oracle must parse with scriptingEnabled: false)', () => {
    // hast-util-raw parses with scripting DISABLED, under which <noscript>
    // content is real markup that nests. parse5's default is scripting ENABLED,
    // where the same content is flat RAWTEXT. An oracle run under the default
    // would read depth 2 here while the real parse builds 5,001.
    expect(renders('<noscript>\n' + '<div>'.repeat(DEEP) + 'payload-text').textContent).toContain('payload-text')
    expect(renders('x <noscript>' + '<div>'.repeat(DEEP) + 'payload-text').textContent).toContain('payload-text')
  })

  it('survives a deep run inside <template> (children live on `content`, not `childNodes`)', () => {
    // parse5 parks template children on a separate `content` fragment, and
    // hast-util-from-parse5 recurses into it, so the oracle must count it.
    expect(renders('<template>\n' + '<div>'.repeat(DEEP) + 'payload-text').textContent).toBeDefined()
    expect(renders('x <template>' + '<div>'.repeat(DEEP) + '</template>payload-text').textContent).toContain('payload-text')
  })

  it('survives fake closes inside comments, CDATA, attribute values and code spans', () => {
    for (const unit of ['<div><!-- </div> -->', '<div><![CDATA[ </div> ]]>', '<div data-x="</div>">', '<div>`</div>`']) {
      expect(renders('x ' + unit.repeat(DEEP) + 'payload-text').textContent).toContain('payload-text')
    }
  })

  it('survives a fence-repair reordering vector (indented open + glued closer)', () => {
    expect(renders('   ```\n   ```X\n' + '>'.repeat(10_000) + ' payload-text').textContent).toContain('payload-text')
  })

  it('leaves ordinary raw HTML below the bound rendered as HTML', () => {
    const c = renders('<div class="x"><b>bold</b> and <i>italic</i></div>')
    expect(c.querySelector('b')?.textContent).toBe('bold')
    expect(c.querySelector('i')?.textContent).toBe('italic')
  })
})

describe('markdown depth bound: plugin order contract', () => {
  it('bounds depth inside parse(), ahead of remark-gfm', () => {
    // remark-gfm's autolink-literal transform runs INSIDE parse() and recurses
    // over the tree. The bound has to run before it, through the same hook --
    // a unified transformer would be too late. If this order is broken, the
    // 15,000-deep parse below throws before any transformer runs.
    const proc = unified().use(remarkParse).use(remarkBoundDepth).use(remarkGfm)
    const tree = proc.parse('>'.repeat(15_000) + ' p')
    let depth = 0
    const stack: Array<[{ children?: unknown[] }, number]> = [[tree, 0]]
    while (stack.length) {
      const [n, d] = stack.pop()!
      if (d > depth) depth = d
      for (const k of n.children ?? []) stack.push([k as { children?: unknown[] }, d + 1])
    }
    expect(depth).toBeLessThanOrEqual(MAX_TREE_DEPTH)
  })

  it('a plain unified transformer would be too late (documents why the hook is used)', () => {
    // Registered as a transformer instead of through fromMarkdownExtensions,
    // the same walk never runs: remark-gfm's in-parse transform throws first.
    const late = unified().use(remarkParse).use(remarkGfm)
    expect(() => late.parse('>'.repeat(15_000) + ' p')).toThrow(RangeError)
  })
})

describe('markdown depth bound: whitespace-run cap', () => {
  it('caps a run after a blockquote prefix, which indents a list just as a leading run does', () => {
    // The space in `> ` joins the run, so the whole run collapses to the cap.
    const src = '> ' + ' '.repeat(MAX_INDENT_COLS + 40) + '- deep'
    expect(capWhitespaceRuns(src)).toBe('>' + ' '.repeat(MAX_INDENT_COLS) + '- deep')
  })

  it('survives a deep list indented behind a blockquote prefix', () => {
    const lines: string[] = []
    for (let i = 0; i < 400; i++) lines.push('> ' + ' '.repeat(i * 2) + '- item')
    expect(renders(lines.join('\n')).textContent).toContain('item')
  })

  it('is an identity when no run exceeds the cap', () => {
    const src = 'a\n' + ' '.repeat(MAX_INDENT_COLS) + '- deep\n\t\tcode\n```\n' + ' '.repeat(200) + 'art  ' + ' '.repeat(100) + 'x\n```'
    expect(capWhitespaceRuns(src)).toBe(src)
  })

  it('truncates only the leading whitespace past the cap, and only on that line', () => {
    const src = 'a\n' + ' '.repeat(MAX_INDENT_COLS + 40) + '- deep\nb'
    expect(capWhitespaceRuns(src)).toBe('a\n' + ' '.repeat(MAX_INDENT_COLS) + '- deep\nb')
  })

  it('counts a tab as advancing to the next multiple of four columns', () => {
    const tabs = '\t'.repeat(MAX_INDENT_COLS / 4 + 1) + 'x'
    expect(capWhitespaceRuns('p\n' + tabs)).toBe('p\n' + ' '.repeat(MAX_INDENT_COLS) + 'x')
  })
})

describe('markdown depth bound: below the bound nothing changes', () => {
  it('ordinary content renders identically with the bound in the pipeline', () => {
    const src = '# hi\n\n> quote\n\n- a\n  - b\n    - c\n\n<div class="x"><b>bold</b></div>\n\n`code` and https://example.com'
    const c = renders(src)
    expect(c.querySelector('h1')?.textContent).toBe('hi')
    expect(c.querySelector('blockquote')?.textContent).toContain('quote')
    expect(c.querySelectorAll('li').length).toBe(3)
    expect(c.querySelector('b')?.textContent).toBe('bold')
    expect(c.querySelector('code')?.textContent).toBe('code')
    expect(c.querySelector('a')?.getAttribute('href')).toBe('https://example.com')
  })

  it('sourcePos coordinates are unaffected (no text is rewritten)', () => {
    const { container } = render(<MarkdownRenderer content={'para one\n\n> quoted'} sourcePos />)
    const bq = container.querySelector('blockquote')
    expect(bq?.getAttribute('data-sourcepos')).toMatch(/^3:/)
  })

  it('sourcePos mode keeps columns exact past a wide whitespace run (cap gated off)', () => {
    // `data-sourcepos` maps a selection back to source coordinates; the cap
    // would shift every column after a >256-col run, so in this mode it does
    // not run -- the same rule the other column-shifting passes follow.
    const line = 'a' + ' '.repeat(MAX_INDENT_COLS + 44) + 'b'
    const { container } = render(<MarkdownRenderer content={line + '\n\nnext'} sourcePos />)
    const p = container.querySelector('p')
    const end = Number(p?.getAttribute('data-sourcepos')?.split('-')[1]?.split(':')[1])
    expect(end).toBeGreaterThan(MAX_INDENT_COLS + 44)
  })
})
