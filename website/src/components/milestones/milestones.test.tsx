/**
 * Explore (RFC "Milestones for New-User Feature Discovery", Ship 1 frontend):
 * nothing shows before load; "Try today" is up to three untried features
 * rotated on the local date, and a done or skipped one drops out; the new-chat
 * page keeps its suggestions and gives up only the last card to a Try today
 * entry; direct send only for `widgets` and `parallel-chats`; each notice shows
 * once; no streak-loss or game wording.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor, cleanup, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { useState, type ReactNode } from 'react'
import type { MilestoneFeatureItem, Milestones } from '../../api/client/featureDiscovery'

vi.mock('../../store/chatSlice', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../../store/chatSlice')>()
  return {
    ...orig,
    // Thunk-shaped stand-ins: the store's thunk middleware returns what they return.
    createSlot: vi.fn(() => () => ({ unwrap: () => Promise.resolve({ key: 'new-slot' }) })),
    // The switch lands: the new chat becomes the active one.
    switchSlot: vi.fn((arg: { key: string }) => (dispatch: (a: unknown) => void) => {
      dispatch(orig.setActiveSlot(arg.key))
      return { unwrap: () => Promise.resolve({}) }
    }),
  }
})

// The foreground create path, emulated over the mocked createSlot: the create
// opens its chat unless the user picked another one meanwhile, and a prefill
// is staged (keyed to the new chat) before it opens, as lifecycle.ts does.
vi.mock('../../lib/createdChatView', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../../lib/createdChatView')>()
  const chat = await import('../../store/chatSlice')
  const { writePrefill } = await import('../../utils/navIntent')
  return {
    ...orig,
    createChatForView: vi.fn(async (dispatch: (a: unknown) => { unwrap: () => Promise<{ key: string }> }, getState: () => { chat: { activeSlot: string | null } }, arg: { prefill?: string }) => {
      const before = getState().chat.activeSlot
      const slot = await dispatch(chat.createSlot(arg as never)).unwrap()
      const opened = getState().chat.activeSlot === before
      if (opened) {
        if (arg?.prefill) writePrefill(slot.key, arg.prefill)
        dispatch(chat.setActiveSlot(slot.key) as never)
      }
      return { slot, opened, shown: opened, from: window.location.pathname }
    }),
  }
})

// WelcomeView reads the theme for its brand mark only.
vi.mock('../../hooks/useTheme', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../hooks/useTheme')>(),
  useTheme: () => ({ colorTheme: 'default', brandWelcome: undefined, brandLogo: undefined }),
}))

import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { store } from '../../store'
import { setActiveSlot, setPendingInput } from '../../store/chatSlice'
import { PREVIEW_CREW_MODE, PREVIEW_EXPLORE, setPreviewFlag } from '../../utils/previewFlags'
import enManual from '../../i18n/locales/en.manual.json'
import WelcomeView from '../WelcomeView'
import { NavigationLeaveGuardProvider, usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../NavigationLeaveGuard'
import MilestonesIndicator, { MilestonesPill } from './MilestonesIndicator'
import MilestonesMenuItem from './MilestonesMenuItem'
import MilestonesSheet from './MilestonesSheet'
import MilestoneNoticeToast, { __resetMilestoneNotices } from './MilestoneNoticeToast'
import { __resetMilestonesRouteMissing, closeMilestonesPanel, earnedBadges, gettingStarted, openMilestonesPanel, tryToday, useExplorePreviewSwitch } from './useMilestones'
import { INTRO_VIDEO_ID, INTRO_VIDEO_POSTER, INTRO_VIDEO_SRC } from './ExploreIntroVideo'
import { existsSync } from 'node:fs'
import path from 'node:path'

function feature(id: string, state: MilestoneFeatureItem['state'], send: 'send' | 'prefill' = 'prefill', kind: 'prompt' | 'route' = 'prompt'): MilestoneFeatureItem {
  return {
    id, state, done_at: state === 'done' ? 1793170000 : null, source: state === 'done' ? 'event' : null, video: null,
    starter: { kind, send, route: kind === 'route' ? '/members?member=mate' : '', agent: '', crew_mode: false },
    doc: 'artifacts.md',
  }
}

const ALL_OPEN = () => [
  feature('widgets', 'open', 'send'),
  feature('scheduled-task', 'open'),
  feature('mate', 'open', 'prefill', 'route'),
  feature('parallel-chats', 'open', 'send'),
  feature('slack', 'open'),
  feature('pr-watch', 'open'),
  feature('crew-mode', 'open'),
]

function payload(over: Partial<Milestones> = {}, items?: MilestoneFeatureItem[]): Milestones {
  return {
    enabled: true,
    features: { done: 1, total: 7 },
    streak: { current: 4, best: 9 },
    groups: [
      { id: 'features', items: items ?? [
        feature('widgets', 'open', 'send'),
        feature('scheduled-task', 'open'),
        feature('mate', 'skipped', 'prefill', 'route'),
        feature('pr-watch', 'done'),
        feature('parallel-chats', 'open', 'send'),
        feature('slack', 'open'),
      ] },
      { id: 'sessions', items: [{ id: 'chats-opened', value: 37, tiers: [10, 100, 1000], tiers_reached_at: { 10: 1793170000 } }] },
      { id: 'streak', items: [{ id: 'streak-best', value: 9, tiers: [3, 7, 30, 100], tiers_reached_at: { 3: 1793000000, 7: 1793100000 } }] },
    ],
    recommended: [],
    notices: [],
    ...over,
  }
}

function Where() {
  const loc = useLocation()
  return <span data-testid="where">{loc.pathname + loc.search}</span>
}

function renderWith(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/chat']}>
          {ui}
          <Where />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

function suggestions() {
  vi.spyOn(api, 'suggestions').mockResolvedValue({ suggestions: ['one', 'two', 'three', 'four', 'five', 'six'], generated_at: 0, stale: false })
}

async function openRow(id: string) {
  act(() => openMilestonesPanel())
  return screen.findByTestId(`milestone-row-${id}`)
}

const prefillNow = () => JSON.parse(sessionStorage.getItem('kirocrew_prefill') ?? 'null') as { slotKey: string; prompt: string } | null

/** The Feature Videos verdicts the mocked server holds, keyed by video id. */
let videoVerdicts: Record<string, { status: 'seen' | 'dismissed'; ts: number }> = {}

