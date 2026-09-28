/**
 * The message font size setting governs the whole conversation surface, not
 * just bubble prose: inline code, code blocks, tables, follow-up chips, the
 * composer and the Compact column width all follow it, each at the ratio it
 * had to body text at the 14px default. These tests pin two things: that each
 * surface is wired to the setting at all, and that the default render is
 * unchanged (every rule reduces to today's value at 14px).
 *
 * jsdom does not resolve `calc()` or custom properties, so the CSS half reads
 * the stylesheet text; the JS half (the width helper) is exercised directly.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'
import LexicalComposerInput from '../components/LexicalComposerInput'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { INPUT_TYPO } from '../components/PasteHighlightLayer'
import { CONTENT_WIDTH } from '../pages/chat/ChatSettings'
import { DEFAULT_MESSAGE_FONT_SIZE, scaleContentWidth } from '../pages/chat/contentWidth'

const FONT_CSS = readFileSync(resolve(process.cwd(), 'src/styles/message-font-size.css'), 'utf-8')

/** Every rule block under the message-font scope, as `{ selector, body }`. */
function scopedRules(): Array<{ selector: string; body: string }> {
  const out: Array<{ selector: string; body: string }> = []
  const re = /([^{}]+)\{([^{}]*)\}/g
  let m: RegExpExecArray | null
  while ((m = re.exec(FONT_CSS.replace(/\/\*[\s\S]*?\*\//g, ''))) !== null) {
    out.push({ selector: m[1].trim(), body: m[2].trim() })
  }
  return out
}

describe('message font size scales the conversation surface', () => {
  describe('Compact column width', () => {
    const at = (size: number | undefined, contentWidth: 'compact' | 'comfortable' | 'full' = 'compact') =>
      scaleContentWidth(CONTENT_WIDTH[contentWidth], contentWidth, size)

    it('is unchanged at the default size', () => {
      expect(at(DEFAULT_MESSAGE_FONT_SIZE)).toEqual(CONTENT_WIDTH.compact)
    })

    it('grows with the font size so the column holds the same characters per line', () => {
      // 20/14 of 800 = 1142.86 -> 1143; of 816 = 1165.71 -> 1166
      expect(at(20)).toEqual({ messages: '1143px', input: '1166px' })
      // and shrinks below the default the same way
      expect(at(12)).toEqual({ messages: '686px', input: '699px' })
    })

    it('always yields a plain px length the minimap can parseFloat', () => {
      for (const size of [12, 14, 17, 22]) {
        const { messages, input } = at(size)
        expect(messages).toMatch(/^\d+px$/)
        expect(input).toMatch(/^\d+px$/)
      }
    })

    it('leaves the percentage widths alone at any size', () => {
      expect(at(22, 'comfortable')).toBe(CONTENT_WIDTH.comfortable)
      expect(at(12, 'full')).toBe(CONTENT_WIDTH.full)
    })

    it('leaves the base untouched when no usable size is supplied, never emitting NaNpx', () => {
      // A config assembled without the field (a test double, a caller that
      // predates the setting) must not turn the column into an unparseable width.
      expect(at(undefined)).toBe(CONTENT_WIDTH.compact)
      expect(at(NaN)).toBe(CONTENT_WIDTH.compact)
      expect(at(0)).toBe(CONTENT_WIDTH.compact)
    })

    it('scales the table it is handed, so a caller-supplied width flows through', () => {
      // ChatPage and ChatPane pass their own CONTENT_WIDTH entry; the helper
      // imports no table of its own, which is what lets a test that mocks
      // ChatSettings with a 900px compact width still see 900px at the default.
      expect(scaleContentWidth({ messages: '900px', input: '916px' }, 'compact', 14)).toEqual({ messages: '900px', input: '916px' })
      expect(scaleContentWidth({ messages: '900px', input: '916px' }, 'compact', 21)).toEqual({ messages: '1350px', input: '1374px' })
    })
  })

  describe('stylesheet', () => {
    it('does not pin any scoped surface to an absolute size', () => {
      // A bare `NNpx` font-size inside the scope is exactly the bug this
      // stylesheet used to carry for inline code: a fixed size that the
      // setting cannot move. Every font-size must be em or derived from the var.
      for (const { selector, body } of scopedRules()) {
        const fixed = body.match(/font-size:\s*\d+(\.\d+)?px/)
        expect(fixed, `${selector} { ${body} }`).toBeNull()
      }
    })

    it('scales inline code, code blocks, Pierre surfaces and table cells', () => {
      const selectors = scopedRules().map((r) => r.selector.replace(/\s+/g, ' '))
      expect(selectors).toContain('.mc-message-font-scope.msg-content :not(pre) > code')
      expect(selectors).toContain('.mc-message-font-scope.msg-content pre')
      expect(selectors).toContain('.mc-message-font-scope.msg-content .pierre-surface')
      expect(selectors).toContain('.mc-message-font-scope.msg-content th')
      expect(selectors).toContain('.mc-message-font-scope.msg-content td')
    })

    it('re-anchors the table so the cell em ratios resolve against the setting', () => {
      // MarkdownRenderer's <table> carries Tailwind `text-sm`: a fixed 14px
      // that, left alone, becomes the base for every `em` in th/td, so the
      // cells would keep their ratio to 14px rather than to the bubble.
      const table = scopedRules().find((r) => r.selector.replace(/\s+/g, ' ') === '.mc-message-font-scope.msg-content table')
      expect(table?.body).toMatch(/font-size:\s*inherit/)
      // `text-sm` also fixes line-height at 1.25rem; the table re-derives it
      // from the setting (20/14) and td, whose own `text-sm` sets it again,
      // hands it back to the table so rows do not tighten as text grows.
      expect(table?.body).toMatch(/line-height:\s*calc\(var\(--mc-message-font-size, 14px\) \* 1\.4286\)/)
      const td = scopedRules().find((r) => r.selector.replace(/\s+/g, ' ') === '.mc-message-font-scope.msg-content td')
      expect(td?.body).toMatch(/line-height:\s*inherit/)
    })

    it('hands Pierre both the size and the line-height, so the stand-in swap stays height-neutral', () => {
      const pierre = scopedRules().find((r) => r.selector.includes('.pierre-surface'))
      expect(pierre?.body).toMatch(/--diffs-font-size:\s*calc\(var\(--mc-message-font-size, 14px\) \* 0\.9286\)/)
      expect(pierre?.body).toMatch(/--diffs-line-height:\s*calc\(var\(--mc-message-font-size, 14px\) \* 1\.4286\)/)
      const plain = scopedRules().find((r) => r.selector.includes('.pierre-plain'))
      expect(plain?.body).toMatch(/line-height:\s*calc\(var\(--mc-message-font-size, 14px\) \* 1\.4286\)/)
    })

    it('every var-derived rule reduces to the pre-setting value at the 14px default', () => {
      // 14 * 0.9286 = 13.0004 and 14 * 1.4286 = 20.0004: the old 13px code and
      // 20px line-height, to within a hundredth of a pixel.
      const factors = [...FONT_CSS.matchAll(/var\(--mc-message-font-size, 14px\) \* ([\d.]+)/g)].map((m) => Number(m[1]))
      expect(factors.length).toBeGreaterThan(0)
      for (const f of factors) {
        const atDefault = 14 * f
        expect(Math.abs(atDefault - Math.round(atDefault))).toBeLessThan(0.01)
      }
    })

    it('defines the chip and composer classes against the setting var', () => {
      const chip = scopedRules().find((r) => r.selector === '.mc-message-font-chip')
      const input = scopedRules().find((r) => r.selector === '.mc-message-font-text')
      expect(chip?.body).toMatch(/font-size:\s*calc\(var\(--mc-message-font-size, 14px\) \* 0\.9286\)/)
      expect(input?.body).toMatch(/font-size:\s*var\(--mc-message-font-size, 14px\)/)
    })
  })

  describe('surfaces outside the bubble', () => {
    it('follow-up chips carry the chip class and no fixed size', () => {
      render(<FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} />)
      const chip = screen.getByRole('button', { name: /Alpha/ })
      expect(chip.className).toContain('mc-message-font-chip')
      expect(chip.className).not.toMatch(/text-\[\d+px\]/)
    })

    it('the composer typography constant sizes from the setting, not a Tailwind step', () => {
      expect(INPUT_TYPO.split(' ')).toContain('mc-message-font-text')
      expect(INPUT_TYPO.split(' ')).not.toContain('text-sm')
    })

    it('the Lexical editor and its placeholder overlay both carry the composer typography hook', () => {
      // index.css applies the coarse-pointer 16px floor by `[data-composer-typo]`.
      // ChatInput's textarea and the paste-highlight mirror carry it; the
      // Lexical path has to as well or a touch device gets WebKit focus-zoom
      // back below 16px. The placeholder is an overlay <div>, not a real
      // `::placeholder`, so it needs the same hook to stay metric-identical.
      render(
        <LexicalComposerInput
          value=""
          blocks={[]}
          onChange={() => {}}
          onBlocksChange={() => {}}
          onSend={() => {}}
          ariaLabel="Message input"
          placeholder="Write a message"
        />,
      )
      expect(screen.getByRole('textbox')).toHaveAttribute('data-composer-typo')
      expect(screen.getByText('Write a message')).toHaveAttribute('data-composer-typo')
    })
  })

  describe('inline reference chips', () => {
    it('the mono path chip is a <code> the inline-code rule already reaches, at its stock size', async () => {
      // `.mc-message-font-scope.msg-content :not(pre) > code` (0,2,1) beats the
      // chip's Tailwind `text-sm` (0,1,0), so the chip scales with inline code
      // without carrying a class of its own; outside the scope it stays 14px.
      const { container } = render(<MarkdownRenderer content={'see `/Volumes/workplace/KiroCrew/README.md` for details'} />)
      const code = await waitFor(() => {
        const el = container.querySelector('code')
        expect(el).not.toBeNull()
        return el!
      })
      expect(code.className).toContain('text-sm')
      expect(code.className).not.toMatch(/mc-message-font/)
    })

    it('the bordered forge URL chip is a <span>, so it carries the scoped marker class', async () => {
      // A <span> is not reached by the inline-code rule. The marker scales it
      // only under `.mc-message-font-scope`; the fixed `text-[13px]` stays for
      // the ~30 non-chat MarkdownRenderer hosts, some of which mount inside the
      // chat page where the setting var is published.
      const { container } = render(<MarkdownRenderer content={'opened https://github.com/kirodotdev/KiroCrew/pull/12665 today'} />)
      const chip = await waitFor(() => {
        const el = container.querySelector('span.mc-md-ref-chip')
        expect(el).not.toBeNull()
        return el!
      })
      expect(chip.className).toContain('text-[13px]')
      const rule = scopedRules().find((r) => r.selector.replace(/\s+/g, ' ') === '.mc-message-font-scope.msg-content .mc-md-ref-chip')
      expect(rule?.body).toMatch(/font-size:\s*0\.9286em/)
      expect(FONT_CSS).not.toMatch(/^\.mc-md-ref-chip\s*\{/m)
    })
  })
})
