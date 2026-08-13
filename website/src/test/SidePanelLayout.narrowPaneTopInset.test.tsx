/**
 * The narrow pane keeps a top inset under the tab strip — and owns it alone.
 *
 * On a phone SidePanelLayout drops the desktop header block — the one whose
 * `pb-3` puts 12px between a tab's title and its content — and replaces it with
 * a scrolling pill strip that ends in a drawn `border-b`. With no inset on the
 * pane, every tab's first element (a Card, a stat row, a banner) rendered
 * directly ON that border: measured at 390px, four of Agent Capabilities' seven
 * tabs and most of Developer's nine had a 0px gap.
 *
 * The shell is shared by Agent Capabilities, Developer and Settings, so the
 * inset is one number for all three — which makes the second half of the rule
 * matter as much as the first: a tab that ALSO puts a top margin on its own
 * first element stacks on the inset and lands further down than its siblings
 * (12 + 16 = 28px against 12px). Skills, Steering, Developer > Storage and nine
 * Settings tabs did. A heading that is its tab's root drops the margin outright;
 * a heading that repeats within a tab keeps it and neutralises it with
 * `first:mt-0`, because there the between-sections gap is load-bearing.
 *
 * The root headings drop the margin rather than neutralise it positionally
 * because `SkillsTab` renders `PendingSkillsPanel` above its heading and that
 * panel returns null when nothing is pending — `:first-child` would then track
 * the pending count. (A conditionally rendered `Modal` is NOT such a sibling: it
 * portals to document.body.)
 *
 * The classes are the contract: jsdom computes no geometry, so what is asserted
 * here is that the narrow branch carries the inset, the desktop branch does NOT
 * (its 12px comes from the header, and a second inset would stack), that
 * containment (`fixedContent`) does not drop it, and that no tab re-stacks on
 * top of it. The rendered gap is measured against a real build in
 * website/scripts/capture-side-panel-pane-inset.mjs.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'
import { SettingsSection } from '../components/settings'

const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))

const TABS: SidePanelTab[] = [
  { key: 'form', label: 'Form', icon: null },
  { key: 'archive', label: 'Archive', icon: null, fixedContent: true },
]

function renderPage(opts: { url?: string; fixedContent?: boolean } = {}) {
  return render(
    <MemoryRouter initialEntries={[opts.url ?? '/page']}>
      <SidePanelLayout title="Test" tabs={TABS} fixedContent={opts.fixedContent}>
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

/** Every `pt-*` / `py-*` the pane wrapper carries, at any breakpoint. */
function topInsets(el: HTMLElement): string[] {
  return el.className.split(/\s+/).filter(c => /^(?:[a-z-]+:)?p[ty]-/.test(c))
}