beforeEach(() => {
  // The surfaces under test here are the ones the Explore preview turns on.
  setPreviewFlag(PREVIEW_EXPLORE, true)
  sessionStorage.removeItem('kirocrew_prefill')
  store.dispatch(setPendingInput(null))
  store.dispatch(setActiveSlot(null))
  __resetMilestoneNotices()
  __resetMilestonesRouteMissing()
  vi.spyOn(api, 'milestoneEvent').mockResolvedValue({})
  vi.spyOn(api, 'milestoneMarkDone').mockResolvedValue({})
  videoVerdicts = {}
  vi.spyOn(api, 'featureVideoStatus').mockImplementation(async () => ({
    enabled: true, cached: 0, total: 0, downloading: null, release: '', state: { ...videoVerdicts },
  }))
  vi.spyOn(api, 'featureVideoFeedback').mockImplementation(async (id, status) => {
    videoVerdicts[id] = { status, ts: 1 }
    return { ok: true }
  })
})
afterEach(() => {
  setPreviewFlag(PREVIEW_CREW_MODE, false)
  setPreviewFlag(PREVIEW_EXPLORE, false)
  act(() => closeMilestonesPanel())
  cleanup()
  vi.restoreAllMocks()
})

describe('tryToday', () => {
  it('takes three untried features, the same all day, rotating with the local date', () => {
    setPreviewFlag(PREVIEW_CREW_MODE, true)
    const data = payload({}, ALL_OPEN())
    const day = (d: number) => tryToday(data, new Date(2026, 9, d, 9)).map(i => i.id)
    expect(day(30)).toHaveLength(3)
    expect(tryToday(data, new Date(2026, 9, 30, 23, 59)).map(i => i.id)).toEqual(day(30))
    // A new day can start on a Getting started row and give the same picks;
    // over a few days they move on.
    expect(new Set([30, 31, 32].map(d => day(d).join())).size).toBeGreaterThan(1)
    // The Getting started pair lives in its own section, and only quick rows
    // are picked (RFC 8.3), so a week brings each of the other three to the front.
    expect(day(30)).toEqual(expect.not.arrayContaining(['widgets', 'scheduled-task', 'pr-watch', 'crew-mode']))
    expect(new Set(Array.from({ length: 7 }, (_, k) => day(24 + k)[0])).size).toBe(3)
  })

  it('keeps the rest of the day\'s picks when one of them is finished', () => {
    setPreviewFlag(PREVIEW_CREW_MODE, true)
    const items = ALL_OPEN()
    items[0].state = 'done'
    items[1].state = 'done'
    const on = new Date(2026, 10, 8, 9)
    const before = tryToday(payload({}, items), on).map(i => i.id)
    expect(before).toHaveLength(3)
    const finished = before[0]
    const after = tryToday(payload({}, items.map(i => (i.id === finished ? { ...i, state: 'done' as const } : i))), on).map(i => i.id)
    // The other two stay, in order; only the finished one drops out.
    expect(after.slice(0, 2)).toEqual(before.slice(1))
    expect(after).not.toContain(finished)
  })

  it('takes the pair back once Getting started is over', () => {
    const items = ALL_OPEN().slice(0, 5)
    items[0].state = 'done'
    const ids = tryToday(payload({}, items), new Date(2026, 9, 30)).map(i => i.id)
    // The brief row is still untried, so it stays in Getting started.
    expect(ids).toEqual(expect.not.arrayContaining(['scheduled-task']))
    items[1].state = 'skipped'
    expect(gettingStarted(payload({}, items))).toEqual([])
  })

  it('drops done, skipped and unavailable rows, and the Crew Mode row while its preview is off', () => {
    const items = ALL_OPEN()
    items[0].state = 'done'
    items[1].state = 'skipped'
    items[2].state = 'unavailable'
    const ids = tryToday(payload({}, items), new Date(2026, 9, 30)).map(i => i.id)
    expect(ids).toEqual(expect.not.arrayContaining(['widgets', 'scheduled-task', 'mate', 'crew-mode']))
    // Only quick rows are picked, so the two left are parallel-chats and slack.
    expect(ids).toEqual(['parallel-chats', 'slack'])
  })

  it('never picks pr-watch or crew-mode, even when they are the only rows left', () => {
    setPreviewFlag(PREVIEW_CREW_MODE, true)
    const items = ALL_OPEN().map(i => (['pr-watch', 'crew-mode'].includes(i.id) ? i : { ...i, state: 'done' as const }))
    for (let d = 1; d <= 7; d++) expect(tryToday(payload({}, items), new Date(2026, 10, d))).toEqual([])
  })

  it('is empty when the switch is off', () => {
    expect(tryToday({ enabled: false })).toEqual([])
  })
})

