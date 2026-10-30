import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { THROTTLE_MS } from '../hooks/useBlockAssembler'

/**
 * While streaming, the assembler renders the open markdown block as separate
 * paragraph pieces (splitMarkdownBlocks). Each piece must parse to the same tree it
 * has inside the whole block, so a settled streaming render matches the
 * finished, unsplit render node for node. `glow` and `smooth` are off, so the
 * streaming render adds no caret or reveal markup of its own.
 */
beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

/** Run the assembler's pending streaming parse, so the DOM shows its result. */
const settle = async () => { await act(async () => { vi.advanceTimersByTime(THROTTLE_MS) }) }

const CORPUS: Record<string, string> = {
  paragraphs: 'First paragraph with **bold** and `code`.\n\nSecond one, a [link](https://example.com).\n\n\nThird after two blank lines.',
  headings: '# Title\n\nIntro.\n\nSetext heading\n===\n\n## Sub\n\n---\n\nAfter a rule.',
  tightList: 'Steps:\n\n- one\n- two\n- three\n\nDone.',
  looseList: '- one\n\n- two\n\n  continued two\n\n- three\n\nAfter.',
  orderedStart: 'Intro.\n\n3. three\n4. four\n\nThen.\n\n1) a\n2) b',
  nestedList: '- outer\n  - inner\n\n    inner paragraph\n\n- outer two\n\nTail.',
  blockquotes: '> quoted one\n\n> quoted two\n\nPlain.',
  table: 'Before.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nAfter the table.',
  indentedCode: 'Para.\n\n    indented code\n\n    still code\n\nBack to prose.',
  html: 'Para.\n\n<details>\n<summary>More</summary>\n\nHidden paragraph.\n\n</details>\n\nAfter.',
  references: 'See [the docs][docs].\n\nMore text.\n\n[docs]: https://example.com/docs',
  footnotes: 'A claim[^1].\n\nMore text.\n\n[^1]: The source.',
  math: 'Inline $x$.\n\n$$\na = b\n\nc = d\n$$\n\nAfter.',
  tildeFence: 'Para.\n\n~~~\ncode\n\nmore code\n~~~\n\nAfter.',
  lazyOrdered: '1. First step\ncontinued text\n\n1. Second step\n\nDone.',
  lazyLoose: '1. **Step**\nexplanation\n\n2. **Step**\nexplanation\n\nDone',
  nestedReference: 'See [the docs][ref].\n\n- item\n\n    [ref]: https://example.com/docs',
  inlineHtmlOpener: 'Intro <details><summary>More</summary>\n\nHidden paragraph.\n\n</details>\n\nAfter.',
  indentedListOpener: ' 1. First step\n\n1. Second step\n\nDone.',
  multilineRefLabel: 'See [docs][long label].\n\nMore.\n\n[long\nlabel]: https://example.com',
  cdata: 'Intro.\n\n<![CDATA[\n\nHidden text\n\n]]>\n\nAfter.',
  latexBracket: 'Intro.\n\n\\[\na = b\n\nc = d\n\\]\n\nAfter.',
  lessThan: 'If a < b then\n\nthe second paragraph.',
  diffPathHint: 'Modified /tmp/report.py\n\n\nHere is the change:\n\n```diff\n@@ -1 +1 @@\n-a = 1\n+a = 2\n```',
  autolink: 'See <https://example.com>.\n\nNext paragraph.',
  mixedFence: 'Intro.\n\n```js\nconst a = 1\n```\n\nMiddle one.\n\nMiddle two.\n\n- x\n- y\n\nEnd.',
}

/** The settled streaming render, then the same mount once the stream ends. */
async function streamedHtml(content: string): Promise<[string, string]> {
  const { container, rerender, unmount } = render(<MarkdownRenderer content={content} streaming />)
  await settle()
  const live = normalize(container.innerHTML)
  rerender(<MarkdownRenderer content={content} />)
  const ended = normalize(container.innerHTML)
  unmount()
  return [live, ended]
}