describe('SidePanelLayout — the narrow pane clears the tab strip border', () => {
  beforeEach(() => { mobile.value = false })

  it('insets the pane from the strip border on a phone', () => {
    mobile.value = true
    renderPage()
    expect(topInsets(screen.getByTestId('side-panel-pane'))).toEqual(['pt-3'])
  })

  it('keeps the inset when the tab is contained rather than page-scrolled', () => {
    mobile.value = true
    renderPage({ fixedContent: true })
    expect(topInsets(screen.getByTestId('side-panel-pane'))).toEqual(['pt-3'])
  })

  it('pairs the inset with the border it clears', () => {
    mobile.value = true
    renderPage()
    // The strip block sits beside the SCROLLING COLUMN, not beside the pane —
    // the pane is that column's only child on this branch. The inset only reads
    // as deliberate because the block above draws a line.
    const strip = screen.getByTestId('side-panel-pane').parentElement!.previousElementSibling
    // Token equality, not substring: `toContain('border-b')` also matches inside
    // the neighbouring `border-border`, which makes the assertion vacuous.
    expect(String(strip?.className).split(/\s+/)).toContain('border-b')
  })

  it('leaves the desktop pane flush, since the header already spaces it', () => {
    renderPage()
    expect(topInsets(screen.getByTestId('side-panel-pane'))).toEqual([])
    expect(screen.getByTestId('side-panel-header').className).toContain('pb-3')
  })

  it('is the only owner of that gap: no tab root adds a top margin of its own', async () => {
    // The pane's inset is the ONE number between the strip border and a tab's
    // first element. A tab that also carries `mt-*` on its own first element
    // stacks on it and lands further down than its sibling tabs — which is
    // exactly what Skills and Steering did (12px pane + 16px heading = 28px,
    // against 12px on every tab whose first element is a Card).
    //
    // These headings sit at the ROOT of their tab, with nothing above them, so
    // the margin is dropped outright rather than with `first:mt-0`: `SkillsTab`
    // renders `PendingSkillsPanel` above its heading and that panel returns null
    // when nothing is pending, so `:first-child` would track the pending count.
    //
    // Read from source rather than by rendering: these tabs need the whole
    // query/router/i18n stack to mount, and the assertion is about a class on a
    // specific element, which cardInsetYield.test.tsx already checks this same
    // pair of files for in the same way.
    const { readFile } = await import('node:fs/promises')
    const { join } = await import('node:path')
    for (const file of ['SkillsTab.tsx', 'SteeringTab.tsx', 'PromptsTab.tsx']) {
      const src = await readFile(join(__dirname, '..', 'pages', 'overview', file), 'utf8')
      // Only headings: a top margin deeper in the tab separates two sections and
      // is none of this rule's business.
      const heads = src.match(/<h4 className="[^"]*"/g) ?? []
      const stacked = heads.filter(h => /\b(?:mt|my)-[\d.]/.test(h))
      expect(stacked, `${file}: heading must not add a top margin over the pane inset`)
        .toEqual([])
    }
  })

  it('covers a leading element that is NOT a heading, which the h4 scan cannot see', async () => {
    // `PendingSkillsPanel` renders ABOVE the Skills heading and is the tab's
    // first in-flow element whenever anything is pending, so its own root
    // carried the same 12+16=28px stack — invisible to the scan above (not an
    // h4) and to the capture harness (its fixture returns no pending skills).
    // Anchored to the component's own returned root, so a top margin deeper
    // inside the panel, which separates its sections, stays allowed.
    const { readFile } = await import('node:fs/promises')
    const { join } = await import('node:path')
    const src = await readFile(
      join(__dirname, '..', 'pages', 'overview', 'SkillsTab.tsx'), 'utf8')
    const body = src.slice(src.indexOf('function PendingSkillsPanel()'))
    const root = body.match(/return \(\s*\n\s*<div className="([^"]*)"/)
    expect(root, 'PendingSkillsPanel should still open with a single root div').toBeTruthy()
    expect(root![1].split(/\s+/).filter(c => /^(?:[a-z-]+:)?(?:mt|my)-[\d.]/.test(c)),
      'the panel is the tab\'s first in-flow element; the pane owns that gap')
      .toEqual([])
  })

  it('lets a REPEATED section heading keep its margin only when something is above it', () => {
    // Settings and Developer put many `SettingsSection`s in one tab, where the
    // margin between two sections is load-bearing — so here the margin stays and
    // is neutralised positionally. Asserted on the RENDERED class list: a source
    // regex would pass on `mt-4` alone, which is the bug.
    const { container } = render(<SettingsSection title="Section">body</SettingsSection>)
    const head = container.firstElementChild as HTMLElement
    const classes = head.className.split(/\s+/)
    expect(classes, 'the between-sections margin is still there').toContain('mt-4')
    expect(classes, 'and it is dropped for the leading section, which the pane already spaces')
      .toContain('first:mt-0')
  })

  it('applies the same pairing to Developer > Storage, the one developer tab that stacked', async () => {
    const { readFile } = await import('node:fs/promises')
    const { join } = await import('node:path')
    const src = await readFile(join(__dirname, '..', 'pages', 'LocalStorageDebug.tsx'), 'utf8')
    const lead = src.match(/<h4 className="([^"]*)"/)
    expect(lead, 'LocalStorageDebug should still open with a section heading').toBeTruthy()
    const classes = lead![1].split(/\s+/)
    if (classes.some(c => /^(?:mt|my)-[\d.]/.test(c))) {
      expect(classes, 'a leading heading that keeps a top margin must neutralise it')
        .toContain('first:mt-0')
    }
  })
})