describe('before the read answers', () => {
  it('shows no indicator, and the new-chat page keeps all six suggestions', async () => {
    suggestions()
    vi.spyOn(api, 'milestones').mockReturnValue(new Promise(() => {}))
    renderWith(<><MilestonesIndicator /><WelcomeView setInput={vi.fn()} /></>)
    await screen.findByText('six')
    expect(screen.queryByTestId('milestones-indicator')).toBeNull()
    expect(screen.queryByTestId('try-today-entry')).toBeNull()
  })

  it('shows nothing at all when the switch is off', async () => {
    suggestions()
    vi.spyOn(api, 'milestones').mockResolvedValue({ enabled: false })
    renderWith(<><MilestonesIndicator /><WelcomeView setInput={vi.fn()} /></>)
    await screen.findByText('six')
    await act(async () => {})
    expect(screen.queryByTestId('milestones-indicator')).toBeNull()
    expect(screen.queryByTestId('try-today-entry')).toBeNull()
  })
})

describe('top bar', () => {
  it('draws the ring with a dot while Try today has items, and the streak in days', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesIndicator />)
    const button = await screen.findByTestId('milestones-indicator')
    expect(screen.getByTestId('milestones-ring').getAttribute('data-ratio')).toBe((1 / 7).toFixed(3))
    expect(screen.getByTestId('milestones-ring-dot')).toBeTruthy()
    // Visible: what the ring is ("Explore 1/7"), then the smaller streak.
    expect(screen.getByTestId('milestones-count').textContent).toBe('Explore 1/7')
    expect(screen.getByTestId('milestones-streak').textContent).toBe('4-day streak')
    expect(screen.getByTestId('milestones-streak').className).toContain('text-[11px]')
    expect(button.getAttribute('aria-label')).toBe('Explore: 1 of 7 features tried, 4-day streak')
  })

  it('has no dot once every feature is tried, and no streak text at 0, but still names itself', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({ streak: { current: 0, best: 9 } }, [feature('widgets', 'done')]))
    renderWith(<MilestonesIndicator />)
    await screen.findByTestId('milestones-indicator')
    expect(screen.queryByTestId('milestones-ring-dot')).toBeNull()
    expect(screen.queryByTestId('milestones-streak')).toBeNull()
    expect(screen.getByTestId('milestones-count').textContent).toMatch(/^Explore \d+\/\d+$/)
  })

  it('on a phone, keeps the ring and "Explore N/M" but leaves the streak to the panel', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesIndicator compact />)
    await screen.findByTestId('milestones-indicator')
    expect(screen.getByTestId('milestones-count').textContent).toBe('Explore 1/7')
    expect(screen.queryByTestId('milestones-streak')).toBeNull()
  })

  it('on desktop, is its own pill holding one button, and draws nothing when off', async () => {
    const read = vi.spyOn(api, 'milestones').mockResolvedValue({ enabled: false })
    const { unmount } = renderWith(<MilestonesPill />)
    await waitFor(() => expect(read).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByTestId('milestones-pill')).toBeNull()
    unmount()
    read.mockResolvedValue(payload())
    renderWith(<MilestonesPill />)
    const pill = await screen.findByTestId('milestones-pill')
    expect(within(pill).getAllByRole('button')).toHaveLength(1)
    // The streak words fold with the feedback labels on a narrow top bar; the ring stays.
    expect(within(pill).getByTestId('milestones-streak').className).toContain('tb-drop-feedback-label')
  })
})

describe('new-chat page', () => {
  it('keeps the personal suggestions and gives only the LAST card to a Try today entry', async () => {
    suggestions()
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<><WelcomeView setInput={vi.fn()} /><MilestonesSheet /></>)
    const entry = await screen.findByTestId('try-today-entry')
    // The count is the rows Try today lists (two here; Getting started holds the other two).
    expect(entry.textContent).toBe('Try today: 2 new features')
    for (const text of ['one', 'two', 'three', 'four', 'five']) expect(screen.getByText(text)).toBeTruthy()
    expect(screen.queryByText('six')).toBeNull()
    fireEvent.click(entry)
    expect(await screen.findByTestId('milestones-try-today')).toBeTruthy()
  })

  it('replaces nothing once every feature is tried', async () => {
    suggestions()
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({}, [feature('widgets', 'done')]))
    renderWith(<WelcomeView setInput={vi.fn()} />)
    await screen.findByText('six')
    await act(async () => {})
    expect(screen.queryByTestId('try-today-entry')).toBeNull()
  })

  it.each(['/embed/chat', '/popout/chat/open-chat'])('adds no entry in %s', async (path) => {
    suggestions()
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    window.history.pushState({}, '', path)
    try {
      renderWith(<WelcomeView setInput={vi.fn()} />)
      await screen.findByText('six')
      await act(async () => {})
      expect(screen.queryByTestId('try-today-entry')).toBeNull()
    } finally {
      window.history.pushState({}, '', '/')
    }
  })
})

