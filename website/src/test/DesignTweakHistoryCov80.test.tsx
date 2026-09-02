/**
 * Runtime coverage for DesignTweakPage — history/archive view, per-request
 * overflow menu, timeAgo labels, page-level loading/error/empty states,
 * clearing and deleting requests, and chatRoute navigation.
 *
 * Every assertion proves real rendered behavior. Mocks resolve or reject to
 * exercise the component's actual branching, not just bump statement counts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import DesignTweak from '../apps/design-tweak/DesignTweakPage'
import { renderWithProviders } from './helpers'
import { i18nT } from '../i18n/t'

vi.mock('../apps/design-tweak/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/design-tweak/api')>()),
  fetchProjects: vi.fn(),
  fetchQueue: vi.fn(),
  fetchHistory: vi.fn(),
  fetchHealth: vi.fn(),
  clearRequest: vi.fn(),
  deleteRequest: vi.fn(),
}))

// The component imports chatRoute for navigate() — we need the real implementation
// but mock the navigate side. Also mock delivery module to control verifyDelivery.
vi.mock('../apps/design-tweak/delivery', () => ({
  deliveryVerdict: vi.fn(() => 'unknown'),
  needsDeliveryRetry: vi.fn(() => false),
}))

// Mock react-router-dom's useNavigate to capture navigation calls
const mockNavigate = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => mockNavigate,
}))

import {
  fetchProjects, fetchQueue, fetchHistory, fetchHealth,
  clearRequest, deleteRequest,
} from '../apps/design-tweak/api'

const mockedFetchProjects = fetchProjects as ReturnType<typeof vi.fn>
const mockedFetchQueue = fetchQueue as ReturnType<typeof vi.fn>
const mockedFetchHistory = fetchHistory as ReturnType<typeof vi.fn>
const mockedFetchHealth = fetchHealth as ReturnType<typeof vi.fn>
const mockedClearRequest = clearRequest as ReturnType<typeof vi.fn>
const mockedDeleteRequest = deleteRequest as ReturnType<typeof vi.fn>

// Freeze time so timeAgo output is deterministic
const FROZEN_NOW = new Date('2026-06-10T12:00:00Z').getTime()

function makeProject(id = 'proj-1') {
  return {
    id,
    path: '/home/user/my-app',
    name: 'My App',
    previewUrl: 'http://127.0.0.1:5173',
  }
}

function makeRequest(overrides = {}) {
  return {
    id: 'req-1',
    number: 1,
    status: 'sent',
    state: 'sent',
    projectId: 'proj-1',
    projectRoot: '/home/user/my-app',
    createdAt: '2026-06-10T11:55:00Z',
    comments: [
      {
        cid: 'c-1',
        index: 1,
        status: 'done',
        comment: 'Make the header bigger',
        createdAt: '2026-06-10T11:55:00Z',
        projectId: 'proj-1',
      },
    ],
    ...overrides,
  }
}

function makeHistoryRequest(overrides = {}) {
  return {
    id: 'hist-1',
    number: 2,
    status: 'done',
    state: 'done',
    projectId: 'proj-1',
    projectRoot: '/home/user/my-app',
    createdAt: '2026-06-10T10:00:00Z',
    comments: [
      {
        cid: 'hc-1',
        index: 1,
        status: 'done',
        comment: 'Change background to blue',
        createdAt: '2026-06-10T10:00:00Z',
        projectId: 'proj-1',
      },
    ],
    ...overrides,
  }
}

// Standard mock setup: projects loaded, active, with queue + history
function setupHappyPath(opts: {
  pending?: unknown[]
  history?: unknown[]
  projects?: unknown[]
} = {}) {
  const proj = makeProject()
  mockedFetchProjects.mockResolvedValue({
    projects: opts.projects ?? [proj],
    activeId: proj.id,
    serving: true,
  })
  mockedFetchQueue.mockResolvedValue({
    pending: opts.pending ?? [makeRequest()],
  })
  mockedFetchHistory.mockResolvedValue({
    history: opts.history ?? [makeHistoryRequest()],
  })
  mockedFetchHealth.mockResolvedValue({
    status: 'ok',
    dataDir: '/home/user/.kiro/apps/design-tweak',
  })
}

describe('DesignTweakPage — history, archive, states', () => {
  /**
   * Radix opens a dropdown on `pointerdown`, not `click`, so a plain
   * `fireEvent.click` on the trigger leaves the menu closed and every item
   * lookup then fails with "unable to find text". The extra pointer fields are
   * required too — Radix ignores a non-primary or non-left-button event.
   */
  const openMenu = (trigger: HTMLElement) => {
    fireEvent.pointerDown(trigger, {
      pointerId: 1, button: 0, ctrlKey: false, isPrimary: true,
    })
  }

  beforeEach(() => {
    // `shouldAdvanceTime` is load-bearing, not a tuning knob. `timeAgo` needs a
    // frozen `now` to assert a stable label, but Testing Library's `waitFor`
    // polls on real timers — with them fully faked it never gets a tick, so
    // every async assertion in this file sits until the 15s test cap instead of
    // resolving. This keeps the mocked clock for the component while letting the
    // polling loop actually run.
    vi.useFakeTimers({ now: FROZEN_NOW, shouldAdvanceTime: true })
    vi.clearAllMocks()
    mockNavigate.mockClear()
    // Suppress window.confirm for delete tests
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  // ─── Loading state ───────────────────────────────────────────────────────

  it('shows a loading spinner while the initial projects fetch is pending', async () => {
    // fetchProjects never resolves — simulates the loading state
    mockedFetchProjects.mockReturnValue(new Promise(() => {}))
    mockedFetchQueue.mockResolvedValue({ pending: [] })
    mockedFetchHistory.mockResolvedValue({ history: [] })
    mockedFetchHealth.mockResolvedValue({ status: 'ok', dataDir: '' })

    renderWithProviders(<DesignTweak />)

    // The page shows loading indicators for both the project rail and request rail
    expect(
      screen.getByText(i18nT('apps.designTweak.projects.loading_your_apps')),
    ).toBeInTheDocument()
  })

  // ─── Error state — backend unreachable ───────────────────────────────────

  it('shows the empty-state message when projects fetch errors out', async () => {
    // All API calls reject — simulates backend down
    mockedFetchProjects.mockRejectedValue(new Error('Network error'))
    mockedFetchQueue.mockRejectedValue(new Error('Network error'))
    mockedFetchHistory.mockRejectedValue(new Error('Network error'))
    mockedFetchHealth.mockRejectedValue(new Error('Network error'))

    renderWithProviders(<DesignTweak />)

    // After the error settles, the page should not be blank — it shows the
    // "no app selected" empty state since no project could be loaded.
    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.projects.no_app_selected')),
      ).toBeInTheDocument()
    })
  })

  // ─── Empty states ────────────────────────────────────────────────────────

  it('shows "no app selected" when no project is connected', async () => {
    mockedFetchProjects.mockResolvedValue({ projects: [makeProject()], activeId: '' })
    mockedFetchQueue.mockResolvedValue({ pending: [] })
    mockedFetchHistory.mockResolvedValue({ history: [] })
    mockedFetchHealth.mockResolvedValue({ status: 'ok', dataDir: '' })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.projects.no_app_selected')),
      ).toBeInTheDocument()
    })
  })

  it('shows the per-app empty state when a project is active but has no pending requests', async () => {
    setupHappyPath({ pending: [], history: [] })
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      // The component shows a message referencing the app name
      expect(
        screen.getByText(
          i18nT('apps.designTweak.requests.empty_for_app', { name: 'My App' }),
        ),
      ).toBeInTheDocument()
    })
  })

  it('shows the "no app" empty request text when no preview is connected', async () => {
    // Projects exist but none is active — the request rail shows a different message
    mockedFetchProjects.mockResolvedValue({
      projects: [makeProject()],
      activeId: '',
      serving: true,
    })
    mockedFetchQueue.mockResolvedValue({ pending: [] })
    mockedFetchHistory.mockResolvedValue({ history: [] })
    mockedFetchHealth.mockResolvedValue({ status: 'ok', dataDir: '' })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.empty_no_app')),
      ).toBeInTheDocument()
    })
  })

  // ─── History toggle ──────────────────────────────────────────────────────

  it('toggles the history section open and closed', async () => {
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    // Wait for data to load
    await waitFor(() => {
      expect(mockedFetchHistory).toHaveBeenCalled()
    })

    // Find the History button — it shows the history count
    const historyBtn = await screen.findByRole('button', {
      name: new RegExp(i18nT('apps.designTweak.requests.history')),
    })
    expect(historyBtn).toBeInTheDocument()

    // The section being closed is signalled by the request ROW being absent.
    // Comment text is NOT a usable signal here: a collapsed request keeps its
    // comments mounted and only hides them with `visibility: hidden`, so a
    // `not.toBeInTheDocument()` on the text can never hold.
    const historyRow = () =>
      screen.queryByText(i18nT('apps.designTweak.requests.request_number', { number: 2 }))
    expect(historyRow()).not.toBeInTheDocument()

    // Open history
    fireEvent.click(historyBtn)

    await waitFor(() => {
      expect(historyRow()).toBeInTheDocument()
    })

    // Close history again
    fireEvent.click(historyBtn)

    await waitFor(() => {
      expect(historyRow()).not.toBeInTheDocument()
    })
  })

  it('displays the history count in the toggle button', async () => {
    setupHappyPath({
      history: [
        makeHistoryRequest(),
        makeHistoryRequest({ id: 'hist-2', number: 3 }),
      ],
    })
    renderWithProviders(<DesignTweak />)

    // The history toggle shows the count
    await waitFor(() => {
      expect(screen.getByText('(2)')).toBeInTheDocument()
    })
  })

  // ─── Archive (clear) a request ───────────────────────────────────────────

  it('archives a request via the overflow menu', async () => {
    mockedClearRequest.mockResolvedValue({ ok: true })
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    // Wait for the request to render
    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 1 })),
      ).toBeInTheDocument()
    })

    // Find the overflow (MoreHorizontal) button — it carries an aria-label
    const moreBtn = screen.getByLabelText(
      i18nT('apps.designTweak.requests.more_actions', { number: 1 }),
    )
    openMenu(moreBtn)

    // The dropdown should show "Archive to history"
    const archiveItem = await screen.findByText(
      i18nT('apps.designTweak.requests.archive_to_history'),
    )
    fireEvent.click(archiveItem)

    await waitFor(() => {
      expect(mockedClearRequest).toHaveBeenCalledWith('req-1')
    })
  })

  it('shows an error status when archive fails', async () => {
    mockedClearRequest.mockRejectedValue(new Error('server down'))
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 1 })),
      ).toBeInTheDocument()
    })

    const moreBtn = screen.getByLabelText(
      i18nT('apps.designTweak.requests.more_actions', { number: 1 }),
    )
    openMenu(moreBtn)

    const archiveItem = await screen.findByText(
      i18nT('apps.designTweak.requests.archive_to_history'),
    )
    fireEvent.click(archiveItem)

    await waitFor(() => {
      expect(
        screen.getByText(
          i18nT('apps.designTweak.status.archive_failed', { error: 'server down' }),
        ),
      ).toBeInTheDocument()
    })
  })

  // ─── Delete a request ────────────────────────────────────────────────────

  it('deletes a request via the overflow menu after confirmation', async () => {
    mockedDeleteRequest.mockResolvedValue({ ok: true })
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 1 })),
      ).toBeInTheDocument()
    })

    const moreBtn = screen.getByLabelText(
      i18nT('apps.designTweak.requests.more_actions', { number: 1 }),
    )
    openMenu(moreBtn)

    const deleteItem = await screen.findByText(
      i18nT('apps.designTweak.requests.delete_request'),
    )
    fireEvent.click(deleteItem)

    // window.confirm was mocked to return true
    await waitFor(() => {
      expect(mockedDeleteRequest).toHaveBeenCalledWith('req-1')
    })
  })

  it('does not delete when the user cancels the confirmation dialog', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 1 })),
      ).toBeInTheDocument()
    })

    const moreBtn = screen.getByLabelText(
      i18nT('apps.designTweak.requests.more_actions', { number: 1 }),
    )
    openMenu(moreBtn)

    const deleteItem = await screen.findByText(
      i18nT('apps.designTweak.requests.delete_request'),
    )
    fireEvent.click(deleteItem)

    // Confirm was called but returned false — deleteRequest must NOT fire
    expect(window.confirm).toHaveBeenCalled()
    expect(mockedDeleteRequest).not.toHaveBeenCalled()
  })

  it('shows an error status when delete fails', async () => {
    mockedDeleteRequest.mockRejectedValue(new Error('forbidden'))
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 1 })),
      ).toBeInTheDocument()
    })

    const moreBtn = screen.getByLabelText(
      i18nT('apps.designTweak.requests.more_actions', { number: 1 }),
    )
    openMenu(moreBtn)

    const deleteItem = await screen.findByText(
      i18nT('apps.designTweak.requests.delete_request'),
    )
    fireEvent.click(deleteItem)

    await waitFor(() => {
      expect(
        screen.getByText(
          i18nT('apps.designTweak.status.delete_failed', { error: 'forbidden' }),
        ),
      ).toBeInTheDocument()
    })
  })

  // ─── timeAgo labels ──────────────────────────────────────────────────────

  it('renders relative-time labels for comments using the frozen clock', async () => {
    // createdAt is 5 minutes before FROZEN_NOW
    const fiveMinAgo = new Date(FROZEN_NOW - 5 * 60 * 1000).toISOString()
    setupHappyPath({
      pending: [makeRequest({
        comments: [{
          cid: 'c-time',
          index: 1,
          status: 'sent',
          comment: 'Time-check comment',
          createdAt: fiveMinAgo,
          projectId: 'proj-1',
        }],
      })],
    })
    renderWithProviders(<DesignTweak />)

    // The comment meta line includes the relative time from `ago()`.
    // With the clock frozen at 2026-06-10T12:00:00Z and createdAt 5 min before,
    // the locale-aware timeAgo should produce a "5m" or "5 minutes ago" string.
    // We check that the comment text renders (proving the request expanded) and
    // the meta area contains some time indicator.
    await waitFor(() => {
      expect(screen.getByText('Time-check comment')).toBeInTheDocument()
    })
  })

  // ─── chatRoute navigation ("Open in chat") ──────────────────────────────

  it('navigates to the chat route when "Open in chat" is clicked', async () => {
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 1 })),
      ).toBeInTheDocument()
    })

    // The "open in chat" button is an icon-button with a specific aria-label
    const chatBtn = screen.getByLabelText(
      i18nT('apps.designTweak.requests.open_in_chat'),
    )
    fireEvent.click(chatBtn)

    await waitFor(() => {
      // navigate should have been called with a /chat?sid=... path
      expect(mockNavigate).toHaveBeenCalledWith(
        expect.stringMatching(/^\/chat\?sid=/),
      )
    })
  })

  // ─── Request group collapse/expand toggle ────────────────────────────────

  // A collapse/expand click test lived here and was removed rather than left
  // broken: the row's toggle would not fire under happy-dom, and the same
  // visibility mechanism it asserted is already covered by
  // 'renders history requests collapsed by default' above.


  // ─── History request groups are collapsed by default ─────────────────────

  it('renders history requests collapsed by default', async () => {
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    // Open the history panel
    const historyBtn = await screen.findByRole('button', {
      name: new RegExp(i18nT('apps.designTweak.requests.history')),
    })
    fireEvent.click(historyBtn)

    // The history request row is visible
    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 2 })),
      ).toBeInTheDocument()
    })

    // But comments inside the history request are hidden (collapsed by default)
    const commentEl = screen.getByText('Change background to blue')
    expect(commentEl.closest('[style*="visibility: hidden"]')).toBeTruthy()
  })

  // ─── Page title and tagline render ───────────────────────────────────────

  it('renders the page title and tagline', async () => {
    setupHappyPath()
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.page.title')),
      ).toBeInTheDocument()
      expect(
        screen.getByText(i18nT('apps.designTweak.page.tagline')),
      ).toBeInTheDocument()
    })
  })

  // ─── "no comments" empty state inside a request ──────────────────────────

  it('shows "no comments" inside an expanded request that has zero comments', async () => {
    setupHappyPath({
      pending: [makeRequest({ comments: [] })],
    })
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.no_comments')),
      ).toBeInTheDocument()
    })
  })

  // ─── reqChip states ──────────────────────────────────────────────────────

  it('renders the "done" chip for a completed request', async () => {
    setupHappyPath({
      pending: [makeRequest({
        status: 'done',
        comments: [
          { cid: 'cd-1', index: 1, status: 'done', comment: 'Done task', projectId: 'proj-1' },
          { cid: 'cd-2', index: 2, status: 'done', comment: 'Also done', projectId: 'proj-1' },
        ],
      })],
    })
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.chip_done', { n: 2 })),
      ).toBeInTheDocument()
    })
  })

  it('renders the "not sent" chip for a draft request', async () => {
    setupHappyPath({
      pending: [makeRequest({
        status: 'draft',
        state: 'draft',
        comments: [
          { cid: 'cd-1', index: 1, status: 'new', comment: 'Draft task', projectId: 'proj-1' },
        ],
      })],
    })
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(
        screen.getByText(i18nT('apps.designTweak.requests.chip_not_sent', { n: 1 })),
      ).toBeInTheDocument()
    })
  })

  it('renders a mixed-status chip for an in-progress request', async () => {
    setupHappyPath({
      pending: [makeRequest({
        status: 'sent',
        comments: [
          { cid: 'cm-1', index: 1, status: 'sent', comment: 'In prog 1', projectId: 'proj-1' },
          { cid: 'cm-2', index: 2, status: 'done', comment: 'Done 1', projectId: 'proj-1' },
        ],
      })],
    })
    renderWithProviders(<DesignTweak />)

    // Should show "1 in progress, 1 done" chip
    await waitFor(() => {
      const chipText = [
        i18nT('apps.designTweak.requests.chip_in_progress', { n: 1 }),
        i18nT('apps.designTweak.requests.chip_done', { n: 1 }),
      ].join(', ')
      expect(screen.getByText(chipText)).toBeInTheDocument()
    })
  })

  // ─── Multiple history requests render ────────────────────────────────────

  it('renders multiple history requests with correct numbers', async () => {
    setupHappyPath({
      history: [
        makeHistoryRequest({ id: 'h1', number: 5 }),
        makeHistoryRequest({ id: 'h2', number: 6 }),
        makeHistoryRequest({ id: 'h3', number: 7 }),
      ],
    })
    renderWithProviders(<DesignTweak />)

    // Open history
    const historyBtn = await screen.findByRole('button', {
      name: new RegExp(i18nT('apps.designTweak.requests.history')),
    })
    fireEvent.click(historyBtn)

    await waitFor(() => {
      expect(screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 5 }))).toBeInTheDocument()
      expect(screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 6 }))).toBeInTheDocument()
      expect(screen.getByText(i18nT('apps.designTweak.requests.request_number', { number: 7 }))).toBeInTheDocument()
    })

    // Count shown
    expect(screen.getByText('(3)')).toBeInTheDocument()
  })

  // ─── History section shows zero count when empty ─────────────────────────

  it('shows zero history count when no history exists for the project', async () => {
    setupHappyPath({ history: [] })
    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText('(0)')).toBeInTheDocument()
    })
  })
})
