import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')

// jsdom lacks layout, so VirtuosoMasonry renders nothing; the table view under
// test doesn't use it, but the module still imports it.
vi.mock('@virtuoso.dev/masonry', () => ({
  VirtuosoMasonry: () => <div data-testid="masonry" />,
}))

const mk = (slug: string, name: string, overrides: Partial<Artifact> = {}): Artifact => ({
  slug,
  name,
  kind: 'widget',
  source: 'chat',
  pinned: false,
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-01-01T00:00:00.000000+00:00',
  updated_at: '2026-01-01T00:00:00.000000+00:00',
  ...overrides,
})

// Server order deliberately not alphabetical, not by version, not by date —
// so each sort visibly rearranges, and "default" is distinguishable from all
// of them. Names carry numeric suffixes to prove NATURAL ordering (item-2
// before item-10; byte order would invert them).
const ARTIFACTS = [
  mk('b-item-10', 'item-10', { version: 10, updated_at: '2026-01-02T00:00:00.000000+00:00' }),
  mk('a-item-2', 'item-2', { version: 2, updated_at: '2026-03-01T00:00:00.000000+00:00' }),
  mk('c-alpha', 'alpha', { version: 1, updated_at: '2026-02-01T00:00:00.000000+00:00' }),
]

/** Artifact row names, in DOM order (header row and any non-artifact rows
 * carry none of these names, so mapping + filtering is order-safe). */
function rowNames(): string[] {
  const known = new Set(ARTIFACTS.map((a) => a.name))
  return screen
    .getAllByRole('row')
    .map((r) => [...known].find((n) => r.textContent?.includes(n)) ?? '')
    .filter(Boolean)
}

const header = (name: string) => screen.getByRole('button', { name })

describe('ArtifactsPage table sorting', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.setItem('mc-artifacts-view', 'table')
    localStorage.setItem('mc-artifacts-pinned-only', '0')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: ARTIFACTS })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  })

  it('cycles Name asc → desc → default, with natural numeric ordering', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('alpha')).toBeInTheDocument())
    // Default: server order.
    expect(rowNames()).toEqual(['item-10', 'item-2', 'alpha'])

    // Ascending: natural collation puts item-2 before item-10.
    fireEvent.click(header('Name'))
    expect(rowNames()).toEqual(['alpha', 'item-2', 'item-10'])

    // Descending.
    fireEvent.click(header('Name'))
    expect(rowNames()).toEqual(['item-10', 'item-2', 'alpha'])

    // Third click: back to the server's order.
    fireEvent.click(header('Name'))
    expect(rowNames()).toEqual(['item-10', 'item-2', 'alpha'])
    // And no header advertises a sort any more.
    const sorted = screen
      .getAllByRole('columnheader')
      .filter((th) => th.getAttribute('aria-sort'))
    expect(sorted).toHaveLength(0)
  })

  it('sorts the version column numerically, not lexically', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('alpha')).toBeInTheDocument())
    fireEvent.click(header('Ver'))
    // Numeric: 1 < 2 < 10 (lexical would give 1 < 10 < 2).
    expect(rowNames()).toEqual(['alpha', 'item-2', 'item-10'])
    fireEvent.click(header('Ver'))
    expect(rowNames()).toEqual(['item-10', 'item-2', 'alpha'])
  })

  it('sorts the Updated column chronologically', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('alpha')).toBeInTheDocument())
    fireEvent.click(header('Updated'))
    expect(rowNames()).toEqual(['item-10', 'alpha', 'item-2'])
    fireEvent.click(header('Updated'))
    expect(rowNames()).toEqual(['item-2', 'alpha', 'item-10'])
  })

  it('marks the active column with aria-sort and moves it on a new column click', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('alpha')).toBeInTheDocument())
    fireEvent.click(header('Name'))
    const nameTh = header('Name').closest('th')
    expect(nameTh?.getAttribute('aria-sort')).toBe('ascending')
    fireEvent.click(header('Name'))
    expect(nameTh?.getAttribute('aria-sort')).toBe('descending')
    // Switching columns starts the new one at ascending and clears the old.
    fireEvent.click(header('Kind'))
    expect(nameTh?.getAttribute('aria-sort')).toBeNull()
    expect(header('Kind').closest('th')?.getAttribute('aria-sort')).toBe('ascending')
  })
})
