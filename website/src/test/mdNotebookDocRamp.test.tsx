/**
 * The Notes document type ramp: the reading base, the heading ratios, and the
 * one derived value (the inline title) that has to track h1.
 *
 * Two things here are easy to break by accident and invisible when broken:
 *
 * 1. The inline note title renders in the chrome header, where `em` cannot
 *    resolve against the reading column, so it needs absolute px. When that px
 *    was a hardcoded literal it silently fell 5.8px under h1 the moment the
 *    reading base moved. The size is therefore DERIVED, and this pins the
 *    derivation end to end — the constant AND the size the component renders.
 * 2. Heading weight is a table (only h1 is 700), which deliberately does NOT
 *    line up with the h1/h2-vs-h3+ split the chrome uses for its rule and rail.
 *    Serving both from one `n <= 2` expression looks like a tidy-up and quietly
 *    changes h2's weight, so the two are asserted apart.
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import { Preview } from '../apps/md-notebook/Preview'
import { BlockEditor, InlineTitle } from '../apps/md-notebook/BlockEditor'
import {
  DOC_BODY_LINE_HEIGHT,
  DOC_BODY_PX,
  DOC_CODE_EM,
  DOC_CODE_PX,
  DOC_H1_PX,
  DOC_HEADING_EM,
  DOC_HEADING_WEIGHTS,
} from '../apps/md-notebook/constants'

const LEVELS = [1, 2, 3, 4, 5, 6] as const

function renderHeadings() {
  const content = LEVELS.map(n => `${'#'.repeat(n)} Title ${n}`).join('\n\n') + '\n\nBody text.'
  const { container } = render(
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
  return {
    container,
    headings: LEVELS.map(n => screen.getByText(`Title ${n}`)),
  }
}

describe('md-notebook document ramp', () => {
  it('derives the inline title size from the ramp rather than a literal', () => {
    expect(DOC_H1_PX).toBe(DOC_BODY_PX * DOC_HEADING_EM[0])
  })

  it('renders the inline title at h1 size and h1 weight', () => {
    render(<InlineTitle path="Folder/Note.md" onRename={vi.fn()} />)
    const style = screen.getByText('Note').getAttribute('style') || ''
    expect(style).toContain(`font-size: ${DOC_H1_PX}px`)
    expect(style).toContain(`font-weight: ${DOC_HEADING_WEIGHTS[0]}`)
  })

  it('sizes every heading from the ratio table, in em so it tracks the base', () => {
    const { headings } = renderHeadings()
    headings.forEach((el, i) => {
      expect(el.getAttribute('style') || '').toContain(`font-size: ${DOC_HEADING_EM[i]}em`)
    })
  })

  it('keeps the ramp strictly decreasing from h1 to h6', () => {
    // The ratios are data, so an edit can transpose a pair without breaking any
    // render — every heading would simply follow the table down, and the
    // per-level size assertion above would still pass. Pin the ORDER so a
    // deeper heading can never come out larger than a shallower one.
    for (let i = 1; i < DOC_HEADING_EM.length; i += 1) {
      expect(DOC_HEADING_EM[i]).toBeLessThan(DOC_HEADING_EM[i - 1])
    }
    expect(DOC_HEADING_EM).toHaveLength(LEVELS.length)
    expect(DOC_HEADING_WEIGHTS).toHaveLength(LEVELS.length)
  })

  it('weights h1 alone at 700 and every level below it at 600', () => {
    const { headings } = renderHeadings()
    headings.forEach((el, i) => {
      expect(el.getAttribute('style') || '').toContain(
        `font-weight: ${DOC_HEADING_WEIGHTS[i]}`,
      )
    })
    // The weight break sits one level above the chrome's own split, so a single
    // `n <= 2` test cannot serve both. Asserted on the table itself: if someone
    // collapses them, h2 becomes 700 and this fails.
    expect(DOC_HEADING_WEIGHTS[0]).toBe(700)
    expect(DOC_HEADING_WEIGHTS[1]).toBe(600)
  })

  it('breaks the heading chrome one level below the weight break', () => {
    // The weight table and the chrome split are deliberately offset by one: h2
    // takes the RULE but not the 700 weight. Both look like "the top of the
    // document", so collapsing them into a single `n <= 2` expression passes
    // every per-level assertion above while quietly making h2 bold. Derive each
    // boundary from what actually renders, then pin the offset between them.
    const { headings } = renderHeadings()
    const chrome = headings.map(el => el.getAttribute('style') || '')
    chrome.forEach((style, i) => {
      // Longhands, not the shorthand: a `border-bottom` value carrying
      // color-mix() is dropped wholesale by jsdom's strict parser.
      if (i + 1 <= 2) {
        expect(style).toContain('border-bottom-style: solid')
        expect(style).not.toContain('border-left-style: solid')
      } else {
        expect(style).toContain('border-left-style: solid')
        expect(style).not.toContain('border-bottom-style: solid')
      }
    })
    const ruledLevels = chrome.filter(s => s.includes('border-bottom-style: solid')).length
    const heavyLevels = DOC_HEADING_WEIGHTS.filter(w => w === DOC_HEADING_WEIGHTS[0]).length
    expect(heavyLevels).toBe(1)
    expect(ruledLevels).toBe(heavyLevels + 1)
  })

  it('renders body prose at the shared reading base', () => {
    const { container } = renderHeadings()
    const root = container.querySelector('.mdnb-note') as HTMLElement | null
    expect(root).not.toBeNull()
    const style = root?.getAttribute('style') || ''
    expect(style).toContain(`font-size: ${DOC_BODY_PX}px`)
    expect(style).toContain(`line-height: ${DOC_BODY_LINE_HEIGHT}`)
  })

  it('defaults an edited block to the same reading base as the rendered view', () => {
    // A block editor that opens at a different size makes the text jump on
    // click, which is what the shared constant exists to prevent.
    render(
      <BlockEditor
        initial="Body text."
        onCommit={vi.fn()}
        onCancel={vi.fn()}
        onSplit={vi.fn()}
      />,
    )
    const style = screen.getByRole('textbox').getAttribute('style') || ''
    expect(style).toContain(`font-size: ${DOC_BODY_PX}px`)
    expect(style).toContain(`line-height: ${DOC_BODY_LINE_HEIGHT}`)
  })

  it('resolves fenced code to the same size as inline code', () => {
    // The fenced surfaces cannot use `em` (their own <pre> is the box being
    // sized), so they carry px. Both spellings must land on one value or a
    // fenced block renders smaller than the inline code beside it.
    expect(DOC_CODE_PX).toBe(DOC_BODY_PX * DOC_CODE_EM)
  })

  it('renders inline code below body prose, in em so it tracks the base', () => {
    render(
      <Preview
        content="Body with `inline code` in it."
        onToggleCheckbox={vi.fn()}
        editRange={null}
        onStartEdit={vi.fn()}
        onCommitEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSplitEdit={vi.fn()}
      />,
    )
    const style = screen.getByText('inline code').getAttribute('style') || ''
    expect(style).toContain(`font-size: ${DOC_CODE_EM}em`)
    expect(DOC_CODE_EM).toBeLessThan(1)
  })
})
