import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { fixCjkAutolinkBoundaries, fixCodeFences } from '../components/MarkdownRenderer'

/**
 * GFM's autolink literal only ends at ASCII whitespace or `<`, so CJK prose
 * written the normal way — no space between the URL and the punctuation that
 * follows it — feeds the punctuation, and any backtick after it, into the href.
 * See the block comment above fixCjkAutolinkBoundaries.
 */
describe('fixCjkAutolinkBoundaries — cuts on evidence', () => {
  it('handles the reported case — comma, code span, paren, colon', () => {
    const src = '**#2137**（https://github.com/o/r/pull/2137，`96ed647b`）：`readiness: passed`'
    expect(fixCjkAutolinkBoundaries(src)).toBe(
      '**#2137**（<https://github.com/o/r/pull/2137>，`96ed647b`）：`readiness: passed`',
    )
  })

  it('cuts at a CJK closing bracket whose opener is outside the URL', () => {
    expect(fixCjkAutolinkBoundaries('（https://example.com/a）')).toBe('（<https://example.com/a>）')
    expect(fixCjkAutolinkBoundaries('【https://example.com/a】')).toBe('【<https://example.com/a>】')
    expect(fixCjkAutolinkBoundaries('「https://example.com/a」')).toBe('「<https://example.com/a>」')
  })

  it('cuts when the opener is earlier on the line, not adjacent', () => {
    expect(fixCjkAutolinkBoundaries('（见 https://example.com/a）')).toBe(
      '（见 <https://example.com/a>）',
    )
  })

  it('cuts at CJK punctuation when a backtick follows it directly', () => {
    for (const p of ['。', '，', '、', '；', '：', '？', '！', '…', '·', '～', '“']) {
      expect(fixCjkAutolinkBoundaries(`x https://example.com/a${p}\`c\``)).toBe(
        `x <https://example.com/a>${p}\`c\``,
      )
    }
  })

  it('cuts at the START of a contiguous punctuation run', () => {
    expect(fixCjkAutolinkBoundaries('https://example.com/a、，`c`')).toBe(
      '<https://example.com/a>、，`c`',
    )
  })

  it('fixes every URL in a multi-URL paragraph', () => {
    expect(fixCjkAutolinkBoundaries('（https://a.com/1）和（https://b.com/2）')).toBe(
      '（<https://a.com/1>）和（<https://b.com/2>）',
    )
  })

  it('fixes a second URL whose opener sits in the prose INSIDE the same run', () => {
    // One whitespace-delimited run holds both URLs, so the `【` lives inside the
    // autolink node — but it is real prose, and it is the only opener the second
    // URL's `】` has to close.
    expect(fixCjkAutolinkBoundaries('（https://a.com/1）和【https://b.com/2】')).toBe(
      '（<https://a.com/1>）和【<https://b.com/2>】',
    )
  })

  it('drops the trailing punctuation GFM would have trimmed', () => {
    expect(fixCjkAutolinkBoundaries('https://example.com/a.，`c`')).toBe(
      '<https://example.com/a>.，`c`',
    )
    expect(fixCjkAutolinkBoundaries('https://example.com/a)，`c`')).toBe(
      '<https://example.com/a>)，`c`',
    )
  })

  it('keeps a balanced ASCII paren that belongs to the URL', () => {
    expect(fixCjkAutolinkBoundaries('https://example.com/a_(b)，`c`')).toBe(
      '<https://example.com/a_(b)>，`c`',
    )
  })
})

