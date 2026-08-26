import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * CommonMark's emphasis flanking rules (commonmark/commonmark-spec#650) refuse to
 * close a `**` that is preceded by punctuation and followed by a letter. CJK prose
 * hits that on every `**粗体（括号）。**后续` — and unlike English it cannot insert
 * a space after the `**` to disambiguate, because the space is visible and wrong.
 *
 * These assert the rendered DOM, not the plugin's presence, so they fail if the
 * plugin is dropped OR ordered wrongly relative to remark-gfm.
 */
const renderMd = (src: string) => render(<MarkdownRenderer content={src} />)

describe('CJK-friendly emphasis', () => {
  it('bolds a run ending in ideographic punctuation', () => {
    for (const [src, text] of [
      ['**中文文本（带括号）。**这句子继续也没问题。', '中文文本（带括号）。'],
      ['**日本語の文章（括弧付き）。**この文が続きます。', '日本語の文章（括弧付き）。'],
      ['**중요 안내(괄호 포함)**를 참고. ', '중요 안내(괄호 포함)'],
      ['**重要提示（Important Notice）：**请注意。', '重要提示（Important Notice）：'],
    ] as const) {
      const { container } = renderMd(src)
      expect(container.querySelector('strong')?.textContent).toBe(text)
      expect(container.textContent).not.toContain('**')
    }
  })

  it('italicises a run ending in ideographic punctuation', () => {
    const { container } = renderMd('*这是斜体文字（带括号）。*这句子继续。')
    expect(container.querySelector('em')?.textContent).toBe('这是斜体文字（带括号）。')
    expect(container.textContent).not.toContain('*')
  })

  it('strikes through a run ending in ideographic punctuation', () => {
    // Needs the gfm-strikethrough companion, which must run AFTER remark-gfm.
    const { container } = renderMd('~~删除的文字（带括号）。~~这个句子是正确的。')
    expect(container.querySelector('del')?.textContent).toBe('删除的文字（带括号）。')
    expect(container.textContent).not.toContain('~~')
  })

  it('handles the full-width closing bracket as the boundary', () => {
    const { container } = renderMd('**中文（括号）**后续')
    expect(container.querySelector('strong')?.textContent).toBe('中文（括号）')
  })

  // ── unchanged behaviour ──
  it('leaves ASCII emphasis exactly as CommonMark specifies', () => {
    const { container } = renderMd('**bold** and *italic* and ~~struck~~')
    expect([...container.querySelectorAll('strong,em,del')].map((n) => n.textContent)).toEqual([
      'bold',
      'italic',
      'struck',
    ])
  })

  it('still does NOT emphasise an intra-word ASCII underscore run', () => {
    const { container } = renderMd('snake_case_name stays plain')
    expect(container.querySelector('em')).toBeNull()
    expect(container.textContent).toContain('snake_case_name')
  })

  it('leaves a lone asterisk that opens nothing as literal text', () => {
    const { container } = renderMd('2 * 3 = 6')
    expect(container.querySelector('em')).toBeNull()
    expect(container.textContent).toContain('2 * 3 = 6')
  })

  it('does not touch emphasis markers inside a code span or fence', () => {
    const { container } = renderMd('`**中文（括号）。**` 和\n\n```\n**中文（括号）。**\n```\n')
    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toContain('**中文（括号）。**')
  })
})
