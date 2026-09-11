// Feature: prose-tagged fences soft-wrap; code fences keep horizontal scroll.
//
// A fenced block tagged with a prose language (markdown, md, text, txt,
// plaintext, plain) is a paragraph, and a paragraph is one long source line —
// rendering it under `white-space: pre` turns every paragraph into a
// horizontal scrub (#9199). The fix lives entirely in CodeBlock's two render
// branches:
//
// 1. The highlighted branch passes `options={{ overflow: 'wrap' }}` to
//    PierreCode for prose tags and `undefined` otherwise, so Pierre's own
//    default (`overflow: 'scroll'` from PIERRE_CODE_DEFAULTS) still governs
//    real source code.
// 2. The plain <pre> stand-in swaps `overflow-x-auto` for
//    `whitespace-pre-wrap break-words` on prose tags, mirroring the wraps
//    branch of PlainFilePairFallback, so the stand-in and the highlighted
//    surface agree on line count and the swap stays a restyle.
//
// The reporter is explicit that source-code fences must KEEP scrolling, so the
// inverse assertions (ts stays scroll) are as load-bearing as the wrap ones.

import { describe, it, expect, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import { vi } from 'vitest'

import { CodeBlock } from '../components/CodeBlock'
import { __resetStagingForTests } from '../components/pierreStaging'

// Pierre's real chunk never resolves under vitest, so the mount is stubbed
// (same pattern as CodeBlock.staging.test.tsx). The stub records the options
// each mount received, which is the whole contract under test for the
// highlighted branch.
const pierreCalls: Array<{ options?: { overflow?: string } }> = []
vi.mock('../pierre', () => ({
  PierreCode: ({ file, options }: { file: { contents: string }; options?: { overflow?: string } }) => {
    pierreCalls.push({ options })
    return <div data-testid="pierre-mounted">{file.contents}</div>
  },
}))

const LONG_LINE = 'A markdown paragraph is normally one long source line that would force a horizontal scrollbar under white-space: pre.'

/** The plain stand-in <pre> of the fallback branch (complete={false}). */
function renderFallbackPre(lang?: string) {
  const { container } = render(<CodeBlock code={LONG_LINE} lang={lang} complete={false} />)
  const pre = container.querySelector('pre')
  expect(pre).not.toBeNull()
  return pre as HTMLPreElement
}

describe('CodeBlock: prose fences wrap, code fences keep horizontal scroll', () => {
  beforeEach(() => {
    __resetStagingForTests()
    pierreCalls.length = 0
  })

  describe('fallback branch (plain <pre> stand-in)', () => {
    it('a markdown fence soft-wraps instead of scrolling', () => {
      const pre = renderFallbackPre('markdown')
      expect(pre.className).toContain('whitespace-pre-wrap')
      expect(pre.className).toContain('break-words')
      expect(pre.className).not.toContain('overflow-x-auto')
    })

    it('a ts fence keeps the horizontal scroll', () => {
      const pre = renderFallbackPre('ts')
      expect(pre.className).toContain('overflow-x-auto')
      expect(pre.className).not.toContain('whitespace-pre-wrap')
    })

    it('the prose tag match is case-insensitive', () => {
      const pre = renderFallbackPre('Markdown')
      expect(pre.className).toContain('whitespace-pre-wrap')
      expect(pre.className).not.toContain('overflow-x-auto')
    })

    it('a missing tag stays code (scroll)', () => {
      const pre = renderFallbackPre(undefined)
      expect(pre.className).toContain('overflow-x-auto')
      expect(pre.className).not.toContain('whitespace-pre-wrap')
    })

    it('the wrap swap keeps the stand-in geometry classes', () => {
      // The stand-in must keep matching Pierre's metrics so the highlight
      // swap stays a restyle, not a reflow — only the overflow class changes.
      const pre = renderFallbackPre('markdown')
      for (const cls of ['pierre-plain', 'px-3', 'py-2', 'm-0']) {
        expect(pre.className).toContain(cls)
      }
    })
  })

  describe('highlighted branch (PierreCode options)', () => {
    it('a markdown fence passes overflow wrap to Pierre', () => {
      render(<CodeBlock code={LONG_LINE} lang="markdown" complete />)
      expect(pierreCalls.length).toBeGreaterThan(0)
      expect(pierreCalls.at(-1)?.options).toEqual({ overflow: 'wrap' })
    })

    it('a ts fence passes no options, keeping Pierre default scroll', () => {
      render(<CodeBlock code={LONG_LINE} lang="ts" complete />)
      expect(pierreCalls.length).toBeGreaterThan(0)
      expect(pierreCalls.at(-1)?.options).toBeUndefined()
    })
  })
})
