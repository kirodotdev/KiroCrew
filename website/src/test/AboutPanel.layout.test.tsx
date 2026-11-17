// Settings > About as a whole page.
//
// Contract under test:
// - the sections stand in order: install card, Updates, What's new, Earlier
//   releases, Report a problem
// - Check for updates sits in the install card, beside the version it checks
// - a gateway checkout offers to load an update's changes until a check has run,
//   then lists them, or says none were written
// - an install that updates by command never offers that load
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { AboutPanel } from '../pages/settings/AboutPanel'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { SETTINGS_KEYWORDS } from '../components/commandPalette/settingsKeywords'

const RELEASES = {
  current_version: '0.12.1',
  stale: false,
  releases: [
    { version: '0.12.1', date: '2026-11-09', body: 'Notes for 0.12.1.', is_current: true, in_progress: false },
    { version: '0.12.0', date: '2026-11-05', body: 'Notes for 0.12.0.', is_current: false, in_progress: false },
  ],
}

const BLANK_STATUS = { uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 } as const

/** Routes the panel's requests; `check` answers /api/update/check. */
function stubFetch(check: Record<string, unknown> = {}) {
  const json = (body: unknown) => ({
    ok: true, status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  const spy = vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.includes('/api/releases')) return json(RELEASES)
    if (url.includes('/api/update/check')) return json(check)
    return json({})
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...utils, qc }
}

function setStatus(extra: Record<string, unknown>) {
  act(() => { store.dispatch(sseStatus({ ...BLANK_STATUS, version: '0.12.1', ...extra } as never)) })
}

/** True when `a` comes before `b` in document order. */
const before = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

describe('AboutPanel page layout', () => {
  beforeEach(() => setStatus({}))
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('stacks the install card, Updates, What\u2019s new, Earlier releases and Report a problem in that order', async () => {
    stubFetch()
    mount()
    const current = await screen.findByTestId('releases-current')
    const sections = [
      screen.getByTestId('about-version'),
      screen.getByRole('heading', { name: /^Updates$/ }),
      current,
      screen.getByTestId('releases-earlier'),
      screen.getByRole('button', { name: /report a problem/i }),
    ]
    for (let i = 1; i < sections.length; i++) expect(before(sections[i - 1], sections[i])).toBe(true)
  })

  it('puts Check for updates in the install card, not the Updates card', async () => {
    stubFetch()
    mount()
    const check = await screen.findByTestId('about-check-for-updates')
    const identity = screen.getByTestId('about-version').closest('.card, [class*="card"]')!
    expect(identity.contains(check)).toBe(true)
    expect(before(check, screen.getByRole('heading', { name: /^Updates$/ }))).toBe(true)
  })
})

describe('AboutPanel What you\u2019d get wiring', () => {
  beforeEach(() => setStatus({}))
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('offers a checkout the load, then lists the changes its check returns', async () => {
    stubFetch({
      check_status: 'succeeded', update_available: true, latest_version: '0.13.0', can_apply: true,
      changes: '## [0.13.0] - 2026-11-20\n\n- **Faster**: x',
    })
    setStatus({ update_available: true, update_can_apply: true, update_check_status: 'succeeded', update_latest_version: '0.13.0' })
    mount()
    fireEvent.click(await screen.findByTestId('update-changes-load'))
    await waitFor(() => expect(screen.getByTestId('update-changes')).toHaveTextContent("What you'd get (1 change since your build)"))
    expect(screen.queryByTestId('update-changes-load')).toBeNull()
  })

  it('says a checkout\u2019s check found no changelog entries, rather than that they are hidden', async () => {
    stubFetch({ check_status: 'succeeded', update_available: true, latest_version: '0.13.0', can_apply: true, changes: '' })
    setStatus({ update_available: true, update_can_apply: true, update_check_status: 'succeeded', update_latest_version: '0.13.0' })
    mount()
    fireEvent.click(await screen.findByTestId('update-changes-load'))
    await waitFor(() => expect(screen.getByTestId('update-changes-none'))
      .toHaveTextContent('No changelog entries between your build and this update.'))
    expect(screen.queryByTestId('update-changes-empty')).toBeNull()
  })

  it('offers to load changes again when a newer background check supersedes an empty local check', async () => {
    stubFetch({
      check_status: 'succeeded', update_available: true, latest_version: '0.13.0', can_apply: true,
      changes: '', last_checked_at: 100,
    })
    setStatus({
      update_available: true, update_can_apply: true, update_check_status: 'succeeded',
      update_latest_version: '0.13.0', update_last_checked_at: 100,
    })
    mount()
    fireEvent.click(await screen.findByTestId('update-changes-load'))
    expect(await screen.findByTestId('update-changes-none'))
      .toHaveTextContent('No changelog entries between your build and this update.')
    expect(screen.queryByTestId('update-changes-load')).toBeNull()

    setStatus({
      update_available: true, update_can_apply: true, update_check_status: 'succeeded',
      update_latest_version: '0.13.0', update_last_checked_at: 200,
    })
    expect(screen.queryByTestId('update-changes-none')).toBeNull()
    expect(screen.getByTestId('update-changes-load')).toBeInTheDocument()
  })

  it('never offers the load to an install that updates by command', async () => {
    stubFetch()
    setStatus({
      update_available: true, update_can_apply: false, update_check_status: 'succeeded',
      update_latest_version: '0.13.0', update_command: 'kirocrew update',
    })
    mount()
    expect(await screen.findByTestId('update-changes-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('update-changes-load')).toBeNull()
    // The Updates card names the version from the background check alone.
    expect(screen.getByText(/Kiro Crew 0\.13\.0 is available\./)).toBeInTheDocument()
  })

  it('never offers the load, or says no entries, to a policy install that applies its update by command', async () => {
    // A provider-backed check carries no changelog lines, so an empty result
    // proves nothing about the update's contents.
    stubFetch({ check_status: 'succeeded', update_available: true, latest_version: '0.13.0', can_apply: true })
    setStatus({
      update_available: true, update_can_apply: true, update_check_status: 'succeeded',
      update_latest_version: '0.13.0', update_managed_by: 'command',
    })
    mount()
    expect(await screen.findByTestId('update-changes-empty')).toHaveTextContent(/read them on github/i)
    expect(screen.queryByTestId('update-changes-load')).toBeNull()
    cleanup()

    // The same install after this tab's own check, which returns no changes.
    setStatus({ update_check_status: 'succeeded', update_can_apply: true, update_managed_by: 'command' })
    mount()
    fireEvent.click(await screen.findByTestId('about-check-for-updates'))
    expect(await screen.findByTestId('update-changes-empty')).toHaveTextContent(/read them on github/i)
    expect(screen.queryByTestId('update-changes-none')).toBeNull()
    expect(screen.queryByTestId('update-changes-load')).toBeNull()
  })
})

describe('About in settings search', () => {
  it('lists About and its release notes, with the words people search for', () => {
    const notes = SETTINGS_REGISTRY.find(e => e.id === 'about.release-notes')
    expect(notes).toMatchObject({ tab: 'about', label: 'Release notes', params: { release: 'notes' } })
    expect(SETTINGS_REGISTRY.find(e => e.id === 'about.version')).toMatchObject({ tab: 'about', label: 'Version' })
    expect(SETTINGS_KEYWORDS['about.release-notes']).toEqual(expect.arrayContaining(['changelog', 'releases', "what's new"]))
    expect(SETTINGS_KEYWORDS['about.version']).toContain('version')
  })
})
