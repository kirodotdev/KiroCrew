/**
 * Test: the startup feature video yields the whole launch to any other startup
 * interruption.
 *
 * `startupVideoGate.test.ts` pins the policy as a predicate. This file pins the
 * WIRING through the real App shell, which is where the interesting failure lives:
 * the gate can be perfectly correct and still be fed a reading taken before the
 * changelog has decided, and the user sees two dialogs stacked on their first
 * screen.
 *
 * Every negative case here is paired with the positive control in the first test.
 * Without it a "does not appear" assertion proves nothing — the video could be
 * absent because the harness never lets it open at all, and the suite would go
 * green while the feature was entirely dead.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import App from '../App'
import { resetStartupVideoLaunchGuardForTests } from '../components/startupVideoGate'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))

const VERSION = '0.9.0'

// `vi.mock` is hoisted above the module body, so a value both the factory and the
// assertions need has to come through `vi.hoisted`. `statusOverride` is mutable
// because the /api/status fetch lands AFTER mount and writes the same slice, so a
// fixed payload would overwrite whatever shape a test set up.
const { statusOverride } = vi.hoisted(() => ({
  statusOverride: { value: {} as Record<string, unknown> },
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockImplementation(async () => ({
      uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
      platform: 'linux', version: VERSION,
      update_available: false, update_can_apply: false, update_check_status: 'succeeded',
      ...statusOverride.value,
    })),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 0, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    changelog: vi.fn().mockResolvedValue({ content: '' }),
    setAutoUpdate: vi.fn().mockResolvedValue({}),
    // A first run that is already complete, so onboarding claims nothing.
    themeBoot: vi.fn().mockResolvedValue({
      onboarded: true, import_onboarded: true, privacy_acked: true,
    }),
    // Governance off by default; the share entry is not what this file measures.
    dashboardConfig: vi.fn().mockResolvedValue({ social_share_enabled: false }),
    featureVideoNext: vi.fn(),
    featureVideoFeedback: vi.fn().mockResolvedValue({ ok: true }),
    featureVideoProbe: vi.fn().mockResolvedValue({ ok: true }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const mockedApi = vi.mocked(api)

const clip = {
  id: 'vid-1',
  feature: 'startup-videos',
  title: 'Feature videos',
  description: 'A short clip introduces each new feature.',
  src: '/app-assets/feature-videos/placeholder.mp4',
  poster: '/app-assets/feature-videos/placeholder.jpg',
  duration_s: 10,
  source: 'local' as const,
}

const startupVideo = () => screen.queryByTestId('startup-video')

/**
 * Let the boot fetches, the changelog decision and the gate effect all land.
 *
 * Several turns rather than one: the open path is a CHAIN — theme boot resolves,
 * which flips `themeBootReady`, which lets the gate effect run, which mounts the
 * modal, which fires its own query. Each link lands on its own turn, so a single
 * flush samples part-way along it and a "does not open" assertion can pass before
 * the video ever had the chance to appear.
 */
async function settle() {
  await waitFor(() => expect(screen.getByTestId('chat-page')).toBeTruthy())
  for (let i = 0; i < 10; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 10)) })
  }
}

