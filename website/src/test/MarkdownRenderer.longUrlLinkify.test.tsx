import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer, { fixUnencodedLinkDestinations } from '../components/MarkdownRenderer'

/**
 * Issue #5729: long / query-param-heavy URLs in chat messages must render as a
 * single clickable anchor whose href carries the FULL URL.
 *
 * Two halves:
 *  - Pinning tests: a properly-encoded long URL with many `&`-separated params
 *    already renders as one full-href anchor, bare and as `[text](url)`; a bare
 *    file path still does NOT linkify. These pin the working behaviour so a
 *    future linkify change cannot silently regress it.
 *  - The repair: an agent-emitted `[text](url)` whose destination carries RAW
 *    spaces (the unencoded pre-filled-URL shape the issue reports) is refused
 *    by CommonMark and used to render as truncated plain text. It now parses
 *    with the full URL, whitespace percent-encoded.
 */

// >200 chars, many &-separated params — the shape the issue names.
const LONG_URL =
  'https://github.com/kirodotdev/KiroCrew/issues/new?title=Bug%3A+dashboard+chat+fails&body=%23%23+What%0A%0AURLs+are+not+clickable%0A%0A%23%23+Why%0A%0AThe+agent+generated+a+link&labels=bug%2Carea%3A+dashboard&assignees=someone&template=bug_report.md&milestone=v2.0'

describe('long URLs with & query params linkify with the full href (#5729)', () => {
  it('a bare long URL renders as ONE anchor carrying every query param', () => {
    const { container } = render(<MarkdownRenderer content={`Open ${LONG_URL} to file it`} />)
    const anchors = container.querySelectorAll('a')
    expect(anchors.length).toBe(1)
    expect(anchors[0].getAttribute('href')).toBe(LONG_URL)
    // The visible text is the whole URL too — nothing spilled out as prose.
    expect(anchors[0].textContent).toBe(LONG_URL)
  })

  it('a markdown [text](url) with & params renders with the full href', () => {
    const { container } = render(
      <MarkdownRenderer content={`[file the issue](${LONG_URL}) when ready`} />,
    )
    const anchors = container.querySelectorAll('a')
    expect(anchors.length).toBe(1)
    expect(anchors[0].getAttribute('href')).toBe(LONG_URL)
    expect(anchors[0].textContent).toBe('file the issue')
  })

  it('a bare file path still does NOT linkify', () => {
    const { container } = render(
      <MarkdownRenderer content={'Edit /home/user/project/src/main.py and rerun'} />,
    )
    expect(container.querySelectorAll('a').length).toBe(0)
  })
})

