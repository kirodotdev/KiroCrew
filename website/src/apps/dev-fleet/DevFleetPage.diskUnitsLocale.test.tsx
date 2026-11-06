/**
 * DevFleetPage — disk sizes follow the active language (#28983).
 *
 * The stat card built the worktree disk size as `toFixed(0) + ' GB'`, and a
 * row's detail printed the raw `disk_mb` number beside a catalog `MB`. Every
 * language then showed ungrouped Latin digits and an English unit. Both now go
 * through `fmtUnit`, which groups the digits and words the unit the way the
 * locale does.
 */
import { describe, it, expect, beforeEach, afterEach, afterAll, vi } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
// `/all` for the Russian and German catalogs: `../i18n` registers English only.
import { i18next } from '../../i18n/all'
import { i18nT } from '../../i18n/t'

import DevFleetPage, { __resetDevFleetNoticesForTests } from './DevFleetPage'

const res = (body: unknown) => new Response(JSON.stringify(body), { status: 200 })
// A pinned clock: the row's age is not what these tests read, but it is drawn.
const NOW = new Date('2026-06-01T12:00:00Z')
const UPDATED_AT = Math.floor(NOW.getTime() / 1000) - 3600

const MAIN_ROW = { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main', is_live: true }
const ROW = {
  name: 'wt-a', is_main: false, running: false, has_dist: true, behind: 0,
  branch: 'feat/a', path: '/w/wt-a', last_updated_at: UPDATED_AT,
}

function installFetch(totalMb: number, diskMb: number) {
  vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
    const u = typeof url === 'string' ? url : url instanceof URL ? url.toString() : (url as Request).url
    if (u.includes('/worktree?name=')) return Promise.resolve(res({ branch: 'feat/a', disk_mb: diskMb }))
    if (u.includes('/fleet')) return Promise.resolve(res({ base_branch: 'main', worktrees: [MAIN_ROW, ROW] }))
    if (u.includes('/disk')) return Promise.resolve(res({ total_mb: totalMb }))
    return Promise.resolve(res({}))
  })
}

// The page's toast timers outlive a test; keep them off the real clock (see
// DevFleetPageCoverage.test.tsx for the unhandled error this prevents).
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(NOW)
  __resetDevFleetNoticesForTests()
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})
afterAll(async () => { await i18next.changeLanguage('en') })

describe('DevFleetPage disk sizes', () => {
  it('words the worktree disk total in the active language', async () => {
    await i18next.changeLanguage('ru')
    installFetch(51200, 512)
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getByText('wt-a')).toBeInTheDocument(), { timeout: 4000 })
    // 51200 MB is the same 50 whole GB as before, with the Russian unit.
    await waitFor(() => expect(screen.getByText(/^50\sГБ$/)).toBeInTheDocument())
    expect(screen.queryByText('50 GB')).toBeNull()
  })

  it('groups the digits of a row\'s disk size the way the language does', async () => {
    await i18next.changeLanguage('de')
    installFetch(51200, 12345)
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getByText('wt-a')).toBeInTheDocument(), { timeout: 4000 })
    fireEvent.click(screen.getByLabelText(i18nT('pages.devFleetPage.expand')))
    await waitFor(() => expect(screen.getByText(/12\.345\sMB/)).toBeInTheDocument(), { timeout: 4000 })
    expect(screen.queryByText(/12345/)).toBeNull()
  })
})
