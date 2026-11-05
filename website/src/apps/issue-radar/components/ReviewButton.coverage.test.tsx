/**
 * ReviewButton: the PR header's "Review" control.
 *
 * It must not act on a lookup it cannot trust (a pending or failed fetch looks like "no record", and
 * starting a session then would orphan the existing one), it resumes an existing session instead of
 * starting a second, it writes the saved record back into the query cache, and a concluded item turns
 * the click into an explicit "Start over". The review hook is mocked; AgentSessionButton is real, so
 * what is asserted is the label, the disabled state and the tooltip the user sees.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { InvestigationRecord, PullRequest, RepoRef } from '../api'
import { itemKey } from '../lib/agentSession'
import { repoScopeKey } from '../lib/links'

const getInvestigation = vi.hoisted(() => vi.fn())
const review = vi.hoisted(() => ({
  reviewPr: vi.fn(),
  busy: false,
  error: null as Error | null,
  concludedFor: null as string | null,
  openOlderSessions: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api')>()
  return { ...mod, issueRadarApi: { ...mod.issueRadarApi, getInvestigation } }
})
vi.mock('../lib/review', () => ({ useReviewPr: () => review }))

import ReviewButton from './ReviewButton'

const repoRef: RepoRef = { owner: 'zzq', repo: 'fabric', provider: 'github', host: 'github.com' }
const pull = { number: 7, title: 'Tighten the retry loop' } as unknown as PullRequest
const record = { slot_key: 'slot-1', status: 'pending' } as unknown as InvestigationRecord
const cacheKey = ['issue-radar', 'investigation', repoScopeKey(repoRef), 'pull', pull.number]

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ReviewButton repoRef={repoRef} pull={pull} />
    </QueryClientProvider>,
  )
  return client
}

beforeEach(() => {
  getInvestigation.mockReset()
  review.reviewPr.mockReset()
  review.busy = false
  review.error = null
  review.concludedFor = null
})

describe('ReviewButton', () => {
  it('looks up the PR record by repo, number and kind, and offers Review when there is none', async () => {
    getInvestigation.mockResolvedValue({ investigation: null })
    mount()
    const button = await screen.findByRole('button', { name: 'Review' })
    await waitFor(() => expect(button).toBeEnabled())
    expect(getInvestigation).toHaveBeenCalledWith(repoRef, 7, 'pull')
    expect(button.title).toMatch(/^Open an AI code-review chat session/)
  })

  it('stays disabled while the lookup is pending and ignores a click', async () => {
    getInvestigation.mockReturnValue(new Promise(() => {}))
    mount()
    const button = await screen.findByRole('button', { name: 'Review' })
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(review.reviewPr).not.toHaveBeenCalled()
  })

  it('reports a failed lookup instead of guessing and never starts a session', async () => {
    getInvestigation.mockRejectedValue(new Error('gateway unreachable'))
    mount()
    const button = await screen.findByRole('button', { name: 'Review' })
    await waitFor(() => expect(button.title).toBe('gateway unreachable'))
    expect(button).toBeDisabled()
    expect(review.reviewPr).not.toHaveBeenCalled()
  })

  it('starts a session on click and caches the saved record, which flips the label to Resume', async () => {
    getInvestigation.mockResolvedValue({ investigation: null })
    review.reviewPr.mockResolvedValue(record)
    const client = mount()
    const button = await screen.findByRole('button', { name: 'Review' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await screen.findByRole('button', { name: 'Resume' })
    expect(review.reviewPr).toHaveBeenCalledWith(repoRef, pull, null, false)
    expect(client.getQueryData(cacheKey)).toEqual({
      owner: 'zzq', repo: 'fabric', number: 7, kind: 'pull', investigation: record,
    })
  })

  it('leaves the cache alone when the session could not be started', async () => {
    getInvestigation.mockResolvedValue({ investigation: null })
    review.reviewPr.mockResolvedValue(null)
    const client = mount()
    const button = await screen.findByRole('button', { name: 'Review' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(review.reviewPr).toHaveBeenCalledTimes(1))
    expect(client.getQueryData(cacheKey)).toEqual({ investigation: null })
  })

  it('resumes an existing session with its record, and shows no status pill', async () => {
    getInvestigation.mockResolvedValue({ investigation: record })
    review.reviewPr.mockResolvedValue(record)
    mount()
    const button = await screen.findByRole('button', { name: 'Resume' })
    expect(button.title).toMatch(/resume/i)
    expect(screen.getByTestId('agent-session-action-row').children).toHaveLength(1)
    fireEvent.click(button)
    await waitFor(() => expect(review.reviewPr).toHaveBeenCalledWith(repoRef, pull, record, false))
  })

  it('does nothing while the review hook is busy', async () => {
    getInvestigation.mockResolvedValue({ investigation: null })
    review.busy = true
    mount()
    const button = await screen.findByRole('button')
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(review.reviewPr).not.toHaveBeenCalled()
  })

  it('turns a concluded item into Start over, which forces a new session', async () => {
    getInvestigation.mockResolvedValue({ investigation: record })
    review.concludedFor = itemKey(repoRef, 7, 'pull')
    review.reviewPr.mockResolvedValue(record)
    mount()
    const button = await screen.findByRole('button', { name: 'Start over' })
    await waitFor(() => expect(button).toBeEnabled()) // the label follows the hook; the click waits for the lookup
    fireEvent.click(button)
    await waitFor(() => expect(review.reviewPr).toHaveBeenCalledWith(repoRef, pull, record, true))
  })
})
