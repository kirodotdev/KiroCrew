/**
 * Test: the post-update changelog modal says when its notes are not this build's.
 *
 * Reported against 0.8.0-insider.1: the modal opened headed `v0.8.0-insider.1`
 * and showed the 0.7.1 notes, a release the reader had already run for a week,
 * with nothing to say they were not what the new build changed. Notes are only
 * written when a stable release ships, so every prerelease and dev build is in
 * that position; the selection (`isNewSection`) is right to show them, the modal
 * was wrong not to label them.
 *
 * The same report called the modal "very tiny", so its size is pinned here too:
 * a viewport-bounded column wide enough for a real release's section, which does
 * not change width when the full changelog is opened.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { i18nT } from '../i18n/t'
import { renderWithProviders } from './helpers'
import type { RootState } from '../store'
import App from '../App'

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

// The running version drives both the preloaded store and the /api/status reply
// that lands after mount, so the fetch confirms the case instead of replacing it.
const { running, changelog } = vi.hoisted(() => ({
  running: { value: '0.8.0-insider.1' },
  changelog: { value: '' },
}))

const CHANGELOG = [
  '# Changelog',
  '',
  '## [0.7.1] - 2026-09-24',
  '- patch note from 0.7.1',
  '',
  '## [0.7.0] - 2026-09-15',
  '- minor note from 0.7.0',
  '',
].join('\n')

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockImplementation(async () => ({
      uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
      version: running.value, update_available: false, update_check_status: 'succeeded',
    })),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 0, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    changelog: vi.fn().mockImplementation(async () => ({ content: changelog.value })),
    setAutoUpdate: vi.fn().mockResolvedValue({}),
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

const stateFor = (version: string) => {
  running.value = version
  return {
    dashboard: {
      connected: true,
      slots: [],
      approvalMode: 'normal',
      status: { platform: 'linux', version, update_available: false, update_check_status: 'succeeded' },
    } as unknown as RootState['dashboard'],
  }
}

describe('changelog modal labeling', () => {
  beforeEach(() => {
    // A DIFFERENT last-seen version is what opens the modal on mount.
    localStorage.setItem('mc-last-version', '0.7.0')
    changelog.value = CHANGELOG
  })

  it('says a prerelease build is being shown the previous release\'s notes', async () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: stateFor('0.8.0-insider.1') })

    const notice = await screen.findByTestId('changelog-stale-notice')
    expect(notice).toHaveTextContent(
      i18nT('app.release_notes_not_published_yet', { release: '0.8.0', shown: '0.7.1' }),
    )
    // Both versions are named: the release the reader is on, and the one shown.
    expect(notice).toHaveTextContent('0.8.0')
    expect(notice).toHaveTextContent('0.7.1')
    // The notes themselves are unchanged: the selection is right, only the label was missing.
    expect(screen.getByTestId('changelog-notes')).toHaveTextContent('patch note from 0.7.1')
    expect(screen.getByTestId('changelog-notes')).not.toHaveTextContent('minor note from 0.7.0')
  })

  it('names the NEWEST shown release when several sections are shown', async () => {
    // A reader skipping a release sees both sections, each under its own heading;
    // the notice must name the latest of them, not whichever came last.
    localStorage.setItem('mc-last-version', '0.6.0')
    renderWithProviders(<App />, { route: '/chat', preloadedState: stateFor('0.8.0-insider.1') })

    const notice = await screen.findByTestId('changelog-stale-notice')
    expect(notice).toHaveTextContent(
      i18nT('app.release_notes_not_published_yet', { release: '0.8.0', shown: '0.7.1' }),
    )
    const notes = screen.getByTestId('changelog-notes')
    expect(notes).toHaveTextContent('## [0.7.1]')
    expect(notes).toHaveTextContent('## [0.7.0]')
  })

  it('shows a prerelease build its own release\'s notes, unlabelled, when the file has them', async () => {
    // The reported case: an insider build of 0.8.0 whose changelog carries a
    // [0.8.0] section. The selection admits the release its build is a
    // prerelease of, so those notes lead and no "not published yet" claim is made.
    changelog.value = ['## [0.8.0] - 2026-09-27', '- release note from 0.8.0', '', CHANGELOG].join('\n')
    renderWithProviders(<App />, { route: '/chat', preloadedState: stateFor('0.8.0-insider.1') })

    expect(await screen.findByTestId('changelog-notes')).toHaveTextContent('release note from 0.8.0')
    expect(screen.queryByTestId('changelog-stale-notice')).toBeNull()
  })

  it('shows no notice when the running build has its own section', async () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: stateFor('0.7.1') })

    expect(await screen.findByTestId('changelog-notes')).toHaveTextContent('patch note from 0.7.1')
    expect(screen.queryByTestId('changelog-stale-notice')).toBeNull()
  })

  it('treats a build segment on the running release as that release', async () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: stateFor('0.7.1+local') })

    expect(await screen.findByTestId('changelog-notes')).toHaveTextContent('patch note from 0.7.1')
    expect(screen.queryByTestId('changelog-stale-notice')).toBeNull()
  })

  it('is sized for a real release and keeps its width when the full changelog opens', async () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: stateFor('0.7.1') })

    const dialog = await screen.findByTestId('changelog-modal')
    // Viewport-bounded column: only the notes scroll, the header stays put.
    expect(dialog).toHaveClass('max-w-3xl', 'max-h-[85vh]', 'flex', 'flex-col', 'w-full')
    expect(screen.getByTestId('changelog-notes')).toHaveClass('max-h-[60vh]')
    expect(screen.getByTestId('changelog-notes-scroll')).toHaveClass('overflow-y-auto')

    fireEvent.click(screen.getByText(i18nT('app.view_full_changelog')))
    const full = await screen.findByTestId('changelog-full')
    // The full changelog takes the dialog's remaining height; the notes shorten
    // and stop shrinking so the full box cannot be squeezed to nothing.
    expect(full).toHaveClass('flex-1', 'min-h-0')
    expect(screen.getByTestId('changelog-full-scroll')).toHaveClass('overflow-y-auto')
    expect(screen.getByTestId('changelog-notes')).toHaveClass('max-h-[30vh]', 'shrink-0')
    // On a short viewport the fixed rows can use up the dialog's height: the
    // full changelog keeps a floor and the dialog scrolls instead.
    expect(full.parentElement).toHaveClass('min-h-[10rem]')
    expect(dialog).toHaveClass('overflow-y-auto')
    await waitFor(() => expect(dialog).toHaveClass('max-w-3xl'))
    expect(dialog).not.toHaveClass('max-w-md')
    expect(dialog).not.toHaveClass('max-w-2xl')
  })
})