describe('fixUnencodedLinkDestinations — repairs a refused [text](url) (#5729)', () => {
  const SPACED =
    'https://github.com/o/r/issues/new?title=Bug: chat fails&labels=bug, area: dashboard&assignees=someone'
  const ENCODED =
    'https://github.com/o/r/issues/new?title=Bug:%20chat%20fails&labels=bug,%20area:%20dashboard&assignees=someone'

  it('percent-encodes raw spaces so the link parses with the full URL', () => {
    expect(fixUnencodedLinkDestinations(`[file it](${SPACED})`)).toBe(`[file it](${ENCODED})`)
  })

  it('the repaired span renders as one anchor with the full href', () => {
    const { container } = render(<MarkdownRenderer content={`[file it](${SPACED}) now`} />)
    const anchors = container.querySelectorAll('a')
    expect(anchors.length).toBe(1)
    expect(anchors[0].getAttribute('href')).toBe(ENCODED)
    expect(anchors[0].textContent).toBe('file it')
  })

  it('encodes tabs as %09', () => {
    expect(fixUnencodedLinkDestinations('[a](https://x.com/p?q=a\tb&x=1)')).toBe(
      '[a](https://x.com/p?q=a%09b&x=1)',
    )
  })

  it('fixes every broken span in a message, leaving surrounding prose alone', () => {
    expect(
      fixUnencodedLinkDestinations('see [a](https://x.com/p?q=1 2&x=3) and [b](https://y.com/r?s=3 4&y=5).'),
    ).toBe('see [a](https://x.com/p?q=1%202&x=3) and [b](https://y.com/r?s=3%204&y=5).')
  })

  it('leaves a legal quoted-title link alone (remark already accepted it)', () => {
    const src = '[a](https://x.com/p "my title")'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('leaves an angle-bracketed destination alone', () => {
    const src = '[a](<https://x.com/p?q=a b>)'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('leaves inline code alone', () => {
    const src = 'run `[a](https://x.com/p?q=1 2&x=3)` verbatim'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('leaves fenced code alone', () => {
    const src = '```\n[a](https://x.com/p?q=1 2&x=3)\n```'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('leaves an escaped bracket alone — the author wrote prose, not a link', () => {
    const src = '\\[a](https://x.com/p?q=1 2&x=3)'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('leaves an escaped CLOSER alone — no link was ever delimited', () => {
    // `[a\](…)` is a literal `]` to CommonMark: the visible prose must not be
    // mutated by encoding the "destination" of a link that never existed.
    const src = '[a\\](https://x.com/p?q=1 2&x=3)'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('runs before the CJK boundary pass — the repaired link is what CJK judges', () => {
    // A repaired span becomes a real link node, so the CJK pass sees it as
    // remark-owned and leaves its surroundings alone; swapping the order
    // would let the CJK pass judge the BROKEN shape. Pin via the rendered
    // result of a message combining both: a CJK-bracket-wrapped rescued link.
    const { container } = render(
      <MarkdownRenderer content={'（见 [报告](https://x.com/p?q=1 2&x=3)）'} />,
    )
    const anchors = container.querySelectorAll('a')
    expect(anchors.length).toBe(1)
    expect(anchors[0].getAttribute('href')).toBe('https://x.com/p?q=1%202&x=3')
    expect(container.textContent).toContain('（见 报告）')
  })

  it('never touches a non-http(s) destination', () => {
    for (const src of [
      '[a](javascript:alert1 x2)',
      '[a](vscode://file/x b)',
      '[a](file:///etc x)',
    ]) {
      expect(fixUnencodedLinkDestinations(src)).toBe(src)
    }
  })

  it('leaves a destination with parens alone — its extent is ambiguous', () => {
    const src = '[a](https://x.com/p?q=(1 2))'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('does not cross a newline', () => {
    const src = '[a](https://x.com/p?q=1\n2)'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('requires query-continuation evidence — prose after a truncated link is never absorbed', () => {
    // The final chunk must open a new `&name=` param, proving the query spans
    // every space. `?ref=1 for the full list` is a truncated link followed by
    // visible words; absorbing them would delete prose and mint a dead URL.
    for (const src of [
      'See [docs](https://x.com/a for the full list of flags).',
      'Try [this](https://x.com/a and read more) too',
      'See [docs](https://x.com/a?ref=1 for the full list).',
      '[a](https://x.com/p?q=1 2)',
    ]) {
      expect(fixUnencodedLinkDestinations(src)).toBe(src)
    }
  })

  it('leaves an uppercase-scheme destination alone — GFM already autolinks its head', () => {
    // `HTTPS://…` heads become GFM autolink nodes, which the parse gate treats
    // as remark-owned — rescuing them would mean loosening the gate that
    // protects every accepted span. Pin the exclusion.
    const src = '[a](HTTPS://x.com/p?q=1 2&x=3)'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('skips an empty label — the rescued anchor would have no accessible name', () => {
    const src = 'x [](https://x.com/p?q=1 2&x=3) y'
    expect(fixUnencodedLinkDestinations(src)).toBe(src)
  })

  it('rescues an image span the same way — the leading ! sits outside the match', () => {
    expect(fixUnencodedLinkDestinations('![alt](https://x.com/i.png?v=1 2&w=3)')).toBe(
      '![alt](https://x.com/i.png?v=1%202&w=3)',
    )
  })

  it('declines a trailing quoted chunk — title vs query text is undecidable', () => {
    // `"t"` is the author's title in one reading, and query TEXT in
    // `?title=Crash when "Save As"` in the other. A guess corrupts whichever
    // reading the author meant, so the span keeps today's render.
    for (const src of [
      '[a](https://x.com/p?q=1 2 "t")',
      "[a](https://x.com/p?q=1 2 't')",
      '[a](https://x.com/p?title=Crash when "Save As")',
    ]) {
      expect(fixUnencodedLinkDestinations(src)).toBe(src)
    }
  })

  it('stays linear on [-heavy adversarial input (no label rescan)', () => {
    // The regex preflight runs on every streaming reparse even when nothing
    // matches, so ITS cost is the one that must stay linear. The label
    // grammar excludes `[`, so a failed start position dies in O(1) instead
    // of rescanning the rest of the line: with no match anywhere the function
    // returns after the preflight, isolating exactly that path. Output is
    // pinned unchanged, and the shape check is a generous doubling ratio
    // (quadrupling the input: linear predicts ~4x, quadratic ~16x) because
    // shared CI runners are noisy.
    const adversarial = (n: number) => '['.repeat(n) + '](https://x.com/?a 1'
    // The `?`+space shape: `[^\s()?]*` pins the FIRST `?` as the only split
    // point, so a run of `a?` repeats offers no restart positions and a
    // missing `)` fails in O(1) per start instead of O(n) backtracks.
    const qAdversarial = (n: number) =>
      '[x](https://' + 'a?'.repeat(n / 2) + ' z'.repeat(16)
    expect(fixUnencodedLinkDestinations(adversarial(4096))).toBe(adversarial(4096))
    expect(fixUnencodedLinkDestinations(qAdversarial(4096))).toBe(qAdversarial(4096))
    const time = (n: number, gen: (n: number) => string) => {
      const s = gen(n)
      const t0 = performance.now()
      for (let i = 0; i < 50; i++) fixUnencodedLinkDestinations(s)
      return performance.now() - t0
    }
    for (const gen of [adversarial, qAdversarial]) {
      time(32768, gen) // warm-up
      const small = time(32768, gen)
      const big = time(131072, gen)
      expect(big).toBeLessThan(Math.max(small, 1) * 10)
    }
  })

  it('a bare URL with a raw space keeps GFM behaviour — href stops at whitespace', () => {
    // No `](…)` delimiters means no evidence of where the URL ends, so the
    // bare form deliberately keeps the stop-at-whitespace rule every other
    // GFM renderer applies. Only the head is linked.
    const { container } = render(
      <MarkdownRenderer content={'Open https://x.com/p?title=a b&c=d now'} />,
    )
    const anchors = container.querySelectorAll('a')
    expect(anchors.length).toBe(1)
    expect(anchors[0].getAttribute('href')).toBe('https://x.com/p?title=a')
  })

  it('sourcePos mode renders unrewritten (column-accurate) content', () => {
    // The rewrite inserts characters, which would shift `data-sourcepos`
    // columns — so that surface keeps the unfixed render, same as the CJK
    // boundary pass. Assert the POSITIVE unfixed state (the truncated
    // autolink head), not merely the absence of the fixed one, so a crash
    // that renders nothing cannot pass this test.
    const { container } = render(
      <MarkdownRenderer content={`[file it](${SPACED})`} sourcePos />,
    )
    const anchors = container.querySelectorAll('a')
    expect(anchors.length).toBe(1)
    expect(anchors[0].getAttribute('href')).toBe(
      'https://github.com/o/r/issues/new?title=Bug',
    )
  })
})
