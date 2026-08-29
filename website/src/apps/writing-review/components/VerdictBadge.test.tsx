/**
 * Contract tests for ``VerdictBadge``.
 *
 * Three code paths in the component correspond to the three verdict
 * levels (red / yellow / green). Each renders a distinct class-token set
 * and a distinct localised label. Both facets are pinned here so a
 * future refactor to a shared class map or a rename of the i18n keys
 * cannot silently break the visual signal (colour) or the accessible
 * label (localised verdict word).
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'

import VerdictBadge from './VerdictBadge'

describe('VerdictBadge', () => {
  it('renders the red verdict with the danger token classes', () => {
    const { container } = render(<VerdictBadge verdict="red" />)
    const badgeSpan = container.querySelector('span')
    expect(badgeSpan).not.toBeNull()
    expect(badgeSpan?.className).toContain('bg-danger-subtle')
    expect(badgeSpan?.className).toContain('text-danger')
    expect(badgeSpan?.className).toContain('border-danger')
    // The label MUST be non-empty — a screen reader hitting a bare
    // pill silhouette would announce nothing.
    expect((badgeSpan?.textContent ?? '').trim().length).toBeGreaterThan(0)
  })

  it('renders the yellow verdict with the warn token classes', () => {
    const { container } = render(<VerdictBadge verdict="yellow" />)
    const badgeSpan = container.querySelector('span')
    expect(badgeSpan?.className).toContain('bg-warn-subtle')
    expect(badgeSpan?.className).toContain('text-warn')
    expect(badgeSpan?.className).toContain('border-warn')
    expect((badgeSpan?.textContent ?? '').trim().length).toBeGreaterThan(0)
  })

  it('renders the green verdict with the ok token classes', () => {
    const { container } = render(<VerdictBadge verdict="green" />)
    const badgeSpan = container.querySelector('span')
    expect(badgeSpan?.className).toContain('bg-ok-subtle')
    expect(badgeSpan?.className).toContain('text-ok')
    expect(badgeSpan?.className).toContain('border-ok')
    expect((badgeSpan?.textContent ?? '').trim().length).toBeGreaterThan(0)
  })

  it('renders each verdict level with a distinct label so users can tell them apart', () => {
    const { container: redContainer } = render(<VerdictBadge verdict="red" />)
    const { container: yellowContainer } = render(<VerdictBadge verdict="yellow" />)
    const { container: greenContainer } = render(<VerdictBadge verdict="green" />)
    const redLabel = redContainer.querySelector('span')?.textContent
    const yellowLabel = yellowContainer.querySelector('span')?.textContent
    const greenLabel = greenContainer.querySelector('span')?.textContent
    // Distinct labels are the accessibility contract — a screen-reader
    // user MUST hear a different word per verdict, and a colour-blind
    // sighted user MUST see a different textual pill.
    expect(new Set([redLabel, yellowLabel, greenLabel]).size).toBe(3)
  })
})
