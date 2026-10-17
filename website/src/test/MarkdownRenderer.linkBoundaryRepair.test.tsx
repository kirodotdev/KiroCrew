import { describe, it, expect } from 'vitest'
import { fixCjkAutolinkBoundaries, fixUnencodedLinkDestinations } from '../components/MarkdownRenderer'

/**
 * The two source-level link repairs, pinned as input -> output over every
 * evidence rule they apply and every refusal they make.
 *
 * Both repairs rewrite the message text before it is parsed, and both read
 * remark's own parse (the renderer's grammar, which the facade hands them) to
 * know what is prose. A case list shows, in one diff, any change to where a URL
 * is judged to end, which spans are off-limits, or which refused destination
 * is rescued. The outputs were recorded against the renderer before its
 * pipeline was split into modules.
 */

const CJK_CASES = [
  // A CJK closing bracket that closes an opener before the URL.
  '（https://example.com/pull/1，`abc`）：`ready`',
  '（见 https://x.com/a）后面',
  // The opener is inside the URL, or there is no opener to close: no cut.
  'https://x.com/苹果（公司） done',
  'https://x.com/search?q=foo） done',
  // Nested brackets inside the run.
  '（https://a.example/【x】）和',
  // A separator directly before a backtick.
  'PR https://github.com/o/r/pull/2137，`96ed647b` 已合并',
  // Sentence enders never end a URL; a separator after one still can.
  'see https://zh.wikipedia.org/wiki/モーニング娘。`紹介`',
  'see https://a.example/x。，`code`',
  // A strong delimiter the author wrote to close an opener before the URL.
  '**https://example.com/a**（revision 1）',
  '__https://example.com/a__（x）',
  '已建好：**https://example.com/a**（revision 1），说明见 **文档**。',
  // A closer still available after the URL: the run's delimiter is the URL's.
  '**See https://example.com/a**（x） for details**',
  // Intraword or ambiguous delimiters are no evidence.
  '__See https://example.com/a__b for details__',
  'a**b https://example.com/a**（x）',
  '**foo* https://example.com/a**（x）',
  // Escape parity decides whether the opener exists.
  '\\**https://example.com/a**（x）',
  '\\\\**https://example.com/a**（x）',
  '**https://example.com/a\\**（x）',
  // Two URLs in one autolink run, and a URL nested in another's query.
  '（https://a.example/1）和【https://b.example/2】',
  '（https://a.example/?u=https://b.example/x）',
  // Hosts GFM would not have linked, and tails GFM trims.
  '（https://a_b.example_c/x）',
  '（https://a.example/1.，`b`）',
  '（https://a.example/(x)）',
  '（https://a.example/x)）',
  // Regions that are never prose.
  '`（https://a.example/1）`',
  '[x](https://a.example/1)（y）',
  '<https://a.example/1>（y）',
  '$$（https://a.example/1）$$',
  '<span title="（">https://a.example/1）</span>',
  // The bracket context is line-scoped.
  'line one（\nhttps://a.example/x）',
  // Fast paths.
  'no scheme here（x）',
  'https://a.example/plain ascii only',
]

const DEST_CASES = [
  '[q](https://x.example/new?title=a b&labels=bug)',
  '[q](https://x.example/new?title=a\tb&labels=bug)',
  '[q]( https://x.example/new?title=a b c&labels=bug&x=1 )',
  // No evidence the query spans the spaces: prose after a truncated link.
  '[docs](https://x.com/a?ref=1 for the full list)',
  '[docs](https://x.com/a for the full list)',
  // A trailing quoted chunk is undecidable.
  '[a](https://x.example/p?q=1 x&y=2 "t")',
  "[a](https://x.example/p?q=1 x&y=2 't')",
  // Escaped brackets, an empty label, code, parens, a real title.
  '\\[q](https://x.example/new?t=a b&l=c)',
  '[q\\](https://x.example/new?t=a b&l=c)',
  '[](https://x.example/new?t=a b&l=c)',
  '`[q](https://x.example/new?t=a b&l=c)`',
  '[q](https://x.example/new?t=(a) b&l=c)',
  '[q](https://x.example/new?t=1 "title")',
  // Images are in scope; the leading `!` stays outside the match.
  '![a](https://x.example/i?t=a b&l=c)',
  // Several spans on one line, and a line break inside the destination.
  '[a](https://x.example/n?t=a b&l=c) and [b](https://y.example/n?t=d e&l=f)',
  '[a](https://x.example/n?t=a\nb&l=c)',
  // Fast paths.
  'no link here',
  '[a](/relative?x=a b&y=c)',
]

