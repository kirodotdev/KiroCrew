// The advance notice of the next update, inline under About > Updates.
//
// Contract under test:
// - no newer build and no notice -> nothing is drawn, not even an empty card
// - an announced version -> version, expected date, up to three highlights, the
//   notes link and the takes-effect-on-restart line, labelled "Announced"
// - a build the feed already offers, or the desktop updater has staged -> nothing
//   here: the update card above is the one place that describes it
// - it is a plain section inside the Updates card: never a dialog, and it adds
//   no button of its own (the card's Update or Restart control stays the only one)
// - it promises no time: the line says the update takes effect when it is restarted
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { AboutPanel } from '../pages/settings/AboutPanel'
import { AboutPanelUpcomingUpdate } from '../pages/settings/AboutPanelUpcomingUpdate'
import { safeNotesUrl, upcomingUpdate } from '../pages/settings/upcomingUpdate'
import { i18nT } from '../i18n/t'

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

const ANNOUNCED = {
  state: 'announced' as const,
  version: '0.13.0',
  date: '2026-11-18',
  highlights: ['Faster startup', 'Clearer doctor output', 'Fewer prompts'],
  notes_url: 'https://github.com/kirodotdev/KiroCrew/releases/tag/v0.13.0',
}

function publish(upcoming: unknown) {
  act(() => { store.dispatch(sseStatus({ ...BLANK_STATUS, update_upcoming: upcoming } as never)) })
}

function mount(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>{ui}</MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('AboutPanelUpcomingUpdate', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({}), text: async () => '{}', headers: new Headers({ 'content-type': 'application/json' }),
    })))
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('none: draws nothing when no newer version is announced or offered', () => {
    publish(null)
    mount(<AboutPanelUpcomingUpdate />)
    expect(screen.queryByTestId('upcoming-update')).toBeNull()
  })

  it('none: an older gateway that sends no field draws nothing either', () => {
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
    mount(<AboutPanelUpcomingUpdate />)
    expect(screen.queryByTestId('upcoming-update')).toBeNull()
  })

  it('announced: version, expected date, three highlights, notes link and the restart line', () => {
    publish(ANNOUNCED)
    mount(<AboutPanelUpcomingUpdate />)
    const block = screen.getByTestId('upcoming-update')
    expect(block.getAttribute('data-state')).toBe('announced')
    expect(within(block).getByText('Upcoming update')).toBeTruthy()
    expect(screen.getByTestId('upcoming-update-meta').textContent).toBe('Version 0.13.0 · Expected Nov 18, 2026')
    expect(screen.getByTestId('upcoming-update-state').textContent).toBe('Announced')
    const items = within(screen.getByTestId('upcoming-update-highlights')).getAllByRole('listitem')
    expect(items.map(li => li.textContent)).toEqual(ANNOUNCED.highlights)
    const link = screen.getByTestId('upcoming-update-notes') as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe(ANNOUNCED.notes_url)
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(screen.getByTestId('upcoming-update-takes-effect').textContent)
      .toBe('Once it is released, it takes effect the next time you restart Kiro Crew.')
    // A fact, not a control: no button of its own.
    expect(within(block).queryByRole('button')).toBeNull()
  })

  it('a build the feed offers is not described a second time beside the Update when I restart card', async () => {
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      update_upcoming: null,
      update_available: true,
      update_check_status: 'succeeded',
      update_latest_version: '0.13.0',
      update_latest_version_display: '0.13.0',
    } as never))
    mount(<AboutPanel />)
    expect(await screen.findByText('Update available')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /Update when I restart/ }).length).toBeGreaterThan(0)
    expect(screen.queryByTestId('upcoming-update')).toBeNull()
  })

  it('draws a link only for an https notes URL and omits unknown parts', () => {
    publish({ ...ANNOUNCED, date: '', highlights: [], notes_url: 'javascript:alert(1)' })
    mount(<AboutPanelUpcomingUpdate />)
    expect(screen.getByTestId('upcoming-update-meta').textContent).toBe('Version 0.13.0')
    expect(screen.queryByTestId('upcoming-update-highlights')).toBeNull()
    expect(screen.queryByTestId('upcoming-update-notes')).toBeNull()
  })

  it('has a catalog string for every sentence it shows', () => {
    for (const key of ['title', 'announced', 'highlights', 'notes_link']) {
      const value = i18nT(`pages.settings.aboutPanel.upcoming_update_${key}`)
      expect(value).not.toContain('upcoming_update_')
    }
  })
})

describe('Upcoming update inside the About page', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({}), text: async () => '{}', headers: new Headers({ 'content-type': 'application/json' }),
    })))
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('sits inside the Updates card and is never a dialog', async () => {
    publish(ANNOUNCED)
    mount(<AboutPanel />)
    const block = await screen.findByTestId('upcoming-update')
    expect(block.closest('[role="dialog"], [role="alertdialog"], [aria-modal="true"]')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    const updatesTitle = screen.getByText('Updates')
    const card = updatesTitle.closest('div[class*="card"], section, div')!.parentElement!
    expect(card.contains(block)).toBe(true)
  })

  it('draws nothing in the page when there is no notice', async () => {
    publish(null)
    mount(<AboutPanel />)
    await screen.findByText('Updates')
    expect(screen.queryByTestId('upcoming-update')).toBeNull()
  })
})

describe('upcomingUpdate view model', () => {
  it('maps the gateway record and caps the highlights at three', () => {
    const got = upcomingUpdate({ ...ANNOUNCED, highlights: [...ANNOUNCED.highlights, 'Fourth'] })
    expect(got).toMatchObject({ state: 'announced', version: '0.13.0', date: '2026-11-18', notesUrl: ANNOUNCED.notes_url })
    expect(got?.highlights).toEqual(ANNOUNCED.highlights)
  })

  it('is null when nothing is announced', () => {
    expect(upcomingUpdate(null)).toBeNull()
    expect(upcomingUpdate(undefined)).toBeNull()
    expect(upcomingUpdate({ ...ANNOUNCED, version: '' })).toBeNull()
  })

  it('accepts only https links without whitespace', () => {
    expect(safeNotesUrl('https://example.com/a')).toBe('https://example.com/a')
    expect(safeNotesUrl('http://example.com')).toBe('')
    expect(safeNotesUrl('https://exa mple.com')).toBe('')
    expect(safeNotesUrl('https://')).toBe('')
    expect(safeNotesUrl(undefined)).toBe('')
  })
})
