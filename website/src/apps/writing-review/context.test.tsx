/**
 * Contract tests for the ``WritingReviewProvider`` + ``useWritingReview``
 * context surface.
 *
 * These pin the state-transition behaviour a component using the context
 * relies on:
 *
 * 1. ``initialReviewId`` prop seeds ``selectedReviewId`` on first render.
 * 2. ``selectReview(id)`` updates ``selectedReviewId``.
 * 3. Dialog open/close toggles work for both New Review and Settings.
 * 4. In-flight setters (``setActiveJobId`` / ``DocName`` / ``Phase``) work
 *    and the sessionStorage mirror stays in step.
 * 5. Backend rehydration on mount:
 *    a. no jobs, no mirror → no-op
 *    b. no jobs, mirror seeded → clears the stale mirror state
 *    c. running job present → hydrates from the newest job
 *    d. backend unreachable → mirror-seeded state persists
 * 6. ``useWritingReview`` OUTSIDE a provider throws a helpful error so
 *    a mis-nested test or route surfaces the problem loudly rather
 *    than reading ``undefined``.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../writing-review/api', () => ({
  writingReviewApi: {
    listJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    listReviews: vi.fn().mockResolvedValue({ reviews: [] }),
    getSettings: vi.fn().mockResolvedValue({ max_concurrent: 9 }),
    getReview: vi.fn().mockResolvedValue({}),
  },
  WritingReviewApiError: class WritingReviewApiError extends Error {},
}))
vi.mock('./lib/activeScanMirror', () => ({
  readActiveScanMirror: vi.fn(() => ({ jobId: null, docName: null, phase: null })),
  writeActiveScanMirror: vi.fn(),
  clearActiveScanMirror: vi.fn(),
}))

import { WritingReviewProvider, useWritingReview } from './context'
import { readActiveScanMirror, writeActiveScanMirror } from './lib/activeScanMirror'
import { writingReviewApi } from './api'

const mockedReadActiveScanMirror = vi.mocked(readActiveScanMirror)
const mockedWriteActiveScanMirror = vi.mocked(writeActiveScanMirror)
const mockedListJobs = vi.mocked(writingReviewApi.listJobs)

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

type CapturedContext = ReturnType<typeof useWritingReview> | null

function CaptureContext({ capture }: { capture: (value: CapturedContext) => void }) {
  const contextValue = useWritingReview()
  capture(contextValue)
  return null
}

function renderProvider(props: {
  initialReviewId?: string | null
  capture: (value: CapturedContext) => void
}) {
  const queryClient = makeQueryClient()
  return render(
    <QueryClientProvider client={queryClient}>
      <WritingReviewProvider initialReviewId={props.initialReviewId ?? null}>
        <CaptureContext capture={props.capture} />
      </WritingReviewProvider>
    </QueryClientProvider>,
  )
}

describe('WritingReviewProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedReadActiveScanMirror.mockReturnValue({ jobId: null, docName: null, phase: null })
    mockedListJobs.mockResolvedValue({ jobs: [] })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('seeds selectedReviewId from the initialReviewId prop', async () => {
    let captured: CapturedContext = null
    renderProvider({
      initialReviewId: 'review-seed',
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.selectedReviewId).toBe('review-seed')
  })

  it('defaults selectedReviewId to null when initialReviewId is not passed', async () => {
    let captured: CapturedContext = null
    renderProvider({
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.selectedReviewId).toBeNull()
  })

  it('selectReview updates selectedReviewId', async () => {
    let captured: CapturedContext = null
    renderProvider({
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    await waitFor(() => expect(captured).not.toBeNull())
    act(() => {
      captured!.selectReview('review-clicked')
    })
    await waitFor(() => expect(captured!.selectedReviewId).toBe('review-clicked'))
  })

  it('openNewReviewDialog + closeNewReviewDialog toggle newReviewDialogOpen', async () => {
    let captured: CapturedContext = null
    renderProvider({
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    await waitFor(() => expect(captured).not.toBeNull())
    expect(captured!.newReviewDialogOpen).toBe(false)
    act(() => {
      captured!.openNewReviewDialog()
    })
    await waitFor(() => expect(captured!.newReviewDialogOpen).toBe(true))
    act(() => {
      captured!.closeNewReviewDialog()
    })
    await waitFor(() => expect(captured!.newReviewDialogOpen).toBe(false))
  })

  it('openSettingsDialog + closeSettingsDialog toggle settingsDialogOpen', async () => {
    let captured: CapturedContext = null
    renderProvider({
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    await waitFor(() => expect(captured).not.toBeNull())
    act(() => {
      captured!.openSettingsDialog()
    })
    await waitFor(() => expect(captured!.settingsDialogOpen).toBe(true))
    act(() => {
      captured!.closeSettingsDialog()
    })
    await waitFor(() => expect(captured!.settingsDialogOpen).toBe(false))
  })

  it('setActiveJobId / setActiveJobDocName / setActiveJobPhase update state and mirror to sessionStorage', async () => {
    let captured: CapturedContext = null
    renderProvider({
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    await waitFor(() => expect(captured).not.toBeNull())
    act(() => {
      captured!.setActiveJobId('job-abc')
      captured!.setActiveJobDocName('draft.md')
      captured!.setActiveJobPhase('scanner')
    })
    await waitFor(() => {
      expect(captured!.activeJobId).toBe('job-abc')
      expect(captured!.activeJobDocName).toBe('draft.md')
      expect(captured!.activeJobPhase).toBe('scanner')
    })
    // The mirror-write effect fires on every triple-update. Assert at
    // least one write carried the last-known-good triple so a same-tab
    // remount (theme change etc.) can restore state instantly.
    await waitFor(() => {
      const lastCall =
        mockedWriteActiveScanMirror.mock.calls[
          mockedWriteActiveScanMirror.mock.calls.length - 1
        ]
      expect(lastCall?.[0]).toEqual({
        jobId: 'job-abc',
        docName: 'draft.md',
        phase: 'scanner',
      })
    })
  })

  it('hydrates active-scan state from the newest running backend job on mount', async () => {
    mockedListJobs.mockResolvedValueOnce({
      jobs: [
        // Deliberately unsorted — the provider MUST defensively sort so
        // a future backend refactor that stops sorting can't silently
        // hydrate the wrong job.
        { id: 'older-job', updated_at: 100, doc_name: 'old.md', phase: 'scanner' },
        { id: 'newer-job', updated_at: 200, doc_name: 'new.md', phase: 'fetch' },
      ],
    } as never)
    let captured: CapturedContext = null
    renderProvider({
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    await waitFor(() => expect(captured?.activeJobId).toBe('newer-job'))
    expect(captured!.activeJobDocName).toBe('new.md')
    expect(captured!.activeJobPhase).toBe('fetch')
  })

  it('clears a stale mirror-seeded active-scan when the backend reports no running jobs', async () => {
    mockedReadActiveScanMirror.mockReturnValue({
      jobId: 'seed-from-mirror',
      docName: 'seeded.md',
      phase: 'scanner',
    })
    mockedListJobs.mockResolvedValueOnce({ jobs: [] } as never)
    let captured: CapturedContext = null
    renderProvider({
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    // The provider seeds from the mirror synchronously, then the fetch
    // resolves "no running jobs" and clears the stale seed.
    await waitFor(() => expect(captured?.activeJobId).toBeNull())
    expect(captured!.activeJobDocName).toBeNull()
    expect(captured!.activeJobPhase).toBeNull()
  })

  it('keeps the mirror-seeded state when the backend rehydration call throws', async () => {
    mockedReadActiveScanMirror.mockReturnValue({
      jobId: 'seed-from-mirror',
      docName: 'seeded.md',
      phase: 'scanner',
    })
    mockedListJobs.mockRejectedValueOnce(new Error('backend unreachable'))
    let captured: CapturedContext = null
    renderProvider({
      capture: (contextValue) => {
        captured = contextValue
      },
    })
    await waitFor(() => expect(captured).not.toBeNull())
    // A transient network failure MUST NOT clear the mirror seed —
    // that would blank the sidebar in-progress card on every hiccup.
    // The seeded state persists until a successful poll happens
    // through ScanProgress or a manual reload.
    expect(captured!.activeJobId).toBe('seed-from-mirror')
    expect(captured!.activeJobDocName).toBe('seeded.md')
    expect(captured!.activeJobPhase).toBe('scanner')
  })

  it('useWritingReview throws a helpful error when used outside the provider', () => {
    // Silence React's expected error boundary noise for this negative
    // case so the terminal stays readable during a green run.
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      expect(() =>
        render(
          <QueryClientProvider client={makeQueryClient()}>
            <CaptureContext capture={() => {}} />
          </QueryClientProvider>,
        ),
      ).toThrow(/useWritingReview must be used inside a WritingReviewProvider/)
    } finally {
      consoleErrorSpy.mockRestore()
    }
  })
})
