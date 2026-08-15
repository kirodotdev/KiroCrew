import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import ReactMarkdown from 'react-markdown'
import remarkCjkFriendly from 'remark-cjk-friendly'
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

  it('cuts at a SEPARATOR when a backtick follows it directly', () => {
    for (const p of ['，', '、', '；', '：', '·', '～', '“']) {
      expect(fixCjkAutolinkBoundaries(`x https://example.com/a${p}\`c\``)).toBe(
        `x <https://example.com/a>${p}\`c\``,
      )
    }
  })

  it('cuts at the separator in a run that starts with a sentence-ender', () => {
    // The `。` may belong to the title, so it stays inside the URL; the `，`
    // after it is the boundary.
    expect(fixCjkAutolinkBoundaries('https://example.com/a。，`c`')).toBe(
      '<https://example.com/a。>，`c`',
    )
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

  it('leaves a sentence-ender alone even when a backtick follows it directly', () => {
    // These are the shapes real CJK titles take, and they reach the URL raw.
    // Cutting here would point the anchor at the wrong article, so a
    // sentence-ender is never a boundary — the trade is that a genuine prose
    // `。` before a code span keeps today's behaviour.
    for (const src of [
      'https://ja.wikipedia.org/wiki/モーニング娘。`紹介`',
      'https://ja.wikipedia.org/wiki/魔法先生ネギま！`概要`',
      'https://example.com/a？`c`',
      'https://example.com/a…`c`',
      'https://example.com/a．`c`',
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

describe('fixCjkAutolinkBoundaries — swallowed emphasis delimiter', () => {
  it('handles the reported case — a bold-wrapped URL followed by a CJK paren', () => {
    const src = '已建好：**https://example.com/reviews/2137**（revision 1）'
    expect(fixCjkAutolinkBoundaries(src)).toBe(
      '已建好：**<https://example.com/reviews/2137>**（revision 1）',
    )
  })

  // CommonMark consumes delimiter CHARACTERS, not runs. These four pin that: the
  // first would be cut by run-scoring, and the second is what a blanket
  // "odd-length run is inconclusive" rule would wrongly refuse.
  it('sees a lone `*` eat one half of the `**`, leaving no strong opener', () => {
    const src = '**foo* https://example.com/a**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('still acts on a bold-italic URL, whose opener run is three characters', () => {
    const src = '***https://example.com/a***（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe('***<https://example.com/a>***（x）')
  })

  it('is undisturbed by a lone `*` that prose uses literally', () => {
    const src = '2 * 3 = 6 见 https://example.com/a**（x）'
    // Nothing to cut: no opener at all, so the lone `*` must not manufacture one.
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('counts a closed pair as closed and the next opener as open', () => {
    const src = '**b** 然后 **https://example.com/a**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe('**b** 然后 **<https://example.com/a>**（x）')
  })

  it('refuses when the prose AFTER the URL still has a closer for the opener', () => {
    // The author's pair is `**See … for details**`, so the `**（公司）` belongs to the
    // URL. Cutting would truncate the href AND orphan the real closing delimiter.
    const src = '**See https://example.com/wiki/苹果**（公司） for details**'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('still acts when the trailing delimiters PAIR with each other', () => {
    // Parity, not presence: these two close each other, so none is left over for the
    // opener and the boundary inside the run is the only reading that closes it.
    const src = '已建好：**https://example.com/reviews/2137**（revision 1），说明见 **文档**。'
    expect(fixCjkAutolinkBoundaries(src)).toBe(
      '已建好：**<https://example.com/reviews/2137>**（revision 1），说明见 **文档**。',
    )
  })

  it('refuses when the prose bold already CLOSED on CJK punctuation', () => {
    // `remark-cjk-friendly` pairs `**中文。**` as one closed strong, so no opener is
    // waiting when the URL starts and the `**（x）` belongs to the URL. Measuring
    // plain CommonMark here would score that closer as a second opener and truncate
    // a link the renderer already gets right.
    const src = '**中文。**后续 https://example.com/a**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('still acts when the CJK-punctuation-adjacent run is the OPENER', () => {
    // Same character class as above, but nothing precedes it to close, so `：**`
    // opens and the delimiter inside the run is the swallowed closing half.
    const src = '见：**https://example.com/a**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe('见：**<https://example.com/a>**（x）')
  })

  it('refuses when a prefix run could be either an opener or a closer', () => {
    // `a**b` is both left- and right-flanking, so counting it either way can be
    // wrong. The whole line is inconclusive rather than guessed at.
    const src = 'a**b 见 https://example.com/x**（y）'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  // cases pin PARITY and POSITION rather than the mere presence of a backslash —
  // treating any backslash as disqualifying would refuse the last two, which carry
  // real delimiters.
  it('does not read an escaped `\\**` as an opener', () => {
    const src = '\\**See https://example.com/a**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('does not read an escaped `\\**` inside the run as a closer', () => {
    const src = '**See https://example.com/a\\**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('still acts when the BACKSLASH is what was escaped, leaving `**` intact', () => {
    const src = '\\\\**See https://example.com/a**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe('\\\\**See <https://example.com/a>**（x）')
  })

  it('still acts on the real `**` that follows an escaped `*`', () => {
    const src = '\\***See https://example.com/a**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe('\\***See <https://example.com/a>**（x）')
  })

  it('refuses an all-ASCII paragraph — the rule needs positive evidence', () => {
    // The same failure happens in ASCII prose, but `(` after the delimiter is
    // exactly what a query string looks like (`?q=foo**-bar`), so there is nothing
    // to tell the two apart. Fullwidth punctuation is the evidence this pass
    // requires; an ASCII mark is not, which is why the pass carries `Cjk` in its
    // name and leaves this shape alone.
    const src = '**https://example.com/pull/2137**(revision 1)'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('cuts on a fullwidth comma too, not just a bracket', () => {
    expect(fixCjkAutolinkBoundaries('**https://example.com/pr/1**，已合并')).toBe(
      '**<https://example.com/pr/1>**，已合并',
    )
  })

  it('refuses when an IDEOGRAPH follows the delimiter, not punctuation', () => {
    // `已` is legal mid-path, so it is not evidence the URL ended — an earlier
    // revision of this rule accepted any non-ASCII character and truncated
    // `?q=a**中文`. The cost of the narrower class is that this shape, which really
    // is the same bug, goes unfixed; the shape the rule is for is `**url**（…`.
    const src = '**https://example.com/pr/1**已合并'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('cuts on a `__` delimiter too', () => {
    expect(fixCjkAutolinkBoundaries('见 __https://example.com/a__（x）')).toBe(
      '见 __<https://example.com/a>__（x）',
    )
  })

  it('cuts a `__` opener that sits against a FULLWIDTH character', () => {
    // `_`'s extra intraword condition tests punct-or-whitespace in the RAW sense, so
    // CJK punctuation counts for it even though the amendment excludes wide characters
    // from the punctuation class used for flanking. Under the amended class a fullwidth
    // `：` reads as neither, which makes `：__` look intraword — so the opener is
    // rejected and the reported shape written with underscores goes uncut. An opener
    // separated by an ASCII space does not exercise this at all.
    for (const [src, want] of [
      [
        '已建好：__https://example.com/reviews/2137__（revision 1）',
        '已建好：__<https://example.com/reviews/2137>__（revision 1）',
      ],
      ['地址：__https://example.com/a__：请查收', '地址：__<https://example.com/a>__：请查收'],
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(want)
    }
  })

  it('refuses when every opener in the prefix is already closed', () => {
    // `**注意**` is balanced, so the `**` inside the URL has nothing to close and
    // is plausibly part of the URL itself.
    const src = '**注意** 见 https://example.com/a**b，然后'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('refuses when nothing before the URL opened an emphasis', () => {
    const src = 'https://example.com/a**b，然后'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('refuses on an INTRAWORD `__`, which GFM renders literally', () => {
    // `report__final` cannot open emphasis (CommonMark forbids intraword `_`), so
    // a textual parity count would see one `__` and truncate a correct link. Both
    // halves of a filename with a double underscore are a real pattern, and so are
    // URLs carrying `__` (path segments, `__hstc`-style tracker params).
    for (const src of [
      'the file report__final.pdf, see https://drive.example.com/report__final.pdf',
      'foo__bar；见 https://example.com/a__b，然后',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('refuses a `**` inside a QUERY STRING, whatever follows it', () => {
    // `?q=foo**-bar` puts a word character before the delimiter and punctuation
    // after it — character-for-character the shape of a real closer before
    // punctuation. Flanking cannot separate them; only the fullwidth-punctuation
    // requirement can, and `-` is ASCII. The third case is the same defect with an
    // ideograph after the delimiter, which "any non-ASCII" would have accepted.
    for (const src of [
      '**See https://example.com/search?q=foo**-bar for details**',
      '**https://example.com/?q=foo**-bar**more**',
      '**See https://example.com/search?q=a**中文 for details**',
      '**See https://example.com/search?q=a**中文',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('refuses when the prefix opener is real but the CANDIDATE closes nothing', () => {
    // Checking only the opener is not enough. Both of these open a genuine
    // delimiter and then hit one sitting INSIDE the URL path; cutting there would
    // truncate a correct link, and the emphasis would stay open regardless.
    for (const src of [
      '__See https://example.com/a__b for details__',
      '**Note: https://example.com/a**b',
    ]) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('refuses when an ASCII word character follows the closing delimiter', () => {
    // `…/2137**and` is indistinguishable from a path containing `**`, and the
    // emphasis could not re-close there anyway (`>` before a letter is not
    // right-flanking).
    const src = 'Filed **https://example.com/pull/2137**and merged it'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('refuses when the pending opener is a NON-FLANKING run a parity count would accept', () => {
    // This is what separates `hasPendingStrongOpener` from a textual count: the `**`
    // in `a ** b` has whitespace on both sides, so GFM renders it literally and no
    // emphasis is open when the URL starts — but a count sees one `**` and calls it
    // pending. The candidate here IS followed by fullwidth punctuation, so the
    // opener check is the only thing that refuses it.
    const src = 'a ** b 见 https://example.com/x**（y）'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('refuses when nothing at all opened an emphasis before the URL', () => {
    // No `**` in the prefix, and `**（` inside the path. Without the opener check
    // this would be cut on the fullwidth mark alone.
    const src = '见 https://example.com/a**（x）'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('refuses on a NON-FLANKING `**`, which opens nothing', () => {
    // Whitespace on both sides makes the run neither left- nor right-flanking, so
    // it is literal text and there is no opener for the URL's `**` to close.
    const src = 'a ** b, see https://example.com/c**d, then'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('refuses when the prefix run could be either an opener or a closer', () => {
    // `a**b` is both left- and right-flanking, so whether an emphasis is open at
    // the URL is genuinely undetermined — the conservative reading wins.
    const src = 'a**b see https://example.com/c**d, then'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('leaves a trailing delimiter alone — GFM keeps it out of the run', () => {
    // Both render correctly today. GFM trims a trailing `*` off the autolink
    // literal, so the delimiter is never inside the node's source and there is
    // nothing here to cut.
    for (const src of ['**https://example.com/a**', '**https://example.com/a** 已合并']) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
  })

  it('does not let a `**` from a non-prose region supply the opener', () => {
    const src = '`**` https://example.com/a**b，然后'
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })

  it('treats a SINGLE `*` or `_` as too weak to act on', () => {
    // Both are legal in a URL and common in query strings, so an unpaired one
    // before the URL is not evidence — same reason `*` is excluded from the
    // markdown-active class.
    for (const src of ['*https://example.com/a*b，然后', '_https://example.com/a_b，然后']) {
      expect(fixCjkAutolinkBoundaries(src)).toBe(src)
    }
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

describe('fixCjkAutolinkBoundaries — never worsens a render the plugin gets right', () => {
  // This pass models the grammar `remark-cjk-friendly` implements, so a plugin bump
  // that shifts its character classes could desync the model and start cutting URLs
  // the renderer now pairs correctly. Direct-call assertions cannot see that. This
  // one can: it renders through the PRODUCTION chain and holds the invariant that
  // decides every case — if the renderer already gets a line right, the pass must
  // leave it alone. A desync flips a case here and reds, instead of shipping as a
  // silently truncated href.
  //
  // Every delimiter needs a case whose opener sits DIRECTLY against a fullwidth
  // character. That is the neighbour the amendment reclassifies, so an opener held
  // off by an ASCII space exercises none of it and leaves the shape CJK prose
  // actually uses — `已建好：__url__（…）` — unguarded.
  const FLANKING_SENSITIVE = [
    '已建好：**https://example.com/reviews/2137**（revision 1）',
    '已建好：**https://example.com/reviews/2137**（revision 1），说明见 **文档**。',
    '**中文。**后续 https://example.com/a**（x）',
    '**See https://example.com/wiki/苹果**（公司） for details**',
    '**See https://example.com/wiki/苹果**（公司）',
    '**See https://example.com/a**（公司） for 详情**后续',
    '已建好：__https://example.com/reviews/2137__（revision 1）',
    '地址：__https://example.com/a__：请查收',
    '__https://example.com/a__（x），说明见 __文档__。',
    '__See https://example.com/a__（公司） for 详情__后续',
    'a**b 见 https://example.com/x**（y）',
    '**foo* https://example.com/a**（x）',
    '***https://example.com/a***（x）',
    '\\**See https://example.com/a**（x）',
    'report__final.pdf, see https://drive.example.com/report__final.pdf',
  ]

  const renderReal = (src: string) =>
    render(<ReactMarkdown remarkPlugins={[remarkCjkFriendly, remarkGfm]}>{src}</ReactMarkdown>)
      .container

  const hrefOf = (c: Element) => c.querySelector('a')?.getAttribute('href') ?? ''
  const strongCount = (c: Element) => c.querySelectorAll('strong').length

  // The invariant that decides every case in this pass: shortening an href is only
  // ever justified when it BUYS emphasis the renderer could not produce on its own.
  // A cut that shortens the href without raising the strong count has taken a URL
  // apart for nothing — which is precisely how the last three counter-examples
  // presented. Expressed this way it also survives a plugin bump: if an amendment
  // change makes the renderer pair a line it used to leave literal, that line's
  // strong count stops rising and this reds instead of shipping a truncated href.
  it.each(FLANKING_SENSITIVE)('only shortens an href when that restores emphasis: %s', (src) => {
    const fixed = fixCjkAutolinkBoundaries(src)
    if (fixed === src) return // refused — nothing to check
    const raw = renderReal(src)
    const out = renderReal(fixed)
    const shortened = hrefOf(out).length < hrefOf(raw).length
    if (!shortened) return
    expect(strongCount(out)).toBeGreaterThan(strongCount(raw))
  })
})

describe('fixCjkAutolinkBoundaries — rendered output', () => {
  // The PRODUCTION plugin chain: `remark-cjk-friendly` runs BEFORE `remark-gfm`
  // and changes how emphasis delimiters are classified, so asserting against gfm
  // alone would grade this pass on a grammar the dashboard does not run.
  const renderMd = (src: string) =>
    render(
      <ReactMarkdown remarkPlugins={[remarkCjkFriendly, remarkGfm]}>
        {fixCjkAutolinkBoundaries(src)}
      </ReactMarkdown>,
    )

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

  it('gives the bold its delimiter back instead of two literal asterisks', () => {
    const src = '已建好：**https://example.com/reviews/2137**（revision 1，目前是 draft）'
    const { container } = renderMd(src)
    const a = container.querySelector('a')
    expect(a?.getAttribute('href')).toBe('https://example.com/reviews/2137')
    // The delimiter is a delimiter again: it closes the bold around the link…
    expect(container.querySelector('strong a')).not.toBeNull()
    // …instead of surviving as text, which is the tell-tale of the bug.
    expect(container.textContent).not.toContain('*')
    expect(container.textContent).toContain('（revision 1，目前是 draft）')
  })

  it('gives a `__`-delimited bold its delimiter back as well', () => {
    // The same reported shape written with underscores. Without the raw-class read of
    // `_`'s intraword condition the href swallows `__（revision`, the bold never opens,
    // and the underscores survive as text.
    const src = '已建好：__https://example.com/reviews/2137__（revision 1，还没 publish）。'
    const { container } = renderMd(src)
    expect(container.querySelector('a')?.getAttribute('href')).toBe(
      'https://example.com/reviews/2137',
    )
    expect(container.querySelector('strong a')).not.toBeNull()
    expect(container.textContent).not.toContain('_')
    expect(container.textContent).toContain('（revision 1，还没 publish）。')
  })

  it('markdown link syntax avoids autolink greediness entirely', () => {
    // When the agent uses [text](url) syntax instead of bare URLs, GFM autolink
    // is never triggered — the URL lives inside the parentheses of a proper link
    // node, so CJK punctuation around it cannot be swallowed.
    const src = 'PR：[PR #3739](https://github.com/kirodotdev/KiroCrew/pull/3739)（commit `e6b4bf448`）'
    const { container } = renderMd(src)
    const a = container.querySelector('a')
    expect(a?.getAttribute('href')).toBe('https://github.com/kirodotdev/KiroCrew/pull/3739')
    expect(a?.textContent).toBe('PR #3739')
    // The surrounding text must NOT be swallowed into the link.
    expect(container.textContent).toContain('（commit')
    expect(container.textContent).toContain('e6b4bf448')
  })

  it('documents the known limitation: bare URL + （ without opener cannot be fixed', () => {
    // This is the edge case from the bug report: bare URL immediately followed
    // by （ with no matching opener earlier on the line. fixCjkAutolinkBoundaries
    // cannot prove （ does not belong to the URL (it could be a path component),
    // so it leaves it alone — GFM then swallows everything up to the next space.
    // The fix is to use markdown link syntax (tested above), not to make this
    // function more aggressive (which would break legitimate CJK-titled URLs).
    const src = 'PR：https://github.com/o/r/pull/3739（commit `e6b4bf448`）'
    // Without a matching opener, the function correctly leaves it untouched.
    expect(fixCjkAutolinkBoundaries(src)).toBe(src)
  })
})
