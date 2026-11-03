/**
 * DevFleetPage — the UPDATED column follows the active language.
 *
 * The column used to glue English suffixes onto a number (`m + 'm ago'`) and
 * then strip `' ago'` again, so every locale read "5m" / "3d" / "2mo". It now
 * renders `fmtRelative`, which words the age the way the locale does.
 */
import { describe, it, expect, beforeEach, afterEach, afterAll, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
// `/all` for the Japanese catalog: `../i18n` registers English only.
import { i18next } from '../i18n/all'

import DevFleetPage, { __resetDevFleetNoticesForTests } from '../pages/DevFleetPage'

const nowSec = () => Math.floor(Date.now() / 1000)
const row = (name: string, lastUpdatedAt: number) => ({
  name, is_main: false, running: false, has_dist: true, behind: 0,
  branch: `feat/${name}`, path: `/w/${name}`, last_updated_at: lastUpdatedAt,
})

function installFleet(worktrees: Record<string, unknown>[]) {
  vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
    const u = typeof url === 'string' ? url : url instanceof URL ? url.toString() : (url as Request).url
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify({ base_branch: 'main', worktrees })))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 })))
    return Promise.resolve(new Response('{}'))
  })
}

// The page's toast timers outlive a test; keep them off the real clock (see
// DevFleetPageCoverage.test.tsx for the unhandled-error this prevents).
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  // A pinned clock: every age below is measured from this instant.
  vi.setSystemTime(new Date('2026-06-01T12:00:00Z'))
  __resetDevFleetNoticesForTests()
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})
afterAll(async () => { await i18next.changeLanguage('en') })

describe('DevFleetPage UPDATED column', () => {
  it('words the age in the active language', async () => {
    await i18next.changeLanguage('ja')
    installFleet([
      row('wt-mins', nowSec() - 300),
      row('wt-days', nowSec() - 3 * 86400),
    ])
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getByText('wt-mins')).toBeInTheDocument(), { timeout: 4000 })
    expect(screen.getByText('5分前')).toBeInTheDocument()
    expect(screen.getByText('3日前')).toBeInTheDocument()
    // No English unit glue survives under another language.
    expect(screen.queryByText('5m')).toBeNull()
    expect(screen.queryByText('3d')).toBeNull()
  })

  it('reads a timestamp ahead of this clock as now, not as a future time', async () => {
    await i18next.changeLanguage('en')
    installFleet([row('wt-skewed', nowSec() + 600)])
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getByText('wt-skewed')).toBeInTheDocument(), { timeout: 4000 })
    expect(screen.getByText('now')).toBeInTheDocument()
    expect(screen.queryByText(/^in /)).toBeNull()
  })
})
