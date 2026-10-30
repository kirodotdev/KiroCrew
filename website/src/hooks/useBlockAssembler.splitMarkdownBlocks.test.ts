import { describe, it, expect } from 'vitest'
import { parseBlocks, splitMarkdownBlocks } from './useBlockAssembler'

/** The streaming parse of `raw`, split the way the assembler's snapshot is. */
const split = (raw: string) => splitMarkdownBlocks(parseBlocks(raw, true))
const contents = (raw: string) => split(raw).map((b) => b.content)

describe('splitMarkdownBlocks', () => {
  it('splits settled paragraphs off the open markdown block', () => {
    const blocks = split('One.\n\nTwo.\n\n\nThree is still stre')
    expect(blocks.map((b) => b.content)).toEqual(['One.', 'Two.', 'Three is still stre'])
    expect(blocks.map((b) => b.startLine)).toEqual([1, 3, 6])
    expect(blocks.map((b) => b.split)).toEqual([
      { from: 1, part: 0 },
      { from: 1, part: 1 },
      { from: 1, part: 2 },
    ])
  })

  it('keeps settled pieces byte-identical as the tail grows', () => {
    const a = split('# Title\n\nFirst paragraph.\n\nSecond')
    const b = split('# Title\n\nFirst paragraph.\n\nSecond paragraph, longer.\n\nThird')
    expect(b.slice(0, 2).map((x) => x.content)).toEqual(a.slice(0, 2).map((x) => x.content))
  })

  it('does not split before a paragraph that has not started', () => {
    expect(contents('One.\n\nTwo.\n\n')).toEqual(['One.', 'Two.\n\n'])
  })

  it('never splits between list items, which may form one loose list', () => {
    expect(contents('- a\n\n- b\n\n1. c')).toEqual(['- a\n\n- b\n\n1. c'])
  })

  it('keeps a list whole across lazy continuation lines', () => {
    expect(contents('1. First\ncontinued\n\n1. Second\n\nDone.')).toEqual(['1. First\ncontinued\n\n1. Second', 'Done.'])
  })

  it('leaves a block whole when a reference definition sits inside a list', () => {
    expect(contents('See [x][ref].\n\n- item\n\n    [ref]: https://example.com')).toHaveLength(1)
  })

  it('leaves a block whole for a reference label that spans lines or escapes a bracket', () => {
    expect(contents('See [a][long label].\n\nMore.\n\n[long\nlabel]: https://example.com')).toHaveLength(1)
    expect(contents('See [a][x\\]y].\n\nMore.\n\n[x\\]y]: https://example.com')).toHaveLength(1)
  })

  it('treats a list opener indented up to three spaces as opening the list', () => {
    expect(contents(' 1. First\n\n1. Second\n\nDone.')).toEqual([' 1. First\n\n1. Second', 'Done.'])
  })

  it('scans a long run of open brackets in linear time', () => {
    // A scan that restarts at every `[` does about 2e10 steps here and runs
    // past the test timeout; the substring search does 2e5.
    const raw = '['.repeat(200_000) + '\n\nTail.'
    expect(contents(raw)).toEqual(['['.repeat(200_000), 'Tail.'])
  })

  it('stops at an HTML tag after prose, but not at an autolink', () => {
    expect(contents('A.\n\nIntro <details>\n\nhidden')).toEqual(['A.', 'Intro <details>\n\nhidden'])
    expect(contents('See <https://example.com>.\n\nB.')).toEqual(['See <https://example.com>.', 'B.'])
    expect(contents('A.\n\n<![CDATA[\n\nx\n\n]]>')).toEqual(['A.', '<![CDATA[\n\nx\n\n]]>'])
    expect(contents('A.\n\n\\[\nx\n\ny\n\\]')).toEqual(['A.', '\\[\nx\n\ny\n\\]'])
  })

  it('splits a list off once an unindented paragraph follows it', () => {
    expect(contents('- a\n- b\n\nAfter the list.')).toEqual(['- a\n- b', 'After the list.'])
  })

  it('keeps a bordered table with the paragraph above it', () => {
    expect(contents('Intro.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nAfter.')).toEqual([
      'Intro.\n\n| a | b |\n|---|---|\n| 1 | 2 |',
      'After.',
    ])
  })

  it('splits a paragraph off before a list that starts after it', () => {
    expect(contents('Intro.\n\n- a\n- b')).toEqual(['Intro.', '- a\n- b'])
  })

  it('keeps an indented continuation with the block above it', () => {
    expect(contents('- item\n\n  continued item\n\n      indented code')).toEqual([
      '- item\n\n  continued item\n\n      indented code',
    ])
  })

  it('stops at a fence, display math or HTML line', () => {
    expect(contents('A.\n\nB.\n\n~~~\nx\n\ny')).toEqual(['A.', 'B.', '~~~\nx\n\ny'])
    expect(contents('A.\n\n$$\nx\n\ny')).toEqual(['A.', '$$\nx\n\ny'])
    expect(contents('A.\n\n<details>\n\nhidden\n\n</details>')).toEqual(['A.', '<details>\n\nhidden\n\n</details>'])
    expect(contents('A.\n\nglued ```js\n\nB.')).toEqual(['A.', 'glued ```js\n\nB.'])
  })

  it('leaves a block whole when a reference or footnote binds across blank lines', () => {
    expect(contents('See [x][ref].\n\nMore.\n\n[ref]: https://example.com')).toHaveLength(1)
    expect(contents('Claim[^1].\n\nMore.')).toHaveLength(1)
  })

  it('keeps a markdown block split once a fence opens after it', () => {
    const before = split('A.\n\nB.\n\nC')
    const after = split('A.\n\nB.\n\nC.\n\n```js\nconst x = 1\n\nconst y')
    expect(after.map((b) => [b.type, b.content, b.split])).toEqual([
      ['markdown', 'A.', { from: 1, part: 0 }],
      ['markdown', 'B.', { from: 1, part: 1 }],
      ['markdown', 'C.\n', { from: 1, part: 2 }],
      ['code', 'const x = 1\n\nconst y', undefined],
    ])
    expect(before.map((b) => b.split)).toEqual(after.slice(0, 3).map((b) => b.split))
  })

  it('offsets piece lines from a markdown block that starts after a fence', () => {
    const blocks = split('```js\nx\n```\nA.\n\nB.')
    expect(blocks.map((b) => [b.type, b.content, b.startLine])).toEqual([
      ['code', 'x', 2],
      ['markdown', 'A.', 4],
      ['markdown', 'B.', 6],
    ])
  })
})