describe('MarkdownRenderer source-level link repairs', () => {
  it('closes a bare URL only where the evidence rules prove it ended', () => {
    expect(CJK_CASES.map(c => JSON.stringify(fixCjkAutolinkBoundaries(c))).join('\n')).toMatchInlineSnapshot(`
      ""（<https://example.com/pull/1>，\`abc\`）：\`ready\`"
      "（见 <https://x.com/a>）后面"
      "https://x.com/苹果（公司） done"
      "https://x.com/search?q=foo） done"
      "（<https://a.example/【x】>）和"
      "PR <https://github.com/o/r/pull/2137>，\`96ed647b\` 已合并"
      "see https://zh.wikipedia.org/wiki/モーニング娘。\`紹介\`"
      "see <https://a.example/x。>，\`code\`"
      "**<https://example.com/a>**（revision 1）"
      "__<https://example.com/a>__（x）"
      "已建好：**<https://example.com/a>**（revision 1），说明见 **文档**。"
      "**See https://example.com/a**（x） for details**"
      "__See https://example.com/a__b for details__"
      "a**b https://example.com/a**（x）"
      "**foo* https://example.com/a**（x）"
      "\\\\**https://example.com/a**（x）"
      "\\\\\\\\**<https://example.com/a>**（x）"
      "**https://example.com/a\\\\**（x）"
      "（<https://a.example/1>）和【<https://b.example/2>】"
      "（<https://a.example/?u=https://b.example/x>）"
      "（https://a_b.example_c/x）"
      "（<https://a.example/1>.，\`b\`）"
      "（<https://a.example/(x)>）"
      "（<https://a.example/x>)）"
      "\`（https://a.example/1）\`"
      "[x](https://a.example/1)（y）"
      "<https://a.example/1>（y）"
      "$$（https://a.example/1）$$"
      "<span title=\\"（\\">https://a.example/1）</span>"
      "line one（\\nhttps://a.example/x）"
      "no scheme here（x）"
      "https://a.example/plain ascii only""
    `)
  })

  it('encodes a refused destination only when the query provably spans it', () => {
    expect(DEST_CASES.map(c => JSON.stringify(fixUnencodedLinkDestinations(c))).join('\n')).toMatchInlineSnapshot(`
      ""[q](https://x.example/new?title=a%20b&labels=bug)"
      "[q](https://x.example/new?title=a%09b&labels=bug)"
      "[q]( https://x.example/new?title=a%20b%20c&labels=bug&x=1 )"
      "[docs](https://x.com/a?ref=1 for the full list)"
      "[docs](https://x.com/a for the full list)"
      "[a](https://x.example/p?q=1 x&y=2 \\"t\\")"
      "[a](https://x.example/p?q=1 x&y=2 't')"
      "\\\\[q](https://x.example/new?t=a b&l=c)"
      "[q\\\\](https://x.example/new?t=a b&l=c)"
      "[](https://x.example/new?t=a b&l=c)"
      "\`[q](https://x.example/new?t=a b&l=c)\`"
      "[q](https://x.example/new?t=(a) b&l=c)"
      "[q](https://x.example/new?t=1 \\"title\\")"
      "![a](https://x.example/i?t=a%20b&l=c)"
      "[a](https://x.example/n?t=a%20b&l=c) and [b](https://y.example/n?t=d%20e&l=f)"
      "[a](https://x.example/n?t=a\\nb&l=c)"
      "no link here"
      "[a](/relative?x=a b&y=c)""
    `)
  })

  it('returns the input unchanged when nothing is repaired', () => {
    for (const input of ['plain text', 'https://a.example/x', '（no url）']) {
      expect(fixCjkAutolinkBoundaries(input)).toBe(input)
      expect(fixUnencodedLinkDestinations(input)).toBe(input)
    }
  })
})