describe('Explore preview', () => {
  it('off: the new-chat page keeps all six suggestions and reads no milestones', async () => {
    setPreviewFlag(PREVIEW_EXPLORE, false)
    suggestions()
    const read = vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<WelcomeView setInput={vi.fn()} />)
    await screen.findByText('six')
    await act(async () => {})
    expect(screen.queryByTestId('try-today-entry')).toBeNull()
    expect(read).not.toHaveBeenCalled()
  })

  it('on: the same page gives its last card to Try today', async () => {
    suggestions()
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<WelcomeView setInput={vi.fn()} />)
    expect(await screen.findByTestId('try-today-entry')).toBeTruthy()
  })
})

describe('"Kiro Crew in 15 seconds" card', () => {
  const openPanel = async () => {
    act(() => openMilestonesPanel())
    return screen.findByTestId('milestones-try-today')
  }

  it('sits at the top of Explore with the bundled clip, and never on the new-chat page', async () => {
    suggestions()
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<><WelcomeView setInput={vi.fn()} /><MilestonesSheet /></>)
    await screen.findByTestId('try-today-entry')
    expect(screen.queryByTestId('explore-intro-video')).toBeNull()
    const list = await openPanel()
    const card = await screen.findByTestId('explore-intro-video')
    expect(card.textContent).toContain('Kiro Crew in 15 seconds')
    expect(card.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(screen.getByTestId('explore-intro-video-watch'))
    const player = screen.getByTestId('explore-intro-video-player')
    expect(player.getAttribute('src')).toBe('/app-assets/feature-videos/kiro-crew-intro.mp4')
    expect(player.getAttribute('poster')).toBe('/app-assets/feature-videos/kiro-crew-intro.jpg')
  })

  it('ships the clip and poster it points at', () => {
    const pub = path.resolve(__dirname, '../../../public')
    expect(existsSync(path.join(pub, INTRO_VIDEO_SRC))).toBe(true)
    expect(existsSync(path.join(pub, INTRO_VIDEO_POSTER))).toBe(true)
  })

  it('is gone for good once closed', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    const { unmount } = renderWith(<MilestonesSheet />)
    await openPanel()
    fireEvent.click(await screen.findByTestId('explore-intro-video-close'))
    await waitFor(() => expect(screen.queryByTestId('explore-intro-video')).toBeNull())
    // Recorded in the Feature Videos state, not in this browser's storage.
    await waitFor(() => expect(api.featureVideoFeedback).toHaveBeenCalledWith(INTRO_VIDEO_ID, 'dismissed', undefined))
    expect(Object.keys(localStorage).filter(k => k.includes('intro'))).toEqual([])
    unmount()
    act(() => closeMilestonesPanel())
    renderWith(<MilestonesSheet />)
    await openPanel()
    expect(screen.queryByTestId('explore-intro-video')).toBeNull()
  })

  it('counts as watched at the end and does not come back', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    const { unmount } = renderWith(<MilestonesSheet />)
    await openPanel()
    fireEvent.click(await screen.findByTestId('explore-intro-video-watch'))
    fireEvent.ended(screen.getByTestId('explore-intro-video-player'))
    await waitFor(() => expect(api.featureVideoFeedback).toHaveBeenCalledWith(INTRO_VIDEO_ID, 'seen', undefined))
    // The player stays up for the rest of this view, even after the record refreshes.
    await act(async () => {})
    expect(screen.getByTestId('explore-intro-video-player')).toBeTruthy()
    unmount()
    act(() => closeMilestonesPanel())
    renderWith(<MilestonesSheet />)
    await openPanel()
    expect(screen.queryByTestId('explore-intro-video')).toBeNull()
  })

  it('stays away when this machine already has a verdict for the clip', async () => {
    videoVerdicts[INTRO_VIDEO_ID] = { status: 'dismissed', ts: 1 }
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    await openPanel()
    await act(async () => {})
    expect(screen.queryByTestId('explore-intro-video')).toBeNull()
  })

  it('shows the card with an error when the saved verdicts cannot be read', async () => {
    vi.mocked(api.featureVideoStatus).mockRejectedValue(new Error('offline'))
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    await openPanel()
    const notice = await screen.findByTestId('explore-intro-video-status-error')
    expect(notice.textContent).toContain(enManual.components.startupVideoModal.media_failed)
    expect(screen.getByTestId('explore-intro-video')).toBeTruthy()
  })

  it('keeps the card up with an error when closing cannot be saved, and Close tries again', async () => {
    vi.mocked(api.featureVideoFeedback).mockRejectedValueOnce(new Error('offline'))
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    await openPanel()
    fireEvent.click(await screen.findByTestId('explore-intro-video-close'))
    const notice = await screen.findByTestId('explore-intro-video-save-error')
    expect(notice.textContent).toContain(enManual.milestones.intro_video_save_failed)
    expect(screen.getByTestId('explore-intro-video')).toBeTruthy()
    fireEvent.click(screen.getByTestId('explore-intro-video-close'))
    await waitFor(() => expect(screen.queryByTestId('explore-intro-video')).toBeNull())
    expect(vi.mocked(api.featureVideoFeedback).mock.calls.map(c => c[1])).toEqual(['dismissed', 'dismissed'])
    expect(videoVerdicts[INTRO_VIDEO_ID]?.status).toBe('dismissed')
  })

  it('shows a failed "watched" save once, without retrying on every frame, and Close saves it', async () => {
    vi.mocked(api.featureVideoFeedback).mockRejectedValueOnce(new Error('offline'))
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    await openPanel()
    fireEvent.click(await screen.findByTestId('explore-intro-video-watch'))
    const player = screen.getByTestId('explore-intro-video-player')
    fireEvent.ended(player)
    await screen.findByTestId('explore-intro-video-save-error')
    fireEvent.ended(player)
    expect(api.featureVideoFeedback).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('explore-intro-video-close'))
    await waitFor(() => expect(screen.queryByTestId('explore-intro-video')).toBeNull())
    expect(vi.mocked(api.featureVideoFeedback).mock.calls.map(c => c[1])).toEqual(['seen', 'seen'])
  })

  it('says so when the clip fails to play', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    await openPanel()
    fireEvent.click(await screen.findByTestId('explore-intro-video-watch'))
    fireEvent.error(screen.getByTestId('explore-intro-video-player'))
    // Through the shared ErrorNotice, not a bare status line.
    expect(screen.getByTestId('explore-intro-video-error').textContent).toContain(enManual.components.startupVideoModal.media_failed)
  })
})

