/**
 * Heading styling in the Notes app's markdown preview.
 *
 * These tests exist to pin ONE property that is easy to break by accident: the
 * heading TEXT colour must never come from `--accent`. Tinting heading text with
 * the theme accent looks appealing and passes on most built-in themes, but a
 * theme pack may set `--accent` to any value the allowlist in
 * `hooks/themeCss.ts` accepts — including a colour indistinguishable from its
 * own `--bg` — which turns every heading unreadable. The accent therefore
 * appears only as chrome: a rule under h1/h2, a rail beside h3-h6, where a
 * low-contrast accent degrades to faint decoration instead of lost content.
 *
 * A future change that moves the accent onto the text should fail here.
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import { Preview } from '../apps/md-notebook/Preview'
import {
  HEADING_FG,
  HEADING_RAIL,
  HEADING_RAIL_INDENT,
  HEADING_RULE_SOFT,
  HEADING_RULE_STRONG,
} from '../apps/md-notebook/constants'
import { MDNB_CSS } from '../apps/md-notebook/styles'

const LEVELS = [1, 2, 3, 4, 5, 6] as const

/** Render a note whose body is one heading per level, plus a paragraph. */
function renderHeadings() {
  const content = LEVELS.map(n => `${'#'.repeat(n)} Title ${n}`).join('\n\n') + '\n\nBody text.'
  render(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={null}
      onStartEdit={vi.fn()}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
  return LEVELS.map(n => screen.getByText(`Title ${n}`))
}

describe('md-notebook heading tokens', () => {
  it('never sources heading text colour from the accent', () => {
    // The invariant, asserted on the token itself so it holds regardless of how
    // faithfully the test environment parses CSS custom properties.
    expect(HEADING_FG).toBe('var(--text-strong)')
    expect(HEADING_FG).not.toContain('accent')
  })

  it('builds every chrome token from the accent', () => {
    // Conversely, the decoration MUST follow the theme — that is the feature.
    // The tokens now point at custom properties, so the chain is asserted in two
    // links: token -> property, then property -> var(--accent), mixed toward
    // transparent. Breaking either link silently detaches the chrome from the
    // theme, which no rendered assertion would catch.
    const props = [
      ['--mdnb-heading-rule-strong', HEADING_RULE_STRONG],
      ['--mdnb-heading-rule-soft', HEADING_RULE_SOFT],
      ['--mdnb-heading-rail', HEADING_RAIL],
    ] as const
    for (const [prop, token] of props) {
      expect(token).toBe(`var(${prop})`)
      const declared = new RegExp(`${prop}\\s*:\\s*color-mix\\(in srgb,\\s*var\\(--accent\\)[^;}]*transparent\\)`)
      expect(MDNB_CSS).toMatch(declared)
    }
  })

  it('declares the chrome properties on the class the preview root carries', () => {
    // The properties are inherited, so they must be declared on an ancestor of
    // the headings — `Preview` puts `.mdnb-note` on its root for exactly this.
    expect(MDNB_CSS).toContain('.mdnb-note{')
  })
})

describe('md-notebook/Preview headings', () => {
  it('renders one element per heading level', () => {
    const els = renderHeadings()
    els.forEach((el, i) => expect(el.tagName).toBe(`H${i + 1}`))
  })

  it('colours every level with the strong text token, not the accent', () => {
    for (const el of renderHeadings()) {
      const style = el.getAttribute('style') || ''
      expect(style).toContain('--text-strong')
      // The accent may appear in this element's chrome, but never as `color:`.
      expect(style).not.toMatch(/(^|;)\s*color:[^;]*--accent/)
    }
  })

  it('rules h1 and h2 underneath and rails h3-h6 beside', () => {
    const [h1, h2, h3, h4, h5, h6] = renderHeadings()
    for (const el of [h1, h2]) {
      const style = el.getAttribute('style') || ''
      expect(style).toContain('border-bottom-style: solid')
      expect(style).not.toContain('border-left-style')
    }
    for (const el of [h3, h4, h5, h6]) {
      const style = el.getAttribute('style') || ''
      expect(style).toContain('border-left-style: solid')
      expect(style).not.toContain('border-bottom-style')
    }
    // h1 carries the heavier of the two rules.
    expect(h1.getAttribute('style')).toContain('border-bottom-width: 2px')
    expect(h2.getAttribute('style')).toContain('border-bottom-width: 1px')
  })

  it('hangs the rail in the gutter so heading text stays flush with body text', () => {
    const [h1, h2, h3] = renderHeadings()
    // h1/h2 rule underneath, so they must not be pulled sideways at all.
    for (const el of [h1, h2]) {
      expect(el.getAttribute('style') || '').not.toContain('-10px')
    }
    // The four margin longhands serialize back into the `margin` shorthand, so
    // the offset is asserted on the composed value rather than on `margin-left`.
    expect(h3.getAttribute('style') || '').toContain(`4px -${HEADING_RAIL_INDENT}px`)
  })

  it('leaves body text unstyled by the heading rules', () => {
    renderHeadings()
    const body = screen.getByText('Body text.')
    const style = body.getAttribute('style') || ''
    expect(style).not.toContain('border-left')
    expect(style).not.toContain('border-bottom')
  })
})
