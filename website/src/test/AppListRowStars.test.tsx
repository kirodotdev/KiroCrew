/**
 * GitHub star count on Discover list rows.
 *
 * The publisher bakes `stargazersCount` only into git-type third-party rows,
 * so FIELD PRESENCE is the display gate: a row with a sanitized number shows
 * the star badge, a row without the field (every built-in) shows nothing.
 * The count renders compact (`fmtCompact`) and locale-aware; the icon carries
 * an accessible name because icon+digits alone is not self-describing.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import AppListRow from '../components/appstore/AppListRow'
import type { RegistryApp } from '../components/appstore/types'

vi.mock('../lib/electron', () => ({ needsDesktopApp: () => false }))

const noop = () => {}

function renderRow(app: Partial<RegistryApp>) {
  return render(
    <AppListRow
      app={{ name: 'demo-app', displayName: 'Demo App', description: '', version: '1.0.0', author: 'acme', tags: [], ...app } as RegistryApp}
      onOpen={noop}
      onGet={noop}
      onUpdate={noop}
      onEnable={noop}
    />,
  )
}

describe('AppListRow star count', () => {
  it('renders the compact count with an accessible star icon when the field is present', () => {
    renderRow({ stargazersCount: 15300 })
    // en compact form of 15300 is "15.3K".
    expect(screen.getByText('15.3K')).toBeInTheDocument()
    expect(screen.getByLabelText('GitHub stars')).toBeInTheDocument()
  })

  it('renders a plain zero (a real count, distinct from the absent field)', () => {
    renderRow({ stargazersCount: 0 })
    expect(screen.getByText('0')).toBeInTheDocument()
    expect(screen.getByLabelText('GitHub stars')).toBeInTheDocument()
  })

  it('renders no star badge when the field is absent (built-ins never carry it)', () => {
    renderRow({ origin: 'builtin', installed: true } as Partial<RegistryApp>)
    expect(screen.queryByLabelText('GitHub stars')).toBeNull()
  })

  it('renders no star badge for a non-numeric value that slipped past normalize', () => {
    renderRow({ stargazersCount: '999' } as unknown as Partial<RegistryApp>)
    expect(screen.queryByLabelText('GitHub stars')).toBeNull()
    expect(screen.queryByText('999')).toBeNull()
  })
})