describe('Explore panel', () => {
  it('lists Try today rows with Try it and Mark as done, and Your progress below', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    const row = await openRow('parallel-chats')
    expect(screen.getByTestId('milestones-try-today').contains(row)).toBe(true)
    // Named as a task, not as something already done.
    expect(row.textContent).toContain('Run two chats at once')
    expect(row.textContent).not.toContain('First parallel chats')
    expect(row.textContent).toContain('Try it')
    expect(row.textContent).toContain('Mark as done')
    // Skipped and done rows are not in Try today.
    expect(screen.queryByTestId('milestone-row-mate')).toBeNull()
    expect(screen.queryByTestId('milestone-row-pr-watch')).toBeNull()
    fireEvent.click(within(row).getByText('Mark as done'))
    expect(api.milestoneMarkDone).toHaveBeenCalledWith('parallel-chats', undefined)
    expect(screen.getByTestId('milestones-streak-line').textContent).toBe('4 days · best 9')
    // A bar per achievement, toward its next tier.
    expect(screen.getByTestId('milestone-next-chats-opened').textContent).toBe('37 / 100')
    expect(screen.getByTestId('milestone-next-streak-best').textContent).toBe('9 / 30')
    expect(screen.queryByTestId('milestone-usage-chats-concurrent')).toBeNull()
  })

  it('lets the actions wrap below the text, so a narrow panel never squeezes the text away', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    const row = await openRow('parallel-chats')
    const text = within(row).getByTestId('milestone-row-text')
    expect(text.className).toContain('min-w-[10rem]')
    expect(text.parentElement?.className).toContain('flex-wrap')
    expect(text.parentElement?.contains(within(row).getByTestId('milestone-row-actions'))).toBe(true)
  })

  it('shows Getting started first: a first result and a morning brief', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    act(() => openMilestonesPanel())
    const pair = await screen.findByTestId('milestones-getting-started')
    expect(pair.textContent).toContain('A first finished result')
    expect(pair.textContent).toContain('Set up a morning brief')
    // Your progress opens with the pair, above the streak line.
    expect(pair.compareDocumentPosition(screen.getByTestId('milestones-streak-line')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId('milestones-try-today').contains(screen.getByTestId('milestone-row-widgets'))).toBe(false)
    expect(enManual.milestone.scheduled_task.starter_prompt).toBe('Every morning, give me a brief')
  })

  it('keeps a finished Getting started row with a check, and says where to start when only the pair is left', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({}, [feature('widgets', 'done'), feature('scheduled-task', 'open')]))
    renderWith(<MilestonesSheet />)
    act(() => openMilestonesPanel())
    const done = await screen.findByTestId('milestone-row-widgets')
    expect(done.textContent).not.toContain('Try it')
    expect(screen.getByTestId('milestone-row-scheduled-task').textContent).toContain('Try it')
    expect(screen.getByTestId('milestones-try-today-none')).toBeTruthy()
    expect(screen.queryByTestId('milestones-all-tried')).toBeNull()
  })

  it('puts a badge on the wall per finished tier: quiet for regular, colored for advanced, none yet for the top tier', async () => {
    const usage = (chats: Record<number, number>, streak: Record<number, number>) => payload({
      groups: [
        { id: 'features', items: [feature('slack', 'open')] },
        { id: 'sessions', items: [{ id: 'chats-opened', value: 1200, tiers: [10, 100, 1000], tiers_reached_at: chats }] },
        { id: 'streak', items: [{ id: 'streak-best', value: 31, tiers: [3, 7, 30, 100], tiers_reached_at: streak }] },
      ],
    })
    const data = usage({ 10: 1, 100: 5, 1000: 9 }, { 3: 2, 7: 3, 30: 6 })
    expect(earnedBadges(data).map(b => `${b.rowId}:${b.tier}:${b.kind}`)).toEqual([
      'chats-opened:10:regular', 'streak-best:3:regular', 'streak-best:7:regular', 'chats-opened:100:advanced', 'streak-best:30:advanced',
    ])
    vi.spyOn(api, 'milestones').mockResolvedValue(data)
    renderWith(<MilestonesSheet />)
    act(() => openMilestonesPanel())
    const wall = await screen.findByTestId('milestones-badges')
    expect(within(wall).getByTestId('milestone-badge-chats-opened-100').getAttribute('data-kind')).toBe('advanced')
    expect(within(wall).getByTestId('milestone-badge-streak-best-3').getAttribute('data-kind')).toBe('regular')
    // Earned is earned: a regular badge carries the same earned border, never the grey 'locked' look.
    expect(within(wall).getByTestId('milestone-badge-streak-best-3').className).toContain('border-accent/40')
    expect(within(wall).getByTestId('milestone-badge-streak-best-3').className).not.toContain('text-muted')
    expect(within(wall).queryByTestId('milestone-badge-chats-opened-1000')).toBeNull()
    // Past the last tier the bar stays at it.
    expect(screen.getByTestId('milestone-next-chats-opened').textContent).toBe('1,200 / 1,000')
  })

  it('has no badge wall before the first tier', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({
      groups: [{ id: 'features', items: [feature('slack', 'open')] }, { id: 'sessions', items: [{ id: 'chats-opened', value: 2, tiers: [10, 100, 1000], tiers_reached_at: {} }] }],
    }))
    renderWith(<MilestonesSheet />)
    act(() => openMilestonesPanel())
    await screen.findByTestId('milestones-try-today')
    expect(screen.queryByTestId('milestones-badges')).toBeNull()
  })

  it('lists the longer features once the quick ones are done, instead of saying all tried', async () => {
    setPreviewFlag(PREVIEW_CREW_MODE, true)
    const items = ALL_OPEN().map(i => (['pr-watch', 'crew-mode'].includes(i.id) ? i : { ...i, state: 'done' as const }))
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({}, items))
    renderWith(<MilestonesSheet />)
    act(() => openMilestonesPanel())
    const more = await screen.findByTestId('milestones-more')
    expect(within(more).getByTestId('milestone-row-pr-watch').textContent).toContain('Try it')
    expect(within(more).getByTestId('milestone-row-crew-mode').textContent).toContain('Mark as done')
    expect(screen.queryByTestId('milestones-all-tried')).toBeNull()
  })

  it('says so when every feature is tried', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({}, [feature('widgets', 'done')]))
    renderWith(<MilestonesSheet />)
    act(() => openMilestonesPanel())
    expect((await screen.findByTestId('milestones-all-tried')).textContent).toBe("You've tried every feature here.")
  })

  it('opens at the row a tier notice names', async () => {
    const scrolled: string[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.getAttribute('data-testid') ?? '') }
    try {
      vi.spyOn(api, 'milestones').mockResolvedValue(payload())
      renderWith(<MilestonesSheet />)
      act(() => openMilestonesPanel('chats-opened'))
      await screen.findByTestId('milestone-usage-chats-opened')
      expect(scrolled).toContain('milestone-usage-chats-opened')
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('opens at a finished feature a notice names, read-only, and keeps it out of Try today', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    // pr-watch is done in the default read, so no section lists it.
    act(() => openMilestonesPanel('pr-watch'))
    const reached = await screen.findByTestId('milestones-reached')
    const row = within(reached).getByTestId('milestone-row-pr-watch')
    expect(row.textContent).toContain('First PR watch')
    expect(row.textContent).not.toContain('Try it')
    expect(screen.getByTestId('milestones-try-today').contains(row)).toBe(false)
    // Opened without a row, the extra card is not there.
    act(() => closeMilestonesPanel())
    act(() => openMilestonesPanel())
    await screen.findByTestId('milestones-try-today')
    expect(screen.queryByTestId('milestones-reached')).toBeNull()
  })
})

