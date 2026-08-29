/**
 * Contract tests for ``ReviewDetail`` — the detail-pane component that
 * renders a review's header, findings buckets, and failed-scanner
 * banner.
 *
 * The existing ``ReviewDetail.test.tsx`` file covers
 * ``buildReviewChatMessage`` behaviour (docx-inline vs md fs_read). This
 * file covers the COMPONENT's own branches:
 *
 * 1. loading state
 * 2. error state
 * 3. populated render with grouped findings by severity
 * 4. empty-findings state
 * 5. ask-label conditional
 * 6. partial-failure banner
 * 7. click "Chat about this" → context fetch success path
 * 8. click "Chat about this" → context fetch failure fallback
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('../context', () => ({
  useWritingReview: vi.fn(),
}))
vi.mock('../api', () => ({
  writingReviewApi: {
    getReviewContext: vi.fn(),
  },
  WritingReviewApiError: class WritingReviewApiError extends Error {},
}))

const mockedOpenChat = vi.fn()
vi.mock('../../../app-sdk', () => ({
  useChatLauncher: () => ({ openChat: mockedOpenChat }),
}))

vi.mock('./FindingCard', () => ({
  default: ({ finding }: { finding: { id: string; issue: string } }) => (
    <div data-testid={`stub-finding-${finding.id}`}>{finding.issue}</div>
  ),
}))
vi.mock('./VerdictBadge', () => ({
  default: ({ verdict }: { verdict: string }) => (
    <span data-testid={`stub-verdict-${verdict}`} />
  ),
}))

import ReviewDetailComponent from './ReviewDetail'
import { useWritingReview } from '../context'
import { writingReviewApi } from '../api'

const mockedUseWritingReview = vi.mocked(useWritingReview)
const mockedGetReviewContext = vi.mocked(writingReviewApi.getReviewContext)

function makeFakeContextValueForReviewDetail(reviewDetailQuery: {
  isLoading?: boolean
  isError?: boolean
  data?: unknown
}) {
  return {
    reviewDetailQuery,
    selectedReviewId: null,
    selectReview: vi.fn(),
    reviewsQuery: {},
    settingsQuery: {},
    newReviewDialogOpen: false,
    openNewReviewDialog: vi.fn(),
    closeNewReviewDialog: vi.fn(),
    settingsDialogOpen: false,
    openSettingsDialog: vi.fn(),
    closeSettingsDialog: vi.fn(),
    activeJobId: null,
    setActiveJobId: vi.fn(),
    activeJobDocName: null,
    setActiveJobDocName: vi.fn(),
    activeJobPhase: null,
    setActiveJobPhase: vi.fn(),
  }
}

function makeReviewData(overrides: Record<string, unknown> = {}) {
  return {
    id: 'r1',
    doc_name: 'design.md',
    doc_path: '/tmp/design.md',
    verdict: 'yellow',
    finding_count: 0,
    scanners_run: ['clarity', 'evidence'],
    created_at: 0,
    context: {
      audience: 'team',
      doc_type: 'update',
      tone: 'neutral',
      additional_context: [],
      ask: '',
    },
    findings: [],
    partial_failure: false,
    failed_scanners: [],
    log_reference: null,
    ...overrides,
  }
}

describe('ReviewDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the loading placeholder while reviewDetailQuery.isLoading', () => {
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewDetail({
        isLoading: true,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewDetailComponent />)
    expect((container.textContent ?? '').trim().length).toBeGreaterThan(0)
    // No verdict badge, no findings stub in the loading state.
    expect(screen.queryByTestId(/stub-verdict-/)).toBeNull()
  })

  it('renders the error placeholder when reviewDetailQuery.isError', () => {
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewDetail({
        isError: true,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewDetailComponent />)
    expect(container.querySelector('.text-danger')).not.toBeNull()
  })

  it('renders header, verdict badge, and grouped findings for a populated review', () => {
    const reviewData = makeReviewData({
      verdict: 'red',
      findings: [
        { id: 'f-high', severity: 'high', issue: 'high issue' },
        { id: 'f-medium', severity: 'medium', issue: 'medium issue' },
        { id: 'f-low', severity: 'low', issue: 'low issue' },
        { id: 'f-advisory', severity: 'advisory', issue: 'advisory issue' },
      ],
    })
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewDetail({
        data: reviewData,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewDetailComponent />)
    // The header MUST show the doc name so a user can tell which review
    // they're reading at a glance.
    expect(container.textContent).toContain('design.md')
    expect(screen.getByTestId('stub-verdict-red')).toBeInTheDocument()
    for (const findingId of ['f-high', 'f-medium', 'f-low', 'f-advisory']) {
      expect(screen.getByTestId(`stub-finding-${findingId}`)).toBeInTheDocument()
    }
  })

  it('renders the "no findings" note when the review has zero findings', () => {
    const reviewData = makeReviewData({ findings: [] })
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewDetail({
        data: reviewData,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewDetailComponent />)
    // No stub-finding-* elements should be rendered on a zero-finding
    // review.
    expect(container.querySelectorAll('[data-testid^="stub-finding-"]').length).toBe(0)
    // A non-empty "no findings" placeholder text MUST appear so the
    // pane doesn't leave the user staring at a blank scroll region.
    expect((container.textContent ?? '').length).toBeGreaterThan('design.md'.length)
  })

  it('renders the ask-label line when context.ask is non-empty', () => {
    const reviewData = makeReviewData({
      context: {
        audience: 'team',
        doc_type: 'update',
        tone: 'neutral',
        additional_context: [],
        ask: 'is my thesis clear?',
      },
    })
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewDetail({
        data: reviewData,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewDetailComponent />)
    // The ask text propagates through i18nT interpolation into the
    // header row. Assert the ask string appears somewhere in the DOM.
    expect(container.textContent).toContain('is my thesis clear?')
  })

  it('renders the partial-failure banner when partial_failure is true and there are failed scanners', () => {
    const reviewData = makeReviewData({
      partial_failure: true,
      failed_scanners: [
        {
          name: 'evidence',
          reason_class: 'provider_timeout',
          duration_ms: 15_000,
          message: 'timed out',
        },
      ],
    })
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewDetail({
        data: reviewData,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    const { container } = render(<ReviewDetailComponent />)
    // The banner carries the ``border-warn`` + ``bg-warn-subtle`` tokens
    // and a ``<ul>`` listing each failed scanner by resolved name.
    expect(container.querySelector('.border-warn')).not.toBeNull()
    // The failed-scanner list MUST reference the failed scanner by name
    // so the user knows which scanner didn't run — a bare "some scanners
    // failed" banner would leave them guessing.
    // ``resolveScannerName`` returns "Evidence" (title-cased localised
    // label) for the ``evidence`` key.
    expect((container.textContent ?? '').toLowerCase()).toContain('evidence')
  })

  it('kicks off openChat with the full context handoff message on click when the context fetch succeeds', async () => {
    const reviewData = makeReviewData({ id: 'r-chat' })
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewDetail({
        data: reviewData,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedGetReviewContext.mockResolvedValueOnce({
      review: reviewData,
      document_content: 'body content',
      scanner_brief_dir: '/tmp/briefs',
    } as never)
    render(<ReviewDetailComponent />)
    // The chat-launch button is the only ``<button>`` on the header row.
    const buttons = screen.getAllByRole('button')
    fireEvent.click(buttons[0])
    await waitFor(() => {
      expect(mockedOpenChat).toHaveBeenCalledTimes(1)
    })
    const openChatCallArgs = mockedOpenChat.mock.calls[0][0] as {
      agent: string
      message: string
    }
    expect(openChatCallArgs.agent).toBe('writing-review-reviewer')
    // The full-context message MUST include the ``[REVIEW CONTEXT]``
    // header — that's the wire-contract prefix the agent parses.
    expect(openChatCallArgs.message).toContain('[REVIEW CONTEXT]')
  })

  it('falls back to the compact review_id marker when the context fetch fails', async () => {
    const reviewData = makeReviewData({ id: 'r-fallback' })
    mockedUseWritingReview.mockReturnValue(
      makeFakeContextValueForReviewDetail({
        data: reviewData,
      }) as unknown as ReturnType<typeof useWritingReview>,
    )
    mockedGetReviewContext.mockRejectedValueOnce(new Error('context fetch broken'))
    render(<ReviewDetailComponent />)
    const buttons = screen.getAllByRole('button')
    fireEvent.click(buttons[0])
    await waitFor(() => {
      expect(mockedOpenChat).toHaveBeenCalledTimes(1)
    })
    const openChatCallArgs = mockedOpenChat.mock.calls[0][0] as {
      agent: string
      message: string
    }
    // Compact fallback marker MUST carry the review id so the agent's
    // opening turn can at least acknowledge which review the user is
    // asking about, even though the full context isn't inlined.
    expect(openChatCallArgs.message).toBe('[REVIEW CONTEXT] review_id=r-fallback')
  })
})
