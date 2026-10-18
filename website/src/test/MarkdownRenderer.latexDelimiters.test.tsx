/**
 * MarkdownRenderer: LaTeX-native math delimiters. `\[ ... \]` and `\( ... \)` render
 * as KaTeX math, verbatim source (code, link destinations, raw HTML) stays literal,
 * and pathological input is handled in linear time.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

// The same branding stub as MarkdownRenderer.test.tsx: a direct-local session.
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true }),
}))

describe('MarkdownRenderer LaTeX-native delimiters (#7803)', () => {
  it('renders \\[ ... \\] display math via KaTeX', () => {
    const content = '\\[\n\\text{Incremental value} = V(a) - V(b)\n\\]'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('.katex, .katex-display')).not.toBeNull()
    expect(container.textContent).not.toContain('\\[')
  })

  it('renders \\( ... \\) inline math via KaTeX', () => {
    const { container } = render(
      <MarkdownRenderer content={'The value \\(a^2 + b^2\\) grows.'} />
    )
    expect(container.querySelector('.katex')).not.toBeNull()
    expect(container.textContent).toContain('grows.')
  })

  it('does not promote \\[ ... \\] to a display block when an inline sibling shares its line', () => {
    // `**bold**` becomes a `strong` sibling, so the text node's raw slice
    // BEGINS at `\[` and a slice-local "owns its line" check would say yes.
    // The source line does not begin there; the math stays inline.
    const { container } = render(<MarkdownRenderer content={'**bold** \\[ x \\] more'} />)
    expect(container.querySelector('.katex-display')).toBeNull()
    expect(container.querySelectorAll('p').length).toBe(1)
    const p = container.querySelector('p')!
    expect(p.querySelector('strong')).not.toBeNull()
    expect(p.textContent).toContain('more')
    // Mirror image: the sibling AFTER the delimiter shares the line.
    const { container: c2 } = render(<MarkdownRenderer content={'\\[ x \\] **bold**'} />)
    expect(c2.querySelector('.katex-display')).toBeNull()
    expect(c2.querySelectorAll('p').length).toBe(1)
  })

  it('never places a display block inside a heading or table cell', () => {
    // Display math is flow content; a heading cannot be split around it, and
    // `\[ \]` has no inline form -- so it renders as the escaped literal
    // `[ x ]` (the ADF/Jira shape), never as a centered block inside <h2>/<td>.
    const { container } = render(<MarkdownRenderer content={'## \\[ x \\]'} />)
    expect(container.querySelector('.katex-display')).toBeNull()
    expect(container.querySelector('h2')!.textContent).toContain('[ x ]')
    const { container: t } = render(
      <MarkdownRenderer content={'| a |\n| - |\n| \\[ x \\] |'} />
    )
    expect(t.querySelector('.katex-display')).toBeNull()
    expect(t.querySelector('td')!.textContent).toContain('[ x ]')
  })

  it('still lifts a display block that owns its own line after a bold line', () => {
    const { container } = render(
      <MarkdownRenderer content={'**bold**\n\\[\nx\n\\]\nafter'} />
    )
    expect(container.querySelector('.katex-display')).not.toBeNull()
    expect(container.textContent).toContain('after')
  })

  it('keeps the soft line breaks around inline math as prose whitespace', () => {
    // Wrapped prose puts a soft break right before or after inline math. That
    // break is a space to the reader; only a DISPLAY block owns its line.
    const content = 'first\n\\(x\\)\nsecond and \\(y\\)\nthird'
    const { container } = render(<MarkdownRenderer content={content} />)
    const p = container.querySelector('p')!
    expect(p.querySelectorAll('.katex').length).toBe(2)
    const text = Array.from(p.childNodes)
      .filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent)
    // Each prose piece still carries the break on its math-facing side.
    expect(text[0]).toBe('first\n')
    expect(text[1]).toBe('\nsecond and ')
    expect(text[2]).toBe('\nthird')
    // The display trim is unchanged: the line break between prose and a
    // display block is layout and is dropped.
    const { container: display } = render(
      <MarkdownRenderer content={'before\n\\[\nx\n\\]\nafter'} />
    )
    for (const p2 of Array.from(display.querySelectorAll('p'))) {
      expect(p2.textContent).not.toMatch(/^\n|\n$/)
    }
  })

  it('leaves \\[ inside fenced code blocks literal', () => {
    const content = '```sh\ngrep "\\[x\\]" file\n```'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('.katex')).toBeNull()
    expect(container.textContent).toContain('\\[x\\]')
  })

  it('leaves \\[ inside inline code literal', () => {
    const { container } = render(
      <MarkdownRenderer content={'Use `\\[escape\\]` in the pattern.'} />
    )
    expect(container.querySelector('.katex')).toBeNull()
    expect(container.textContent).toContain('\\[escape\\]')
  })

  it('leaves an unmatched \\[ opener alone', () => {
    const { container } = render(<MarkdownRenderer content={'A lone \\[ bracket here'} />)
    expect(container.querySelector('.katex')).toBeNull()
  })

  it('does not defeat markdown bracket-escapes (ADF safety shapes)', () => {
    // Escaped literal brackets HUG their text — converting them to math
    // would defeat the escape the ADF converter emits for security.
    const image = render(<MarkdownRenderer content={'!\\[a\\](http://example.com/i.png)'} />)
    expect(image.container.querySelector('.katex')).toBeNull()
    expect(image.container.textContent).toContain('![a](http://example.com/i.png)')
    const redacted = render(<MarkdownRenderer content={'\\[REDACTED: credential\\]'} />)
    expect(redacted.container.querySelector('.katex')).toBeNull()
    expect(redacted.container.textContent).toContain('[REDACTED: credential]')
  })

  it('converts whitespace-padded single-line display math', () => {
    const { container } = render(<MarkdownRenderer content={'\\[ a^2 + b^2 = c^2 \\]'} />)
    expect(container.querySelector('.katex, .katex-display')).not.toBeNull()
  })

  it('leaves mid-sentence escaped brackets literal even when padded (Jira/ADF shape)', () => {
    const { container } = render(
      <MarkdownRenderer content={'have you seen \\[ x \\] in the board column?'} />
    )
    expect(container.querySelector('.katex')).toBeNull()
    expect(container.textContent).toContain('[ x ]')
  })

  it('does not let an unmatched opener consume a closer inside inline code', () => {
    const { container } = render(
      <MarkdownRenderer content={'A lone \\( here, then code `f\\(x\\)` stays code.'} />
    )
    expect(container.textContent).toContain('f\\(x\\)')
  })

  it('does not treat a non-closing fence-like line as ending code protection', () => {
    const content = '~~~\ncontent\n~~~not-close\n\\[ still in fence \\]\n~~~'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('.katex')).toBeNull()
    expect(container.textContent).toContain('still in fence')
  })

  it('renders math from ELIGIBLE TEXT NODES only: never inside link destinations', () => {
    const content = 'see [the page](https://example.com/wiki/Name_\\(disambiguation\\)) for more'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('.katex')).toBeNull()
    const a = container.querySelector('a')
    expect(a?.getAttribute('href')).toContain('disambiguation')
  })

  it('never rewrites raw HTML attributes (the transform sees an html node, not text)', () => {
    // A source scanner cannot tell an attribute value from prose; the remark
    // transform never visits `html` nodes, so an escaped paren in an href
    // survives to rehype-raw intact instead of becoming `$$`.
    const content = 'Try <a href="https://example.com/a_\\(b\\)">here</a> and \\(x\\) now.'
    const { container } = render(<MarkdownRenderer content={content} />)
    const a = container.querySelector('a')
    expect(a?.getAttribute('href')).toContain('a_\\(b\\)')
    // …while the prose math beside it still converts: the guard is per node.
    expect(container.querySelectorAll('.katex').length).toBe(1)
  })

  it('handles pathological unmatched-opener input in linear time', () => {
    // 50k unmatched openers = 100k chars; a quadratic per-opener rescan would
    // trip vitest's 5s test timeout. This test IS the regression guard.
    const pathological = '\\('.repeat(50000)
    const { container } = render(<MarkdownRenderer content={pathological} />)
    expect(container.querySelector('.katex')).toBeNull()
  })

  // Timing guards below assert RATIOS of medians, never a single sample: one
  // GC pause or a noisy neighbour on a shared runner can double a lone
  // measurement, and the median of three interleaved samples per shape is
  // immune to any one of them. Interleaving (a, b, a, b, ...) means drift
  // during the run lands on both shapes alike.
  const medianRatio = (numerator: () => number, denominator: () => number, samples = 3) => {
    const num: number[] = []
    const den: number[] = []
    for (let i = 0; i < samples; i++) {
      den.push(denominator())
      num.push(numerator())
    }
    const median = (xs: number[]) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)]
    return median(num) / median(den)
  }

  it('handles a flood of empty display pairs in linear time (no per-closer slice)', { timeout: 300000 }, () => {
    // `\[ \] ` x150k: every closer reaches the display-eligibility gate and
    // nothing converts (the body trims empty). The first/last-line checks used
    // to slice the raw text per closer -- O(offset) each, quadratic overall.
    // Measured against an equal-size (900 KB) flood of unmatched openers, which
    // is known-linear and pays the same remark parse: ratio 2.55 on the
    // previous head, 1.01 now. A RATIO is asserted, not a wall-clock bound, so
    // the guard reads the same on a slow CI runner as on a fast workstation.
    // V8's SIMD `includes` hides the quadratic term below ~100k chars, so the
    // size is chosen where the shapes separate; this is a gross-regression
    // guard, not a proof of linearity. Each shape is rendered three times and
    // the medians are compared (see medianRatio), which is why the timeout is
    // generous: six 900 KB renders, not two.
    const time = (s: string) => {
      const t0 = performance.now()
      render(<MarkdownRenderer content={s} />)
      return performance.now() - t0
    }
    const ratio = medianRatio(
      () => time('\\[ \\] '.repeat(150000)),
      () => time('\\[ '.repeat(300000)),
    )
    // flake-ok: pre-existing ratio-of-medians timing guard, not a wall-clock bound; moved verbatim and tracked for a de-flake pass
    expect(ratio).toBeLessThan(1.8)
  })

  it('pairs thousands of unclosed verbatim openers in linear time (one pass, not a scan per opener)', () => {
    // 6000 unclosed `<customblock>` openers before one math delimiter. Both the
    // renderer's verbatim-unknown-tags pass and the plugin's verbatim context
    // used to run a full-suffix close scan per opener -- O(n²): 3x the input
    // cost 7.5x the time. With the shared single-pass pairing map it is linear.
    //
    // The guard compares the opener flood against an EQUAL-BYTE-SIZE flood of a
    // plain word with no opener to pair, exactly as the empty-display-pair test
    // above does: both inputs are the same length and pay the same remark parse,
    // so machine speed and shard load scale BOTH measurements together and
    // cancel in the ratio. What is left is the per-opener work. The current
    // single-pass pairing keeps that at a stable ~3x of the no-opener baseline
    // (both floods are 84 KB); a per-opener close scan would make only the
    // opener side quadratic -- the no-opener baseline can never go quadratic --
    // pushing the ratio into the tens. Asserting a ratio of equal-size inputs,
    // not a size-scaling ratio and not a wall-clock bound, is why the earlier
    // 6000-vs-2000 form read 5.24 against a limit of 5 on a loaded shard: at
    // only 3x input the renderer's superlinear remark cost already drifts the
    // "linear" ratio close to the limit, with no load headroom (issue #18623).
    // '<customblock> ' and the baseline word are both 14 chars so the two
    // floods match byte for byte. A warm-up render absorbs JIT and module-load
    // cost; the ratio is of medians over three interleaved samples per shape.
    const openerFlood = (n: number) => '<customblock> '.repeat(n) + '\\(x\\)'
    const plainFlood = (n: number) => 'plainwordxyz. '.repeat(n) + '\\(x\\)'
    const time = (s: string) => {
      const t0 = performance.now()
      render(<MarkdownRenderer content={s} />)
      return performance.now() - t0
    }
    time(openerFlood(6000))
    const ratio = medianRatio(
      () => time(openerFlood(6000)),
      () => time(plainFlood(6000)),
    )
    // flake-ok: pre-existing ratio-of-medians timing guard, not a wall-clock bound; moved verbatim and tracked for a de-flake pass
    expect(ratio).toBeLessThan(4.5)
    const { container } = render(<MarkdownRenderer content={openerFlood(6000)} />)
    // Unclosed openers are lone tags shown as source; the math after them is prose math.
    expect(container.querySelectorAll('.katex').length).toBe(1)
    expect(container.textContent).toContain('<customblock>')
  })

  it('handles a pathological backslash run in linear time (escape parity is tracked forward)', () => {
    // 100k backslashes ending in `\(`: a per-position backward run scan would
    // be O(n²) (~5e9 steps) and trip the test timeout; the forward parity
    // counter makes it one visit per character. Even-length run → the final
    // `\(` is an escaped backslash + a plain paren, so nothing converts.
    const run = '\\'.repeat(100000)
    const { container } = render(<MarkdownRenderer content={run + '( x \\) end'} />)
    expect(container.querySelector('.katex')).toBeNull()
    // Odd-length run → a real opener; it pairs and converts.
    const odd = render(<MarkdownRenderer content={'\\'.repeat(100001) + '( x \\) end'} />)
    expect(odd.container.querySelector('.katex')).not.toBeNull()
  })

  it('handles pathological repeated ]( junk in linear time', () => {
    // The transform's own cost on `](` junk is one visit per character. The
    // PARSE that precedes it is remark's and is super-linear on this input
    // (measured ~7s/100kB on a laptop, worse on CI), so the size here is what
    // exercises the transform without paying for remark's parse: 2k junk
    // tokens, both maths convert, and the render stays well inside the
    // timeout. The transform-level linearity is pinned by the backslash-run
    // test above, which does not go through remark's slow path.
    const junk = ']('.repeat(2000)
    const { container } = render(<MarkdownRenderer content={junk + ' \\( x \\)\n\n\\( y \\)'} />)
    expect(container.querySelectorAll('.katex').length).toBe(2)
  })

  it('leaves math delimiters inside indented code blocks literal', () => {
    // A 4-space-indented code block is a `code` node to the parser even
    // with no fence; the transform never visits it.
    const content = 'Example:\n\n    result = \\(x\\) + 1\n\nDone.'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('.katex')).toBeNull()
    expect(container.textContent).toContain('\\(x\\)')
  })

  it('leaves double-escaped delimiters literal (escape parity)', () => {
    // `\\(x\\)` is an escaped backslash followed by a plain paren — the shape
    // Jira's inline escaper emits for a literal backslash-paren.
    const literal = render(<MarkdownRenderer content={'literal \\\\(x\\\\) stays'} />)
    expect(literal.container.querySelector('.katex')).toBeNull()
    expect(literal.container.textContent).toContain('\\(x\\)')
    // Parity, not blanket suppression: `\\\(` is an escaped backslash THEN a
    // real opener, so a triple-backslash pair still converts.
    const triple = render(<MarkdownRenderer content={'\\\\\\( x \\\\\\)'} />)
    expect(triple.container.querySelector('.katex')).not.toBeNull()
  })

  it('leaves math delimiters inside blockquoted fenced code literal', () => {
    const content = '> ```\n> \\(x\\)\n> ```\n\n\\( y \\)'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.textContent).toContain('\\(x\\)') // fenced inside blockquote: untouched
    expect(container.querySelectorAll('.katex').length).toBe(1) // prose after: converts
  })

  it('leaves reference-link definition destinations untouched', () => {
    // `[label]: url` is a `definition` node whose destination is a URL —
    // escaped parens there are literal path characters, never math.
    const content = '[fn]: https://example.test/Name_\\(detail\\)\n\n[link][fn] and \\( y \\)'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('a')?.getAttribute('href')).toContain('Name_')
    expect(container.querySelectorAll('.katex').length).toBe(1)
  })

  it('splits a paragraph around display math so the block is valid flow content', () => {
    const content = 'Before\n\\[\na = b\n\\]\nAfter'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('.katex-display')).not.toBeNull()
    // Neither the display block nor its <pre> lives inside a <p>.
    expect(container.querySelector('p .katex-display, p pre')).toBeNull()
    expect(container.textContent).toContain('Before')
    expect(container.textContent).toContain('After')
  })

  it('keeps blockquote prose intact around math (prose comes from the parsed value, not a raw slice)', () => {
    // Inside a blockquote the text node's raw source spans the interior `> `
    // continuation markers. Prose must come from remark's decoded value; only
    // the math span is read from raw (so its backslashes survive).
    const content = '> first line \\(a\\) more\n> second line \\(b\\) done'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelectorAll('.katex').length).toBe(2)
    const text = container.querySelector('blockquote')?.textContent ?? ''
    expect(text).not.toContain('>')
    expect(text).toContain('first line')
    expect(text).toContain('second line')
    expect(text).toContain('done')
  })

  it('keeps an entity it does not know by name exactly as remark decoded it', () => {
    // `&Omega;` is not in the transform's small named table. Prose must still
    // read "Ω" because it comes from the parser's value, not a re-decode.
    const { container } = render(<MarkdownRenderer content={'Resistance in &Omega; is \\(R\\) here'} />)
    expect(container.querySelector('.katex')).not.toBeNull()
    expect(container.textContent).toContain('Ω')
    expect(container.textContent).not.toContain('&Omega;')
  })

  it('leaves text inside paired raw <code> HTML verbatim', () => {
    // Raw inline `<code>…</code>` parses as two `html` nodes around a plain
    // `text` node; that text is verbatim even though remark typed it text.
    const content = 'Use <code>f\\(x\\)</code> literally, but \\(y\\) renders.'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelectorAll('.katex').length).toBe(1)
    // remark itself decodes the escapes inside the raw element's text (that is
    // CommonMark); what must NOT happen is a KaTeX node inside the <code>.
    const code = container.querySelector('code')
    expect(code?.textContent).toBe('f(x)')
    expect(code?.querySelector('.katex')).toBeNull()
  })

  it('gives synthesized paragraphs and display math real source positions (sourcePos mode)', () => {
    const content = 'Line one\n\\[\na = b\n\\]\nLine five'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const ps = Array.from(container.querySelectorAll('p'))
    expect(ps.length).toBe(2)
    // The paragraph AFTER the display block must anchor to line 5, not be
    // position-less (which rehypeSourcepos would skip, mis-anchoring comments).
    const after = ps.find((p) => p.textContent?.includes('Line five'))
    expect(after?.getAttribute('data-sourcepos')).toMatch(/^5:1-5:/)
    const before = ps.find((p) => p.textContent?.includes('Line one'))
    expect(before?.getAttribute('data-sourcepos')).toMatch(/^1:1-1:/)
    // The display block itself carries no data-sourcepos — rehype-katex replaces
    // the positioned element — exactly as remark-math's own `$$` blocks behave;
    // the mdast `math` node does carry the 2:1-4:3 span for any consumer that
    // reads positions before KaTeX runs.
    expect(container.querySelector('.katex-display')).not.toBeNull()
  })

  it('handles an ampersand flood beside math in linear time (the reference scan is bounded)', () => {
    // Gross-regression guard only: V8's vectorised indexOf hides the old
    // quadratic below ~1M chars, so a CI-sized input cannot distinguish the
    // bounded scan from the unbounded one by timing (measured 3x at 250k).
    // The bound is what the code review verifies; this asserts no blow-up.
    // The measure is a ratio of medians against the same flood with the math
    // replaced by plain parens, which pays the same remark parse and none of
    // the reference scan, so it reads the same on any runner: ~1.1 today.
    const amp = '&'.repeat(100_000)
    const time = (s: string) => {
      const t0 = performance.now()
      render(<MarkdownRenderer content={s} />)
      return performance.now() - t0
    }
    const ratio = medianRatio(
      () => time('x \\(a\\) ' + amp),
      () => time('x (a) ' + amp),
    )
    // flake-ok: pre-existing ratio-of-medians timing guard, not a wall-clock bound; moved verbatim and tracked for a de-flake pass
    expect(ratio).toBeLessThan(3)
    const { container } = render(<MarkdownRenderer content={'x \\(a\\) ' + amp} />)
    expect(container.querySelector('.katex')).not.toBeNull()
    expect(container.textContent).toContain('&'.repeat(100))
  })

  it('leaves text inside an unknown paired container as source, like the tags around it', () => {
    // The verbatim-unknown-tags pass shows `<customBlock>…</customBlock>` as
    // literal source; a KaTeX span in the middle of that source would be wrong.
    const { container } = render(<MarkdownRenderer content={'<customBlock>\\(x\\)</customBlock> then \\(y\\)'} />)
    expect(container.querySelectorAll('.katex').length).toBe(1)
    expect(container.textContent).toContain('<customBlock>')
  })

  it('agrees with the verbatim pass on a tag whose quoted attribute contains ">"', () => {
    // Both passes now share one tag grammar (htmlTagGrammar). A `>` inside a
    // quoted attribute value is part of the tag to the renderer's verbatim pass,
    // so it must open the plugin's verbatim context too -- otherwise the source
    // the renderer shows literally would carry a KaTeX span in its middle.
    const { container } = render(
      <MarkdownRenderer content={'<customBlock title="a>b">\\(x\\)</customBlock> then \\(y\\)'} />
    )
    expect(container.querySelectorAll('.katex').length).toBe(1)
    expect(container.textContent).toContain('title="a>b"')
    // The inner math stayed source (remark decodes the `\(` escape in text).
    expect(container.textContent).toContain('>(x)<')
  })

  it('keeps the verbatim context correct after an earlier text node in the paragraph was rewritten', () => {
    // The first container builds the pairing map on the ORIGINAL indices. The
    // text between the containers is rewritten into three nodes, shifting the
    // second container by two. A map consulted after that splice has no entry
    // at the shifted index, so no verbatim context opens and the math inside
    // `<customBlock>` converts. Decisions are made on a frozen snapshot now.
    const content = '<code>\\(a\\)</code> then \\(x\\) then <customBlock>\\(z\\)</customBlock>'
    const { container } = render(<MarkdownRenderer content={content} />)
    // Only x renders; a (in <code>) and z (in the unknown container) stay source.
    expect(container.querySelectorAll('.katex').length).toBe(1)
    expect(container.textContent).toContain('<customBlock>(z)</customBlock>')
  })

  it('still converts math inside an allowlisted paired prose tag', () => {
    const { container } = render(<MarkdownRenderer content={'<b>bold \\(x\\) here</b>'} />)
    expect(container.querySelectorAll('.katex').length).toBe(1)
  })

  it('treats text after an UNCLOSED unknown tag as prose', () => {
    const { container } = render(<MarkdownRenderer content={'<customBlock> lone tag then \\(x\\)'} />)
    expect(container.querySelectorAll('.katex').length).toBe(1)
  })

  it('keeps character references in prose next to converted math', () => {
    const { container } = render(<MarkdownRenderer content={'Tom &amp; Jerry \\(x\\) &copy; 2026'} />)
    expect(container.querySelector('.katex')).not.toBeNull()
    expect(container.textContent).toContain('Tom & Jerry')
    expect(container.textContent).toContain('© 2026')
  })
})