describe('starter tasks (Try it)', () => {
  const tryIt = (row: HTMLElement) => fireEvent.click(row.querySelector('button:last-of-type') as HTMLElement)

  it('sends directly for widgets', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    tryIt(await openRow('widgets'))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/chat?autoSend=1'))
    expect(store.getState().chat.pendingInput).toBe('Make a widget that shows my calendar for this week')
    // Counted as a click in Explore, the only place a starter runs.
    expect(api.milestoneEvent).toHaveBeenCalledWith('widgets', 'starter_clicked', undefined, 'panel')
  })

  it('only fills the composer for a row off the low-cost list, even when the server says send', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({}, [feature('slack', 'open', 'send')]))
    renderWith(<MilestonesSheet />)
    tryIt(await openRow('slack'))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/chat'))
    // Staged as the new chat's keyed prefill before it opens; never written
    // into the composer after the chat loads, where it would replace typing.
    expect(prefillNow()).toMatchObject({ slotKey: 'new-slot', prompt: 'Connect Crew to my Slack' })
    expect(store.getState().chat.pendingInput).toBeNull()
  })

  it('opens the chat through the foreground create, so Split View steps aside', async () => {
    const { createChatForView } = await import('../../lib/createdChatView')
    vi.mocked(createChatForView).mockClear()
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({}, [feature('slack', 'open')]))
    renderWith(<MilestonesSheet />)
    tryIt(await openRow('slack'))
    await waitFor(() => expect(createChatForView).toHaveBeenCalledTimes(1))
    // No background create (`activate: false` skips lastCreatedActivation, which is what exits Split View).
    expect(vi.mocked(createChatForView).mock.calls[0][2]).not.toHaveProperty('activate')
  })

  it('opens the page for a route row and fills nothing', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({}, [feature('mate', 'open', 'prefill', 'route')]))
    renderWith(<MilestonesSheet />)
    tryIt(await openRow('mate'))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/members?member=mate'))
    expect(store.getState().chat.pendingInput).toBeNull()
  })

  it('seeds nothing when the user picked another chat while the new one was being created', async () => {
    const chat = await import('../../store/chatSlice')
    vi.mocked(chat.createSlot).mockImplementationOnce((() => (dispatch: (a: unknown) => void) => {
      dispatch(chat.setActiveSlot('picked-meanwhile'))
      return { unwrap: () => Promise.resolve({ key: 'new-slot' }) }
    }) as never)
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<MilestonesSheet />)
    tryIt(await openRow('widgets'))
    await waitFor(() => expect(chat.createSlot).toHaveBeenCalled())
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBe('picked-meanwhile')
    expect(store.getState().chat.pendingInput).toBeNull()
    expect(prefillNow()).toBeNull()
  })

  it('runs nothing when the page on screen keeps an unsaved draft', async () => {
    const chat = await import('../../store/chatSlice')
    vi.mocked(chat.createSlot).mockClear()
    function KeepsDraft() { useRegisterNavigationLeaveGuard(() => false); return null }
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<NavigationLeaveGuardProvider><KeepsDraft /><MilestonesSheet /></NavigationLeaveGuardProvider>)
    tryIt(await openRow('scheduled-task'))
    await act(async () => {})
    expect(chat.createSlot).not.toHaveBeenCalled()
    expect(vi.mocked(api.milestoneEvent).mock.calls.filter(c => c[1] === 'starter_clicked')).toHaveLength(0)
  })

  it('asks the page about its draft once: a yes carries over to the new chat', async () => {
    const asked = vi.fn(() => true)
    function HasDraft() { useRegisterNavigationLeaveGuard(asked); usePublishNavigationStake(true); return null }
    vi.spyOn(api, 'milestones').mockResolvedValue(payload())
    renderWith(<NavigationLeaveGuardProvider><HasDraft /><MilestonesSheet /></NavigationLeaveGuardProvider>)
    tryIt(await openRow('scheduled-task'))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/chat'))
    expect(prefillNow()).toMatchObject({ prompt: 'Every morning, give me a brief' })
    expect(asked).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('where').textContent).toBe('/chat')
  })
})

