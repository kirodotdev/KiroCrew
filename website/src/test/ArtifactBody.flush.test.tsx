import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { createRef } from 'react'

// The native artifact body renders markdown through ContentRenderer, which pulls
// in the full markdown pipeline. That is fine here — these tests only assert the
// wrapper's chrome classes, which are computed before any of it runs.
import { ArtifactBodyNative } from '../components/ArtifactBody'

/**
 * Chrome parity between a markdown FILE and a markdown ARTIFACT in the chat side
 * panel.
 *
 * Before `flush`, an artifact in the panel was drawn inside its own
 * `rounded-xl border bg-card` box with `p-5`, nested just inside the panel's own
 * border — so a markdown artifact looked nothing like the markdown file rendered
 * by the tab beside it (MarkdownPanel passes `flush` and lets DetailPanel supply
 * the single layer of reading padding). The full-page route keeps the card,
 * because there the document floats on the page background and the border is
 * what bounds it.
 */
describe('ArtifactBodyNative — flush chrome', () => {
  const base = {
    kind: 'markdown' as const,
    content: '# Hello',
    editing: false,
    onChange: vi.fn(),
  }

  /** The body's outermost element — the scroller that carries the chrome. */
  function renderBody(flush?: boolean) {
    const { container } = render(
      <ArtifactBodyNative {...base} previewRef={createRef<HTMLDivElement>()} flush={flush} />,
    )
    const scroller = container.firstElementChild as HTMLElement
    return { scroller, padded: scroller.firstElementChild as HTMLElement }
  }

  it('keeps the card chrome by default (the full-page route)', () => {
    const { scroller, padded } = renderBody(undefined)
    expect(scroller.className).toContain('border-border')
    expect(scroller.className).toContain('rounded-xl')
    expect(scroller.className).toContain('bg-card')
    // Reading padding lives inside the card on the full page.
    expect(padded.className).toContain('p-5')
  })

  it('drops the border, rounding and inner padding when flush', () => {
    const { scroller, padded } = renderBody(true)
    expect(scroller.className).not.toContain('border-border')
    expect(scroller.className).not.toContain('rounded-xl')
    expect(scroller.className).not.toContain('bg-card')
    // No second layer of padding — DetailPanel's own px-5 py-4 is the only one,
    // which is exactly what a file gets in the same panel.
    expect(padded.className).not.toContain('p-5')
  })

  it('stays scrollable and positioned in both modes', () => {
    // `relative` anchors the inline comment overlay and `overflow-auto` owns the
    // scroll; neither is chrome, so flush must not strip them or comment anchors
    // would land in the wrong place.
    for (const flush of [undefined, true]) {
      const { scroller } = renderBody(flush)
      expect(scroller.className).toContain('relative')
      expect(scroller.className).toContain('overflow-auto')
    }
  })
})