beforeEach(() => {
  localStorage.clear()
  resetStartupVideoLaunchGuardForTests()
  statusOverride.value = {}
  // App's boot path still reaches `fetch` directly in places the `api` mock does
  // not cover, so a permissive stub stands in for the network. The clip's
  // reachability is NOT one of those places any more -- it goes through
  // `api.featureVideoProbe`, answered below.
  vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 200 })))
  mockedApi.featureVideoNext.mockReset()
  mockedApi.featureVideoNext.mockResolvedValue({
    video: clip, enabled: true, download_enabled: true,
  } as never)
  mockedApi.featureVideoProbe.mockReset()
  mockedApi.featureVideoProbe.mockResolvedValue({ ok: true } as never)
  mockedApi.featureVideoFeedback.mockClear()
  mockedApi.chatSlots.mockResolvedValue([] as never)
  // Restore the completed-first-run boot. The first-run cases below override this per
  // case, and without a reset that override leaks into every later test.
  mockedApi.themeBoot.mockResolvedValue({
    onboarded: true, import_onboarded: true, privacy_acked: true,
  } as never)
  mockedApi.changelog.mockResolvedValue({ content: '' } as never)
  // Same version as last seen: no release notes to show, so the launch is free.
  localStorage.setItem('mc-last-version', VERSION)
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('startup video sequencing', () => {
  it('POSITIVE CONTROL: opens on a launch with nothing else claiming the screen', async () => {
    // Everything below depends on this passing. If this test ever goes red, the
    // "does not open" tests in this file stop meaning anything.
    renderWithProviders(<App />, { route: '/chat' })
    await settle()
    await waitFor(() => expect(startupVideo()).toBeInTheDocument())
    expect(screen.getByText(clip.title)).toBeInTheDocument()
  })

  it('does not open when the changelog claimed this launch', async () => {
    // A version bump with notes to show — the changelog opens, so the video must
    // wait for the next launch rather than queue behind it.
    localStorage.setItem('mc-last-version', '0.8.0')
    mockedApi.changelog.mockResolvedValue({
      content: `## [${VERSION}]\n- something shipped\n`,
    } as never)

    renderWithProviders(<App />, { route: '/chat' })
    await settle()

    // The changelog really is up: without this the test could pass because
    // nothing at all rendered.
    await waitFor(() => expect(screen.getByText(/something shipped/)).toBeInTheDocument())
    expect(startupVideo()).not.toBeInTheDocument()
  })

  it('does not even ASK for a video when the changelog claimed the launch', async () => {
    // Cost, not just appearance: a launch the video cannot use must not spend a
    // request finding out what it would have shown.
    localStorage.setItem('mc-last-version', '0.8.0')
    mockedApi.changelog.mockResolvedValue({
      content: `## [${VERSION}]\n- something shipped\n`,
    } as never)

    renderWithProviders(<App />, { route: '/chat' })
    await settle()
    await waitFor(() => expect(screen.getByText(/something shipped/)).toBeInTheDocument())
    expect(mockedApi.featureVideoNext).not.toHaveBeenCalled()
  })

  it('stays shut in the window BEFORE the changelog has decided', async () => {
    // The race this whole mechanism exists for. Theme boot resolves first and the
    // screen is empty, so "nothing is showing" is true and yet totally
    // uninformative: the release notes are one unresolved fetch away. A gate that
    // reads only the live conditions opens here, and the user gets both dialogs.
    localStorage.setItem('mc-last-version', '0.8.0')
    let releaseChangelog: (value: { content: string }) => void = () => {}
    mockedApi.changelog.mockImplementation(
      () => new Promise<{ content: string }>(resolve => { releaseChangelog = resolve }),
    )

    renderWithProviders(<App />, { route: '/chat' })
    await settle()

    // Mid-window: boot is done, the notes are still in flight.
    expect(startupVideo()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoNext).not.toHaveBeenCalled()

    // The notes land and claim the launch, which is what the window was hiding.
    releaseChangelog({ content: `## [${VERSION}]\n- late notes\n` })
    await waitFor(() => expect(screen.getByText(/late notes/)).toBeInTheDocument())
    expect(startupVideo()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoNext).not.toHaveBeenCalled()
  })

  it('stays shut, and keeps the version unstamped, when the changelog fetch FAILS', async () => {
    // A failure is not an answer. The notes may still be owed, so the baseline must
    // survive for the next launch to retry, and the video must not read a rejected
    // fetch as "no changelog is coming" and open on that guess.
    localStorage.setItem('mc-last-version', '0.8.0')
    mockedApi.changelog.mockRejectedValue(new Error('gateway down'))

    renderWithProviders(<App />, { route: '/chat' })
    await settle()

    expect(startupVideo()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoNext).not.toHaveBeenCalled()
    // The old `finally` stamped this either way, which retired that version's notes
    // permanently after one failed request.
    expect(localStorage.getItem('mc-last-version')).toBe('0.8.0')
  })

  it('stays shut until the slot list is authoritative', async () => {
    // The incognito veto reads `memory_mode` off the ACTIVE slot, and an unloaded
    // list reports no mode at all — which reads as "not incognito". Opening on that
    // is how an incognito session gets asked for a durable verdict, so the gate
    // waits for the store's own `slotsLoaded` flag.
    let releaseSlots: (value: unknown[]) => void = () => {}
    mockedApi.chatSlots.mockImplementation(
      () => new Promise<unknown[]>(resolve => { releaseSlots = resolve }),
    )

    renderWithProviders(<App />, { route: '/chat' })
    await settle()
    expect(startupVideo()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoNext).not.toHaveBeenCalled()

    // Once the list lands the launch is decidable, and this one is free.
    releaseSlots([])
    await waitFor(() => expect(startupVideo()).toBeInTheDocument())
  })

  it('does not open when an update is available', async () => {
    // UpdateFoundModal's own gate. The video is the lowest-priority interruption
    // and yields to it.
    statusOverride.value = { update_available: true, update_can_apply: true }

    renderWithProviders(<App />, { route: '/chat' })
    await settle()
    expect(startupVideo()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoNext).not.toHaveBeenCalled()
  })

  it('does not open when first-run onboarding is still owed', async () => {
    localStorage.removeItem('mc-onboarded')
    mockedApi.themeBoot.mockResolvedValue({
      onboarded: false, import_onboarded: true, privacy_acked: true,
    } as never)

    renderWithProviders(<App />, { route: '/chat' })
    await settle()
    expect(startupVideo()).not.toBeInTheDocument()
  })

  it.each([
    ['agent import', { onboarded: true, import_onboarded: false, privacy_acked: true }],
    // `onboarded: false` is required for coherence: useTheme reads a completed
    // first run as proof the Privacy chapter was passed, so onboarded+unacked is
    // a boot the backend cannot produce.
    ['privacy', { onboarded: false, import_onboarded: true, privacy_acked: false }],
    ['the tour', { onboarded: false, import_onboarded: true, privacy_acked: true }],
  ])('stays shut on a fresh first run that still owes %s', async (_label, boot) => {
    // The commit-ordering race, found independently by two reviewers. The effect that
    // opens the first-run modals runs in the SAME commit that flips `themeBootReady`,
    // but `setShowAgentImport(true)` merely SCHEDULES its update — so a gate reading
    // the derived `showAgentImport` / `showPrivacy` / `showOnboarding` booleans sees
    // `false` and opens the video beside first-run on a brand-new install.
    //
    // The gate therefore reads the AUTHORITATIVE flags those booleans are derived
    // from, which are already correct in that commit.
    localStorage.removeItem('mc-onboarded')
    localStorage.removeItem('mc-import-onboarded')
    localStorage.removeItem('mc-privacy-acked')
    mockedApi.themeBoot.mockResolvedValue(boot as never)

    renderWithProviders(<App />, { route: '/chat' })
    await settle()
    expect(startupVideo()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoNext).not.toHaveBeenCalled()
  })

  it('does not re-open after it has been closed in the same launch', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await settle()
    await waitFor(() => expect(startupVideo()).toBeInTheDocument())

    const { fireEvent } = await import('@testing-library/react')
    fireEvent.click(screen.getByRole('button', { name: /close/i }))
    await waitFor(() => expect(startupVideo()).not.toBeInTheDocument())

    // The verdict POST is still in flight (or may have failed) at this point, so
    // the launch guard — not the server — is what keeps it closed.
    await new Promise(r => setTimeout(r, 60))
    expect(startupVideo()).not.toBeInTheDocument()
  })

  it.each(['incognito', 'temporary'] as const)('skips the video in a %s session', async mode => {
    // A verdict is a durable per-user write; a session that keeps nothing is not
    // asked for one.
    //
    // The slot comes from `api.chatSlots`, not from a preloaded store: that fetch
    // is the authoritative writer and lands AFTER mount, so a preloaded slot is
    // overwritten and the case would silently test a session with no mode at all.
    mockedApi.chatSlots.mockResolvedValue([
      { key: 'chat-1', title: 'private', messages: 0, running: false, memory_mode: mode },
    ] as never)
    const { createTestStore } = await import('./helpers')
    const { setActiveSlot } = await import('../store/chatSlice')
    const store = createTestStore()
    store.dispatch(setActiveSlot('chat-1'))

    renderWithProviders(<App />, { route: '/chat', store })
    await settle()
    expect(startupVideo()).not.toBeInTheDocument()
    expect(mockedApi.featureVideoNext).not.toHaveBeenCalled()
  })
})