describe('a failed read', () => {
  it('stays silent for a gateway without the route (404)', async () => {
    vi.spyOn(api, 'milestones').mockRejectedValue(new ApiError(404, 'not found'))
    renderWith(<MilestonesIndicator />)
    await waitFor(() => expect(api.milestones).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByTestId('milestones-indicator')).toBeNull()
  })

  it('reads a missing route once: turn ends and new readers add no more 404s', async () => {
    const read = vi.spyOn(api, 'milestones').mockRejectedValue(new ApiError(404, 'not found'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (ui: ReactNode) => (
      <Provider store={store}><QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider></Provider>
    )
    const { rerender } = render(tree(<MilestonesIndicator />))
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1))
    await act(async () => {})
    // What a finished turn does (turnCompletion), then a new reader mounting.
    await act(async () => { await qc.invalidateQueries({ queryKey: ['milestones'] }) })
    rerender(tree(<><MilestonesIndicator /><WelcomeView setInput={vi.fn()} /></>))
    await act(async () => {})
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('does not read again just because a reader remounts after a failure', async () => {
    const read = vi.spyOn(api, 'milestones').mockRejectedValue(new ApiError(500, 'boom'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = (n: number) => (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>{Array.from({ length: n }, (_, i) => <MilestonesIndicator key={i} />)}</MemoryRouter>
        </QueryClientProvider>
      </Provider>
    )
    const { rerender } = render(view(1))
    await screen.findByTestId('milestones-indicator')
    for (let n = 2; n <= 5; n++) rerender(view(n))
    rerender(view(0))
    rerender(view(3))
    await act(async () => {})
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('keeps the indicator and the phone menu row for any other failure, and Explore says so', async () => {
    const Item = ({ children, onSelect, ...rest }: { children?: ReactNode; onSelect?: (e: Event) => void }) => (
      <button type="button" {...rest} onClick={() => onSelect?.(new Event('select'))}>{children}</button>
    )
    vi.spyOn(api, 'milestones').mockRejectedValue(new ApiError(500, 'boom'))
    renderWith(<><MilestonesIndicator /><MilestonesMenuItem Item={Item} /><MilestonesSheet /></>)
    await screen.findByTestId('milestones-indicator')
    fireEvent.click(await screen.findByTestId('mobile-milestones-item'))
    expect(await screen.findByText('Could not load Explore.')).toBeTruthy()
    // Its dismiss button works, and the notice comes back the next time Explore opens.
    fireEvent.click(within(screen.getByTestId('milestones-sheet')).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('Could not load Explore.')).toBeNull()
    act(() => closeMilestonesPanel())
    act(() => openMilestonesPanel())
    expect(await screen.findByText('Could not load Explore.')).toBeTruthy()
  })

  it('keeps showing the last answer while a chat not read yet loads its own', async () => {
    let answer: (m: Milestones) => void = () => {}
    const read = vi.spyOn(api, 'milestones').mockResolvedValueOnce(payload())
    renderWith(<><MilestonesIndicator /><MilestonesSheet /></>)
    await screen.findByTestId('milestones-indicator')
    act(() => openMilestonesPanel())
    await screen.findByTestId('milestones-sheet')
    read.mockImplementationOnce(() => new Promise<Milestones>(r => { answer = r }))
    act(() => { store.dispatch(setActiveSlot('other-chat')) })
    await waitFor(() => expect(read).toHaveBeenCalledWith('dashboard:other-chat'))
    // The new key has no cache yet: the ring and the open panel stay up.
    expect(screen.getByTestId('milestones-indicator')).toBeTruthy()
    expect(screen.getByTestId('milestones-sheet')).toBeTruthy()
    await act(async () => { answer(payload()) })
  })
})

describe('Crew Mode preview', () => {
  it('leaves the Crew Mode row out of the ring while the preview is off', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({ features: { done: 1, total: 7 } }, ALL_OPEN()))
    renderWith(<MilestonesIndicator />)
    await screen.findByTestId('milestones-indicator')
    expect(screen.getByTestId('milestones-ring').getAttribute('data-ratio')).toBe((1 / 6).toFixed(3))
  })
})

describe('Explore preview switch', () => {
  function Shell() {
    const [explore, setExplore] = useState(true)
    useExplorePreviewSwitch(explore)
    return (
      <>
        <button type="button" onClick={() => setExplore(v => !v)}>flip</button>
        {explore && <MilestonesPill />}
        {explore && <MilestonesSheet />}
      </>
    )
  }

  it('reads milestones again when the preview turns back on, and closes the sheet when it turns off', async () => {
    const read = vi.spyOn(api, 'milestones').mockResolvedValue(payload({ features: { done: 0, total: 7 } }, ALL_OPEN()))
    renderWith(<Shell />)
    await screen.findByTestId('milestones-indicator')
    expect(read).toHaveBeenCalledTimes(1)
    act(() => { openMilestonesPanel() })
    fireEvent.click(screen.getByText('flip'))
    expect(screen.queryByTestId('milestones-indicator')).toBeNull()
    // Opening the sheet read again; settle that before counting.
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))
    const before = read.mock.calls.length
    fireEvent.click(screen.getByText('flip'))
    await screen.findByTestId('milestones-indicator')
    // The cached answer is fresh and the read does not re-fetch on mount, so
    // only the switch can bring in what changed while Explore was hidden.
    await waitFor(() => expect(read).toHaveBeenCalledTimes(before + 1))
    expect(screen.queryByTestId('milestones-sheet')).toBeNull()
  })
})

describe('quiet notice', () => {
  it('shows each notice once and records it', async () => {
    const read = vi.spyOn(api, 'milestones').mockResolvedValue(payload({ notices: ['widgets'] }))
    const { unmount } = renderWith(<MilestoneNoticeToast />)
    expect((await screen.findByTestId('milestone-notice')).textContent).toBe('Reached: First widget')
    expect(api.milestoneEvent).toHaveBeenCalledWith('widgets', 'notice_shown', undefined, undefined)
    unmount()
    renderWith(<MilestoneNoticeToast />)
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(screen.queryByTestId('milestone-notice')).toBeNull()
    expect(vi.mocked(api.milestoneEvent).mock.calls.filter(c => c[1] === 'notice_shown')).toHaveLength(1)
  })

  it('merges several into one line, names a tier, and skips ids it has no text for', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({ notices: ['chats-opened:100', 'streak-best:7', 'from-a-newer-gateway'] }))
    renderWith(<MilestoneNoticeToast />)
    expect((await screen.findByTestId('milestone-notice')).textContent).toBe('2 new in Explore')
    // Only what was shown is acknowledged; the unknown id stays for a build that can show it.
    const acked = vi.mocked(api.milestoneEvent).mock.calls.filter(c => c[1] === 'notice_shown').map(c => c[0])
    expect(acked.sort()).toEqual(['chats-opened:100', 'streak-best:7'])
  })

  it('acknowledges nothing when no notice in the read has text here', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({ notices: ['turns-sent:100'] }))
    renderWith(<MilestoneNoticeToast />)
    await waitFor(() => expect(api.milestones).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByTestId('milestone-notice')).toBeNull()
    expect(vi.mocked(api.milestoneEvent).mock.calls.filter(c => c[1] === 'notice_shown')).toHaveLength(0)
  })

  it('names a single tier', async () => {
    vi.spyOn(api, 'milestones').mockResolvedValue(payload({ notices: ['streak-best:7'] }))
    renderWith(<MilestoneNoticeToast />)
    expect((await screen.findByTestId('milestone-notice')).textContent).toBe('Reached: 7-day streak')
  })
})

describe('copy', () => {
  const text = JSON.stringify({
    milestone: (enManual as Record<string, unknown>).milestone,
    milestones: (enManual as Record<string, unknown>).milestones,
  })

  it('never talks about losing a streak', () => {
    expect(text).not.toMatch(/\b(lose|losing|lost|at risk|break|broke|broken|miss|missed|keep your|don't let)\b/i)
  })

  it('uses no game words', () => {
    // Values only: the i18n key names still say `milestone`. "Badges" is the
    // owner's own word for the wall, so it is allowed; trophies and the rest are not.
    const values = text.replace(/"[a-z_]+":/g, '')
    expect(values).not.toMatch(/\b(milestones?|achievements?|quests?|unlock(ed)?|levels?|trophy|trophies|rewards?|XP)\b/i)
  })
})