describe('fixCjkAutolinkBoundaries — refuses to cut without evidence', () => {
  it('leaves CJK punctuation that is part of a real page title alone', () => {
    // All three are live Wikipedia articles; GFM links them correctly today and
    // this pass must not "fix" them into the wrong page.
    for (const src of [
      'https://zh.wikipedia.org/wiki/苹果（公司）',
      'https://zh.wikipedia.org/wiki/我，机器人',
      'https://ja.wikipedia.org/wiki/モーニング娘。',
      'https://zh.wikipedia.org/wiki/中文',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('leaves a CJK-titled URL alone even when a code span follows in the run', () => {
    // The comma is followed by more TITLE, not by the markup — the backtick is a
    // separate token. Requiring the evidence to sit directly after the
    // punctuation is what keeps this link pointing at the right article.
    const src = 'https://zh.wikipedia.org/wiki/我，机器人`简介`'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('leaves a closing bracket with no opener to close alone', () => {
    // Nothing before the URL opened a `（`, so the bracket is plausibly part of
    // the query string — GFM links it today and must keep doing so.
    for (const src of [
      'see https://example.com/search?q=foo）',
      'https://example.com/a】',
      // Opener already closed before the URL starts.
      '（注）https://example.com/search?q=foo）',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('treats only a backtick as evidence, not other markdown-active chars', () => {
    // `*`, `[` and `]` are legal and common in query strings, so they cannot
    // stand as proof that the URL ended. Only the backtick — which RFC 3986
    // excludes, so browsers percent-encode it — qualifies.
    for (const src of [
      'https://www.google.com/search?q=中文，*test',
      'https://example.com/a?filter[name]=中文，[x]',
      'https://example.com/a，**注意**',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('does not let a bracket from a non-prose region supply the opener', () => {
    // The `（` lives in an earlier URL's query string, an inline-code span, and an
    // HTML attribute respectively — none of them is bracket context for the URL
    // that follows, so its legitimate `）` must survive.
    for (const src of [
      'https://example.com/?q=（ https://example.com/?q=）',
      '`（` https://example.com/?q=）',
      '<span title="（"></span> https://example.com/?q=）',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('leaves a bare CJK sentence running off a URL alone', () => {
    // Character-for-character indistinguishable from `…/wiki/我，机器人`, so this
    // keeps today's behaviour rather than risking a correct link. Documented
    // limitation, not an oversight.
    const src = '见 https://example.com/pull/1，然后回来'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('leaves a URL with no CJK punctuation after it alone', () => {
    const src = 'see https://example.com/a, and `code` here'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('does not touch an inline-code span', () => {
    const src = '写 `curl https://example.com/a，`c`` 就行'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('does not touch a fenced code block', () => {
    const src = '```sh\ncurl https://example.com/a，`c`\n```\n'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('does not touch a BLOCKQUOTED fenced code block', () => {
    const src = '> ```sh\n> curl https://example.com/a，`c`\n> ```\n'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('does not touch an indented code block, quoted or tab-indented', () => {
    for (const src of [
      'text\n\n    curl https://example.com/a，`c`\n',
      '> text\n>\n>     curl https://example.com/a，`c`\n',
      // CommonMark: one tab advances to column 4, so this is a code block too.
      'text\n\n\tcurl https://example.com/a，`c`\n',
      'text\n\n \tcurl https://example.com/a，`c`\n',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('does not touch a MULTI-LINE inline-code span', () => {
    // CommonMark inline code may span line breaks. A per-line mask cannot pair
    // the opening ``` `` ``` with its closer on the next line; the parser does.
    const src = '``see https://example.com/a，`c`\ntail``\n'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('does not touch a fenced block whose content looks like a closer', () => {
    // `~~~not-a-close` is content, not a closing fence — a closer may carry only
    // spaces or tabs after the marker.
    const src = '~~~\n~~~not-a-close\ncurl https://example.com/a，`c`\n~~~\n'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('does not touch a math span', () => {
    const src = '$$\nf(https://example.com/a，`c`)\n$$\n'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('does not touch an existing markdown link, image, or angle autolink', () => {
    for (const src of [
      '[看这里](https://example.com/a，`c`)',
      '![图](https://example.com/a，`c`)',
      '<https://example.com/a，`c`>',
      '<a href="https://example.com/a，`c`">x</a>',
      '[ref]: https://example.com/a，`c`',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('does not CREATE a link out of a run GFM would not have autolinked', () => {
    // No dot in the host, and `_` in a trailing label — GFM autolinks neither,
    // so wrapping them in <…> would invent a link the author never wrote.
    for (const src of ['http://localhost:5476/a，`c`', 'http://a_b.com/x，`c`']) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('still rewrites prose inside a blockquote that is not code', () => {
    expect(fixCjkAutolinkBoundaries('> 见（https://example.com/a）')).toBe(
      '> 见（<https://example.com/a>）',
    )
  })

  it('still rewrites prose in an indented list continuation', () => {
    // The old conservative mask blanked any 4-column-indented line, so a list
    // paragraph was skipped. Reading regions off the parse distinguishes a list
    // continuation from an indented code block.
    expect(fixCjkAutolinkBoundaries('- item\n\n    见（https://example.com/a）\n')).toBe(
      '- item\n\n    见（<https://example.com/a>）\n',
    )
  })

  it('still rewrites prose in an indented list continuation', () => {
    // The old conservative mask blanked any 4-column-indented line, so a list
    // paragraph was skipped. Reading regions off the parse distinguishes a list
    // continuation from an indented code block.
    expect(fixCjkAutolinkBoundaries('- item\n\n    见（https://example.com/a）\n')).toBe(
      '- item\n\n    见（<https://example.com/a>）\n',
    )
  })
})

describe('fixCjkAutolinkBoundaries — pipeline order', () => {
  it('sees the code block that fixCodeFences creates from a glued fence opener', () => {
    // `text```sh` is paragraph text until fixCodeFences inserts the blank line
    // that turns it into a real fence. Boundary rewriting must run AFTER that,
    // or the URL is judged as prose and the displayed code gains a literal `<…>`.
    const src = 'text```sh\ncurl https://example.com/a，`c`\n```\n'
    const prepared = fixCjkAutolinkBoundaries(fixCodeFences(src))
    expect(prepared).toContain('curl https://example.com/a，`c`')
    expect(prepared).not.toContain('<https://')
  })
})

describe('fixCjkAutolinkBoundaries — rendered output', () => {  const renderMd = (src: string) =>
    render(<ReactMarkdown remarkPlugins={[remarkGfm]}>{fixCjkAutolinkBoundaries(src)}</ReactMarkdown>)

  it('keeps the punctuation out of the href', () => {
    const { container } = renderMd('（https://github.com/o/r/pull/2137，`96ed647b`）')
    expect(container.querySelector('a')?.getAttribute('href')).toBe('https://github.com/o/r/pull/2137')
  })

  it('restores the code-span pairing the swallowed backtick had shifted', () => {
    const src = '（https://github.com/o/r/pull/2137，`96ed647b`）：`readiness: passed`，45 绿 0 红'
    const { container } = renderMd(src)
    expect([...container.querySelectorAll('code')].map((c) => c.textContent)).toEqual([
      '96ed647b',
      'readiness: passed',
    ])
    // The tell-tale of the bug: literal backticks left in the rendered text.
    expect(container.textContent).not.toContain('`')
  })

  it('leaves a CJK-titled article URL linking to the right page', () => {
    const { container } = renderMd('https://ja.wikipedia.org/wiki/モーニング娘。')
    expect(container.querySelector('a')?.getAttribute('href')).toBe(
      'https://ja.wikipedia.org/wiki/%E3%83%A2%E3%83%BC%E3%83%8B%E3%83%B3%E3%82%B0%E5%A8%98%E3%80%82',
    )
  })
})
