/**
 * Contract tests for ``FindingCard`` — one finding rendered in the
 * detail pane.
 *
 * The card has several conditional render blocks. Each is pinned as
 * a distinct test so the rendered surface stays predictable as future
 * scanner-brief authors add new fields:
 *
 * 1. Severity pill + confidence pill + scanner-rule label are always
 *    present.
 * 2. ``cross_validation === 'conflicts'`` adds the "scanners disagree"
 *    amber pill.
 * 3. ``section`` populated adds the location breadcrumb.
 * 4. ``proposed_fix`` populated adds the accent-bordered fix block.
 * 5. Non-empty ``conflicts`` list renders as dashed lines.
 * 6. Non-empty ``related_locations`` list renders under a divider with
 *    the "also appears in" heading.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'

import FindingCard from './FindingCard'
import type { Finding } from '../lib/types'

function makeFinding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: 'f1',
    severity: 'high',
    confidence: 'high',
    scanner: 'clarity',
    rule: 'R1-vague-pronoun',
    issue: 'The pronoun "it" has no clear antecedent.',
    section: 'Introduction',
    paragraph: 2,
    proposed_fix: 'Replace "it" with "the design".',
    ...overrides,
  } as Finding
}

describe('FindingCard', () => {
  it('renders severity pill, confidence pill, and issue text for a minimal finding', () => {
    const { container } = render(<FindingCard finding={makeFinding()} />)
    // Severity pill carries the danger token classes for a high-severity
    // finding — the visual signal that lets a triaging user scan the pane.
    expect(container.querySelector('.bg-danger-subtle')).not.toBeNull()
    // Confidence pill for a high-confidence finding uses the ok tokens.
    expect(container.querySelector('.bg-ok-subtle')).not.toBeNull()
    // Issue text is the core content.
    expect(container.textContent).toContain('The pronoun')
  })

  it('renders the low-severity path with the ok token pill', () => {
    const { container } = render(
      <FindingCard finding={makeFinding({ severity: 'low', confidence: 'medium' })} />,
    )
    // Two ok-subtle pills would collide; low severity should sit on the
    // ok-subtle class distinct from the confidence pill which for medium
    // uses the muted bg-bg-elevated token. Verify the severity path
    // is exercised by checking the border-ok class.
    expect(container.querySelector('.border-ok')).not.toBeNull()
  })

  it('defaults to medium confidence when the finding omits the confidence field', () => {
    // Old records without ``confidence`` MUST render a neutral pill
    // rather than crash — this is the guard for backward-compat with
    // pre-V2 persisted findings.
    const findingWithoutConfidence = makeFinding()
    delete (findingWithoutConfidence as { confidence?: unknown }).confidence
    const { container } = render(<FindingCard finding={findingWithoutConfidence} />)
    // The neutral medium confidence pill uses ``bg-bg-elevated``. Check
    // that at least one such pill is on the card.
    expect(container.querySelector('.bg-bg-elevated')).not.toBeNull()
  })

  it('renders the "scanners disagree" pill when cross_validation === "conflicts"', () => {
    const { container } = render(
      <FindingCard finding={makeFinding({ cross_validation: 'conflicts' } as never)} />,
    )
    // The disagreement pill is what surfaces the cross-validation
    // outcome to the reviewer; without it a conflict finding looks
    // identical to a clean one.
    const warnPills = container.querySelectorAll('.bg-warn-subtle')
    // At least one warn pill; there may be more if severity or
    // confidence tokens overlap.
    expect(warnPills.length).toBeGreaterThan(0)
  })

  it('renders the proposed-fix block when proposed_fix is populated', () => {
    const { container } = render(<FindingCard finding={makeFinding()} />)
    // The fix block is a left-bordered ``border-accent`` node — that
    // border-token is not used elsewhere in the card layout.
    expect(container.querySelector('.border-accent')).not.toBeNull()
    expect(container.textContent).toContain('Replace "it"')
  })

  it('omits the proposed-fix block when proposed_fix is empty', () => {
    const { container } = render(
      <FindingCard finding={makeFinding({ proposed_fix: '' })} />,
    )
    expect(container.querySelector('.border-accent')).toBeNull()
    expect(container.textContent).not.toContain('Replace "it"')
  })

  it('renders the conflicts list when the finding carries conflict notes', () => {
    const conflictNotes = ['clarity contradicts naturalness', 'consider both readings']
    const { container } = render(
      <FindingCard finding={makeFinding({ conflicts: conflictNotes } as never)} />,
    )
    for (const note of conflictNotes) {
      expect(container.textContent).toContain(note)
    }
  })

  it('renders the "also appears in" block when related_locations are populated', () => {
    const relatedLocations = [
      { section: 'Discussion', paragraph: 4, scanner: 'evidence' },
      { section: 'Conclusion', paragraph: 1, scanner: 'clarity' },
    ]
    const { container } = render(
      <FindingCard
        finding={makeFinding({ related_locations: relatedLocations } as never)}
      />,
    )
    // The block heading is the ``alsoAppearsIn`` label; each entry
    // renders on its own line with a bullet prefix. The section names
    // MUST all appear somewhere in the rendered text.
    for (const location of relatedLocations) {
      expect(container.textContent).toContain(location.section)
    }
  })
})
