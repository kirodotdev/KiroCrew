// @vitest-environment jsdom
/**
 * The Keep / Discard row on its own: two choices alone on their row, every line
 * about them below it, and each refusal said where the person is looking.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { renderWithProviders } from './helpers'
import DashboardPreviewActions from '../pages/chat/DashboardPreviewActions'

const refused = (status: number, code: string) => new ApiError(status, code, JSON.stringify({ error: code, code }))

describe('DashboardPreviewActions', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps Keep and Discard alone on their row, with the note below', () => {
    renderWithProviders(<DashboardPreviewActions slug="atlas" member="Atlas" stagedMs={7} onDone={vi.fn()} />)
    const row = screen.getByTestId('dashboard-preview-keep').parentElement!
    expect(row.querySelectorAll('button')).toHaveLength(2)
    expect(screen.getByTestId('dashboard-preview-discard-note')).toHaveTextContent("Discard can't be undone.")
  })

  it('a click before the frame has drawn the staged page does nothing', () => {
    const apply = vi.spyOn(api, 'memberDashboardPreviewApply')
    const discard = vi.spyOn(api, 'memberDashboardPreviewDiscard')
    renderWithProviders(<DashboardPreviewActions slug="atlas" member="Atlas" stagedMs={0} onDone={vi.fn()} />)
    fireEvent.click(screen.getByTestId('dashboard-preview-keep'))
    fireEvent.click(screen.getByTestId('dashboard-preview-discard'))
    expect(apply).not.toHaveBeenCalled()
    expect(discard).not.toHaveBeenCalled()
  })

  it('a failed Discard stays put and says so as an error', async () => {
    vi.spyOn(api, 'memberDashboardPreviewDiscard').mockRejectedValue(refused(500, 'dashboard_failed'))
    const onDone = vi.fn()
    renderWithProviders(<DashboardPreviewActions slug="atlas" member="Atlas" stagedMs={7} onDone={onDone} />)
    fireEvent.click(screen.getByTestId('dashboard-preview-discard'))
    const problem = await screen.findByTestId('dashboard-preview-problem')
    expect(problem).toHaveTextContent("Couldn't discard this preview. Nothing changed. Try again.")
    expect(problem.closest('[role="alert"]')).not.toBeNull()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('a Keep that really failed (500) is an ErrorNotice, never the status notice', async () => {
    vi.spyOn(api, 'memberDashboardPreviewApply').mockRejectedValue(refused(500, 'dashboard_failed'))
    renderWithProviders(<DashboardPreviewActions slug="atlas" member="Atlas" stagedMs={7} onDone={vi.fn()} />)
    fireEvent.click(screen.getByTestId('dashboard-preview-keep'))
    const problem = await screen.findByTestId('dashboard-preview-problem')
    expect(problem).toHaveTextContent("Couldn't keep this preview. Nothing changed. Try again.")
    expect(problem.closest('[role="alert"]')).not.toBeNull()
    expect(screen.queryByTestId('dashboard-preview-changed')).toBeNull()
  })

  it('a newer staged page refuses Keep, says so, and redraws the preview', async () => {
    vi.spyOn(api, 'memberDashboardPreviewApply').mockRejectedValue(refused(409, 'preview_changed'))
    const onDone = vi.fn()
    const { queryClient } = renderWithProviders(<DashboardPreviewActions slug="atlas" member="Atlas" stagedMs={7} onDone={onDone} />)
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    fireEvent.click(screen.getByTestId('dashboard-preview-keep'))
    const line = await screen.findByTestId('dashboard-preview-changed')
    expect(line).toHaveTextContent('The preview changed while you were looking. The new one is below; Keep now keeps it.')
    // The shared StatusNotice, warn tone: nothing failed, the newer page is drawn.
    expect(line).toHaveAttribute('role', 'status')
    expect(line).toHaveAttribute('data-tone', 'warn')
    expect(line.closest('[role="alert"]')).toBeNull()
    expect(screen.queryByTestId('dashboard-preview-problem')).toBeNull()
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['member-dashboard', 'atlas'] }))
    expect(onDone).not.toHaveBeenCalled()
  })
})
