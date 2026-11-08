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
import { ProseFencesContext, proseFenceSet } from '../components/codeBlockProse'
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

  describe('error-report (the dashboard error->agent prompt tag)', () => {
    // utils/errorReport.prompt.ts fences the diagnostic block as
    // ```error-report. Its `- Message: …` line is one long sentence; under
    // scroll it was clipped at the bubble edge, hiding the very text the user
    // asked the agent to diagnose.
    it('soft-wraps in the fallback branch', () => {
      const pre = renderFallbackPre('error-report')
      expect(pre.className).toContain('whitespace-pre-wrap')
      expect(pre.className).not.toContain('overflow-x-auto')
    })

    it('passes overflow wrap to Pierre in the highlighted branch', () => {
      render(<CodeBlock code={LONG_LINE} lang="error-report" complete />)
      expect(pierreCalls.at(-1)?.options).toEqual({ overflow: 'wrap' })
    })
  })

  describe("a host's own prose tags (ProseFencesContext, ChatEmbed's proseFences)", () => {
    const hosted = (lang: string, complete: boolean, tags = ['ticket-update']) => render(
      <ProseFencesContext.Provider value={proseFenceSet(tags)}>
        <CodeBlock code={LONG_LINE} lang={lang} complete={complete} />
      </ProseFencesContext.Provider>,
    )

    it('a listed tag wraps in both branches, matched without regard to case', () => {
      const { container } = hosted('Ticket-Update', false)
      expect(container.querySelector('pre')!.className).toContain('whitespace-pre-wrap')
      hosted('ticket-update', true)
      expect(pierreCalls.at(-1)?.options).toEqual({ overflow: 'wrap' })
    })

    it('a tag the host did not list keeps the scroll', () => {
      hosted('json', true)
      expect(pierreCalls.at(-1)?.options).toBeUndefined()
    })

    it('without a host list the tag is code, as before', () => {
      render(<CodeBlock code={LONG_LINE} lang="ticket-update" complete />)
      expect(pierreCalls.at(-1)?.options).toBeUndefined()
    })

    it('the host list is trimmed and drops empty tags', () => {
      expect([...proseFenceSet([' Ticket-Update ', '', '  '])]).toEqual(['ticket-update'])
    })
  })

  describe('the scrollbar gutter', () => {
    // Pierre reserves its scrollbar height under the last line even when the
    // block wraps and never scrolls sideways; the override removes that strip.
    const surface = (lang: string) =>
      render(<CodeBlock code={LONG_LINE} lang={lang} complete />).container.querySelector<HTMLElement>('.pierre-surface')!

    it('a wrapped block sets the gutter override to zero', () => {
      expect(surface('markdown').style.getPropertyValue('--diffs-scrollbar-gutter-override')).toBe('0px')
    })

    it('a scrolling block keeps the gutter for its scrollbar', () => {
      expect(surface('ts').style.getPropertyValue('--diffs-scrollbar-gutter-override')).toBe('')
    })
  })
})
