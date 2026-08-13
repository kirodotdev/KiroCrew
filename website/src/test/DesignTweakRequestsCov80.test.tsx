/**
 * Runtime coverage for DesignTweakPage.tsx — request rail, comment rows,
 * numbering, comment delete, follow-up labels, send flow, resend/retry bar,
 * delivery-verdict states, and per-request status lines.
 *
 * Uses real rendering (not source-text regexes) so vitest --coverage reports
 * genuine statement hits.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import DesignTweak from '../apps/design-tweak/DesignTweakPage'
import { renderWithProviders } from './helpers'
import { i18nT } from '../i18n/t'

vi.mock('../apps/design-tweak/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/design-tweak/api')>()),
  fetchProjects: vi.fn(),
  fetchQueue: vi.fn(),
  fetchHistory: vi.fn(),
  fetchHealth: vi.fn(),
  submitComment: vi.fn(),
  deleteComment: vi.fn(),
  sendRequest: vi.fn(),
  markDelivered: vi.fn(),
  readSlotTranscript: vi.fn(),
  createChatSlot: vi.fn(),
  sendChatMessage: vi.fn(),
  setChatSlotProject: vi.fn(),
}))

// Grab typed references to the mocked api functions
import * as api from '../apps/design-tweak/api'
const mockFetchProjects = api.fetchProjects as ReturnType<typeof vi.fn>
const mockFetchQueue = api.fetchQueue as ReturnType<typeof vi.fn>
const mockFetchHistory = api.fetchHistory as ReturnType<typeof vi.fn>
const mockFetchHealth = api.fetchHealth as ReturnType<typeof vi.fn>
const mockSendRequest = api.sendRequest as ReturnType<typeof vi.fn>
const mockMarkDelivered = api.markDelivered as ReturnType<typeof vi.fn>
const mockDeleteComment = api.deleteComment as ReturnType<typeof vi.fn>
const mockReadSlotTranscript = api.readSlotTranscript as ReturnType<typeof vi.fn>
const mockCreateChatSlot = api.createChatSlot as ReturnType<typeof vi.fn>
const mockSendChatMessage = api.sendChatMessage as ReturnType<typeof vi.fn>
const mockSetChatSlotProject = api.setChatSlotProject as ReturnType<typeof vi.fn>

// Stable fixtures
const PROJECT = {
  id: 'proj-1',
  path: '/home/user/myapp',
  name: 'MyApp',
  previewUrl: 'http://127.0.0.1:5555',
}

function makeComment(overrides: Record<string, unknown> = {}) {
  return {
    cid: 'c-100',
    index: 1,
    status: 'new',
    comment: 'Make the button red',
    createdAt: new Date().toISOString(),
    element: 'button.submit',
    locator: 'button.submit',
    ...overrides,
  }
}

function makeRequest(overrides: Record<string, unknown> = {}) {
  return {
    id: 'req-1',
    number: 1,
    status: 'draft',
    state: 'draft',
    projectId: 'proj-1',
    projectRoot: '/home/user/myapp',
    comments: [makeComment()],
    ...overrides,
  }
}

// Standard mount mocks that resolve the page past its booting state
function setupDefaultMocks() {
  mockFetchProjects.mockResolvedValue({
    projects: [PROJECT],
    activeId: 'proj-1',
    serving: true,
  })
  mockFetchHealth.mockResolvedValue({ dataDir: '/data/design-tweak' })
  mockFetchQueue.mockResolvedValue({ pending: [] })
  mockFetchHistory.mockResolvedValue({ history: [] })
  mockCreateChatSlot.mockResolvedValue({ key: 'slot-key', messages: 0 })
  mockSendChatMessage.mockResolvedValue({ ok: true })
  mockSetChatSlotProject.mockResolvedValue({ ok: true })
  mockMarkDelivered.mockResolvedValue({ ok: true })
  mockReadSlotTranscript.mockResolvedValue(null)
}

beforeEach(() => {
  vi.clearAllMocks()
  setupDefaultMocks()
})

describe('DesignTweak — request rail', () => {
  it('renders pending requests with numbered labels and comment text', async () => {
    const req = makeRequest({ number: 3 })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      // The request group header shows "Request #3"
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.request_number', { number: 3 }),
      )).toBeInTheDocument()
    })

    // Comment text is visible inside the request group
    expect(screen.getByText('Make the button red')).toBeInTheDocument()
  })

  it('shows the empty state when no requests exist for the connected app', async () => {
    mockFetchQueue.mockResolvedValue({ pending: [] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.empty_for_app', { name: 'MyApp' }),
      )).toBeInTheDocument()
    })
  })

  it('shows "no comments" placeholder when a request has empty comments array', async () => {
    const req = makeRequest({ comments: [] })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.request_number', { number: 1 }),
      )).toBeInTheDocument()
    })

    expect(screen.getByText(
      i18nT('apps.designTweak.requests.no_comments'),
    )).toBeInTheDocument()
  })

  it('renders the status chip for a draft with correct label', async () => {
    const req = makeRequest({ status: 'draft', comments: [makeComment(), makeComment({ cid: 'c-101', index: 2 })] })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.chip_not_sent', { n: 2 }),
      )).toBeInTheDocument()
    })
  })

  it('renders the status chip for a done request', async () => {
    const req = makeRequest({
      status: 'done',
      state: 'sent',
      comments: [makeComment({ status: 'done' })],
    })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.chip_done', { n: 1 }),
      )).toBeInTheDocument()
    })
  })

  it('renders a chip for an in-progress request with mixed statuses', async () => {
    const req = makeRequest({
      status: 'sent',
      state: 'sent',
      comments: [
        makeComment({ status: 'sent', cid: 'c-1', index: 1 }),
        makeComment({ status: 'done', cid: 'c-2', index: 2 }),
      ],
    })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    // The chip shows the composite "1 in progress, 1 done" label
    await waitFor(() => {
      const chipText = [
        i18nT('apps.designTweak.requests.chip_in_progress', { n: 1 }),
        i18nT('apps.designTweak.requests.chip_done', { n: 1 }),
      ].join(', ')
      expect(screen.getByText(chipText)).toBeInTheDocument()
    })
  })

  it('shows follow-up label when a comment references another', async () => {
    const comments = [
      makeComment({ cid: 'c-orig', index: 1 }),
      makeComment({ cid: 'c-fu', index: 2, followUpTo: 'c-orig', comment: 'Follow up note' }),
    ]
    const req = makeRequest({ number: 5, comments })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    // The follow-up label resolves the origin's number label "5.1"
    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.comments.follow_up_to', { label: '5.1' }),
      )).toBeInTheDocument()
    })
  })

  it('shows thread last-agent reply when present', async () => {
    const thread = [
      { role: 'user', text: 'Initial request' },
      { role: 'agent', text: 'Done — changed color to red' },
    ]
    const req = makeRequest({ comments: [makeComment({ thread })] })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText('Done — changed color to red')).toBeInTheDocument()
    })
  })
})

describe('DesignTweak — comment deletion', () => {
  it('shows delete button only on draft request comments and calls api on click', async () => {
    const req = makeRequest()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    mockDeleteComment.mockResolvedValue({})

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText('Make the button red')).toBeInTheDocument()
    })

    const removeLabel = i18nT('apps.designTweak.comments.remove_from_draft')
    const deleteBtn = screen.getByLabelText(removeLabel)
    expect(deleteBtn).toBeInTheDocument()

    fireEvent.click(deleteBtn)

    await waitFor(() => {
      expect(mockDeleteComment).toHaveBeenCalledWith('req-1', 'c-100')
    })
  })

  it('does not show delete button on sent (non-draft) request comments', async () => {
    const req = makeRequest({ state: 'sent', status: 'sent' })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText('Make the button red')).toBeInTheDocument()
    })

    const removeLabel = i18nT('apps.designTweak.comments.remove_from_draft')
    expect(screen.queryByLabelText(removeLabel)).not.toBeInTheDocument()
  })
})

describe('DesignTweak — send request flow', () => {
  it('shows Send button for draft with comments and calls sendRequest on click', async () => {
    const req = makeRequest()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    mockSendRequest.mockResolvedValue({
      ok: true,
      request: { ...req, state: 'sent', status: 'sent' },
    })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.send_request', { number: 1 }),
      )).toBeInTheDocument()
    })

    const sendBtn = screen.getByText(
      i18nT('apps.designTweak.requests.send_request', { number: 1 }),
    )
    fireEvent.click(sendBtn)

    await waitFor(() => {
      expect(mockSendRequest).toHaveBeenCalledWith('req-1')
    })
  })

  it('does not show Send button for draft with zero comments', async () => {
    const req = makeRequest({ comments: [] })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.request_number', { number: 1 }),
      )).toBeInTheDocument()
    })

    expect(screen.queryByText(
      i18nT('apps.designTweak.requests.send_request', { number: 1 }),
    )).not.toBeInTheDocument()
  })

  it('shows "already sent" status when sendRequest returns already:true', async () => {
    const req = makeRequest()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    mockSendRequest.mockResolvedValue({
      ok: true,
      already: true,
      request: { ...req, state: 'sent' },
    })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.send_request', { number: 1 }),
      )).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText(
      i18nT('apps.designTweak.requests.send_request', { number: 1 }),
    ))

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.status.already_sent', { number: 1 }),
      )).toBeInTheDocument()
    })
  })

  it('shows send_failed status when sendRequest returns ok:false', async () => {
    const req = makeRequest()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    mockSendRequest.mockResolvedValue({ ok: false, error: 'Server exploded' })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.send_request', { number: 1 }),
      )).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText(
      i18nT('apps.designTweak.requests.send_request', { number: 1 }),
    ))

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('pages.chatPage.send_failed_with_error', { error: 'Server exploded' }),
      )).toBeInTheDocument()
    })
  })
})

describe('DesignTweak — resend/retry bar (delivery verdict)', () => {
  // A "sealed but undelivered" request that verifyDelivery can judge
  function sealedUndeliveredReq() {
    return makeRequest({
      id: 'req-sealed',
      number: 2,
      state: 'sent',
      status: 'sent',
      deliveredAt: undefined,
    })
  }

  it('shows resend bar when verifyDelivery confirms batch is missing from session', async () => {
    const req = sealedUndeliveredReq()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    // transcript returns content that does NOT include the request id
    mockReadSlotTranscript.mockResolvedValue({
      messages: [{ content: 'unrelated message' }],
      queue: [],
    })
    // Slot creation for verifyDelivery
    mockCreateChatSlot.mockResolvedValue({ key: 'slot-key', messages: 1 })

    renderWithProviders(<DesignTweak />)

    // The resend button appears after verifyDelivery determines "missing"
    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.send_missing_request', { number: 2 }),
      )).toBeInTheDocument()
    }, { timeout: 3000 })
  })

  it('confirmed resend retires the retry control so a duplicate cannot fire', async () => {
    const req = sealedUndeliveredReq()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    // Initial verify says missing
    mockReadSlotTranscript.mockResolvedValue({
      messages: [{ content: 'nothing relevant' }],
      queue: [],
    })
    mockCreateChatSlot.mockResolvedValue({ key: 'slot-key', messages: 1 })
    mockSendChatMessage.mockResolvedValue({ ok: true })

    renderWithProviders(<DesignTweak />)

    // Wait for the resend bar to appear
    const resendLabel = i18nT('apps.designTweak.requests.send_missing_request', { number: 2 })
    await waitFor(() => {
      expect(screen.getByText(resendLabel)).toBeInTheDocument()
    }, { timeout: 3000 })

    // Click resend — deliverSealed succeeds
    fireEvent.click(screen.getByText(resendLabel))

    // The retry button disappears after confirmed dispatch
    await waitFor(() => {
      expect(screen.queryByText(resendLabel)).not.toBeInTheDocument()
    })
  })

  it('failed resend does NOT retire the retry control', async () => {
    const req = sealedUndeliveredReq()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    mockReadSlotTranscript.mockResolvedValue({
      messages: [],
      queue: [],
    })
    mockCreateChatSlot.mockResolvedValue({ key: 'slot-key', messages: 1 })
    // The dispatch throws — simulates network failure
    mockSendChatMessage.mockRejectedValue(new Error('Network down'))

    renderWithProviders(<DesignTweak />)

    const resendLabel = i18nT('apps.designTweak.requests.send_missing_request', { number: 2 })
    await waitFor(() => {
      expect(screen.getByText(resendLabel)).toBeInTheDocument()
    }, { timeout: 3000 })

    fireEvent.click(screen.getByText(resendLabel))

    // After the failure the retry button must still be present
    await waitFor(() => {
      expect(screen.getByText(resendLabel)).toBeInTheDocument()
    })
  })

  it('does not show resend bar when verifyDelivery confirms delivery', async () => {
    const req = sealedUndeliveredReq()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    // Transcript CONTAINS the request id — verdict is "delivered"
    mockReadSlotTranscript.mockResolvedValue({
      messages: [{ content: 'Apply request req-sealed blah' }],
      queue: [],
    })
    mockCreateChatSlot.mockResolvedValue({ key: 'slot-key', messages: 1 })

    renderWithProviders(<DesignTweak />)

    // Wait for the component to settle, then confirm the resend bar is absent
    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.request_number', { number: 2 }),
      )).toBeInTheDocument()
    })

    // Give verifyDelivery time to run and not render the bar
    await new Promise((r) => setTimeout(r, 100))
    expect(screen.queryByText(
      i18nT('apps.designTweak.requests.send_missing_request', { number: 2 }),
    )).not.toBeInTheDocument()
  })

  it('does not show resend bar when transcript lookup fails (verdict: unknown)', async () => {
    const req = sealedUndeliveredReq()
    mockFetchQueue.mockResolvedValue({ pending: [req] })
    // null transcript → unknown verdict → no action
    mockReadSlotTranscript.mockResolvedValue(null)

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.request_number', { number: 2 }),
      )).toBeInTheDocument()
    })

    await new Promise((r) => setTimeout(r, 100))
    expect(screen.queryByText(
      i18nT('apps.designTweak.requests.send_missing_request', { number: 2 }),
    )).not.toBeInTheDocument()
  })
})

describe('DesignTweak — request toggling and history', () => {
  it('collapses/expands a request group on click', async () => {
    const req = makeRequest()
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      expect(screen.getByText('Make the button red')).toBeInTheDocument()
    })

    // Click the request header to collapse
    const header = screen.getByText(
      i18nT('apps.designTweak.requests.request_number', { number: 1 }),
    )
    fireEvent.click(header)

    // The comment is now hidden (FolderBody's visibility toggle)
    const commentText = screen.getByText('Make the button red')
    const folderBody = commentText.closest('[style*="visibility"]')
    expect(folderBody).toBeTruthy()
    expect(folderBody!.getAttribute('style')).toContain('hidden')
  })

  it('opens the history section and shows archived requests', async () => {
    const histReq = makeRequest({
      id: 'req-hist',
      number: 10,
      state: 'sent',
      status: 'done',
      comments: [makeComment({ status: 'done', cid: 'c-hist' })],
    })
    mockFetchHistory.mockResolvedValue({ history: [histReq] })

    renderWithProviders(<DesignTweak />)

    // History button shows count
    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.history'),
      )).toBeInTheDocument()
    })

    // Click to expand history
    fireEvent.click(screen.getByText(i18nT('apps.designTweak.requests.history')))

    // Now the history request is visible
    await waitFor(() => {
      expect(screen.getByText(
        i18nT('apps.designTweak.requests.request_number', { number: 10 }),
      )).toBeInTheDocument()
    })
  })
})

describe('DesignTweak — comment meta and status dots', () => {
  it('renders the meta line with label, element, and relative time', async () => {
    const req = makeRequest({ number: 7 })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      // The meta format includes label "7.1" and element name
      expect(screen.getByText(/7\.1/)).toBeInTheDocument()
    })
  })

  it('uses element_count fallback when element is empty', async () => {
    const comment = makeComment({ element: '', count: 3 })
    const req = makeRequest({ comments: [comment] })
    mockFetchQueue.mockResolvedValue({ pending: [req] })

    renderWithProviders(<DesignTweak />)

    await waitFor(() => {
      const countLabel = i18nT('apps.designTweak.comments.element_count', { n: 3 })
      // The meta line includes this count label
      expect(screen.getByText(new RegExp(countLabel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).toBeInTheDocument()
    })
  })
})
