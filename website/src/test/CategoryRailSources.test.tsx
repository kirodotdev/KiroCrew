import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import CategoryRail, { type SourceRow } from '../components/appstore/CategoryRail'

const CURATED: SourceRow = { name: 'internal', label: 'Internal apps', count: 4, builtin: false, review: 'curated' }
const COMMUNITY: SourceRow = { name: 'community', label: 'Community apps', count: 9, builtin: false, review: 'community' }
const PLAIN: SourceRow = { name: 'mine', label: 'My registry', count: 1, builtin: false }
const BUILTIN: SourceRow = { name: '__builtin__', label: 'Built-in', count: 7, builtin: true }

function renderRail(sources: SourceRow[]) {
  return render(
    <CategoryRail
      categories={[]}
      total={0}
      selected="all"
      onSelect={vi.fn()}
      sources={sources}
      onAddSource={vi.fn()}
    />,
  )
}

describe('CategoryRail SOURCES review tier', () => {
  it('marks a curated source as reviewed by the team', () => {
    renderRail([CURATED])
    expect(screen.getByLabelText('Reviewed by the Kiro Crew team')).toBeInTheDocument()
    expect(screen.getByTitle('Reviewed by the Kiro Crew team')).toBeInTheDocument()
  })

  it('marks a community source as not vetted', () => {
    renderRail([COMMUNITY])
    expect(
      screen.getByLabelText('Community-listed, not vetted by the Kiro Crew team'),
    ).toBeInTheDocument()
    expect(
      screen.getByTitle('Community-listed, not vetted by the Kiro Crew team'),
    ).toBeInTheDocument()
  })

  it('does not read a community source as first-party or reviewed', () => {
    renderRail([COMMUNITY])
    expect(screen.queryByLabelText('First-party')).toBeNull()
    expect(screen.queryByLabelText('Reviewed by the Kiro Crew team')).toBeNull()
  })

  it('shows the community tier as VISIBLE text, not only on hover', () => {
    // These rows are non-interactive divs: a touch user has no hover and a
    // keyboard user cannot focus them, so a title-only claim reaches neither.
    renderRail([COMMUNITY])
    expect(screen.getByText('Not vetted ·')).toBeInTheDocument()
  })

  it('shows the curated tier as VISIBLE text too', () => {
    renderRail([CURATED])
    expect(screen.getByText('Team reviewed ·')).toBeInTheDocument()
  })

  it('names and draws a tier the SAME way the registries card does', () => {
    // The two surfaces list the same sources. Reading "Team reviewed" with a
    // shield on one and "Reviewed" with a different check on the other left a
    // reader unable to tell whether it was the same stamp. Both words now come
    // from `components.appstore.registryTier`, which is what this asserts: the
    // rail renders the card's own string, not a shorter synonym.
    renderRail([CURATED])
    expect(screen.getByText('Team reviewed ·')).toBeInTheDocument()
  })

  it('keeps the first-party check off a curated registry', () => {
    // `BadgeCheck` means built-in. Giving it to a curated external registry too
    // blurred two different claims — shipped with Kiro Crew, versus reviewed by
    // its team — so curated takes the card's shield instead.
    renderRail([BUILTIN, CURATED])
    expect(screen.getByLabelText('First-party')).toBeInTheDocument()
    expect(screen.getByLabelText('Reviewed by the Kiro Crew team')).toBeInTheDocument()
  })

  it('claims nothing for a source with no review tier', () => {
    // Inventing reassuring hover text for an unreviewed source would be the
    // over-claim this change removes.
    renderRail([PLAIN])
    expect(screen.getByText('My registry')).toBeInTheDocument()
    expect(screen.queryByTitle(/Kiro Crew team/)).toBeNull()
  })

  it('shows the display label, not the registry id', () => {
    renderRail([CURATED])
    expect(screen.getByText('Internal apps')).toBeInTheDocument()
    expect(screen.queryByText('internal')).toBeNull()
  })

  it('renders rows in the order given, built-in first', () => {
    // The rail renders what useAppsData ordered; asserting it here pins that the
    // component adds no sort of its own.
    renderRail([BUILTIN, CURATED, PLAIN, COMMUNITY])
    const labels = screen
      .getAllByText(/^(Built-in|Internal apps|My registry|Community apps)$/)
      .map(n => n.textContent)
    expect(labels).toEqual(['Built-in', 'Internal apps', 'My registry', 'Community apps'])
  })
})