function finalHtml(content: string): string {
  const { container, unmount } = render(<MarkdownRenderer content={content} />)
  const html = normalize(container.innerHTML)
  unmount()
  return html
}

/** React `useId` values count mounts, so two renders never share them. The
 *  whole block also keeps the `\n` text nodes between its root-level blocks,
 *  which a piece boundary drops; whitespace between block boxes lays out to
 *  nothing, so it is removed from both sides. */
const normalize = (html: string) => html.replace(/:r[0-9a-z]+:/g, ':r:').replace(/>\s+</g, '><')

describe('streaming paragraph split renders the same tree as the whole block', () => {
  for (const [name, content] of Object.entries(CORPUS)) {
    it(name, async () => {
      const [live, ended] = await streamedHtml(content)
      const final = finalHtml(content)
      expect(live, 'while streaming').toBe(final)
      expect(ended, 'after the stream ends in the same view').toBe(final)
    })
  }

  it('keeps a reply with a redaction record whole, so its card ids match the reloaded render', async () => {
    // A redaction card id names the rendered block holding its chip; an allowed
    // host's card reopens by that id after the reply reloads unsplit.
    const content = 'Intro.\n\nThe page: [REDACTED: suspicious URL to reviews.corp.example]\n\nAfter.'
    const records = [{ domain: 'reviews.corp.example', rule: 'exfil_query_length', path: '/reviews', query_chars: 290, url: 'https://reviews.corp.example/reviews?filter=abc', url_withheld: null }]
    const { container, rerender, unmount } = render(<MarkdownRenderer content={content} streaming blockedLinks={records} slotKey="s1" />)
    await settle()
    rerender(<MarkdownRenderer content={content} blockedLinks={records} slotKey="s1" />)
    const ended = normalize(container.innerHTML)
    unmount()
    const fresh = render(<MarkdownRenderer content={content} blockedLinks={records} slotKey="s1" />)
    expect(ended).toBe(normalize(fresh.container.innerHTML))
    fresh.unmount()
  })

  it('keeps a settled paragraph node while the tail streams on', async () => {
    const head = 'Settled paragraph.\n\nGrowing'
    const { container, rerender } = render(<MarkdownRenderer content={head} streaming />)
    await settle()
    const settled = Array.from(container.querySelectorAll('p')).find((p) => p.textContent === 'Settled paragraph.')
    expect(settled).toBeTruthy()
    rerender(<MarkdownRenderer content={`${head} tail.\n\nAnother paragraph`} streaming />)
    await settle()
    // The re-parse after the rerender has landed: the new paragraph is its own block.
    expect(Array.from(container.querySelectorAll('p')).map((p) => p.textContent)).toEqual([
      'Settled paragraph.', 'Growing tail.', 'Another paragraph',
    ])
    expect(Array.from(container.querySelectorAll('p')).find((p) => p.textContent === 'Settled paragraph.')).toBe(settled)
    const later = Array.from(container.querySelectorAll('p')).find((p) => p.textContent === 'Growing tail.')
    // A fence opening after the prose leaves every piece mounted.
    rerender(<MarkdownRenderer content={`${head} tail.\n\nAnother paragraph.\n\n\`\`\`js\nconst x = 1`} streaming />)
    await settle()
    expect(Array.from(container.querySelectorAll('p')).find((p) => p.textContent === 'Growing tail.')).toBe(later)
    rerender(<MarkdownRenderer content={`${head} tail.\n\nAnother paragraph.`} />)
    // Every piece keeps its node when the stream ends, not only the first, so
    // state inside a later piece (an image allowed to load) survives.
    expect(Array.from(container.querySelectorAll('p')).find((p) => p.textContent === 'Settled paragraph.')).toBe(settled)
    expect(Array.from(container.querySelectorAll('p')).find((p) => p.textContent === 'Growing tail.')).toBe(later)
  })
})
