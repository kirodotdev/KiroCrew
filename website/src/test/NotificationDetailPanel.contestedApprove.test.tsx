/**
 * A contested approval -- a run continued from a second chat, which the
 * admission gate declared one only a human may answer -- leads with copy whose
 * safe action is to start the task again from one chat. The Review panel's
 * Approve must not be the filled primary a habituated approver presses on
 * sight: on a contested card it is drawn as the outline secondary, on every
 * other approval it stays the filled primary it has always been.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { api } from '../api/client'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: { resolveApproval: vi.fn().mockResolvedValue({}), ackNotification: vi.fn().mockResolvedValue({}) },
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const approvalNote: Notification = {
  kind: 'approval', ts: '2026-10-02T15:00:00Z', title: 'Tool approval: shell',
  body: 'psql --dry-run', approval_id: 'ap-1', acked: true,
}

describe('NotificationDetailPanel — Approve on a contested approval', () => {
  it('draws Approve as the outline secondary when the prompt is contested', () => {
    renderWithProviders(<NotificationDetailPanel n={{ ...approvalNote, contested: true }} onClose={() => {}} />)
    const approve = screen.getByTestId('approval-approve')
    expect(approve).toHaveAttribute('data-contested', 'true')
    expect(approve.className).toContain('border-ok')
    expect(approve.className).not.toContain('bg-ok ')
  })

  it('keeps the filled primary for an ordinary approval', () => {
    renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={() => {}} />)
    const approve = screen.getByTestId('approval-approve')
    expect(approve).not.toHaveAttribute('data-contested')
    expect(approve.className).toContain('bg-ok ')
    expect(approve.className).not.toContain('border-ok')
  })
})

/**
 * A rejected `resolveApproval` leaves the card in place (it is deleted only on
 * success), so the panel must SAY the decision was not recorded, beside the
 * buttons, naming the one that failed as the retry -- a console line is not a
 * report. On success the panel closes and no notice ever shows.
 */
describe('NotificationDetailPanel — a failed decision is named beside the buttons', () => {
  beforeEach(() => { vi.mocked(api.resolveApproval).mockReset() })

  it('names a rejected Approve and offers the same button as the retry', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(new Error('resolve returned 500'))
    const onClose = vi.fn()
    renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={onClose} />)
    expect(screen.queryByTestId('notif-approval-error')).toBeNull()
    await userEvent.click(screen.getByTestId('approval-approve'))
    const notice = await screen.findByTestId('notif-approval-error')
    expect(notice).toHaveTextContent('Could not record your decision. Click Approve to retry.')
    expect(notice).not.toHaveTextContent('resolve returned 500')
    expect(onClose).not.toHaveBeenCalled()
    // The failed button is still here and still works.
    expect(screen.getByTestId('approval-approve')).toBeEnabled()
  })

  it('names a rejected Reject by its own label', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(new Error('offline'))
    renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={() => {}} />)
    await userEvent.click(screen.getByTestId('approval-reject'))
    expect(await screen.findByTestId('notif-approval-error')).toHaveTextContent('Click Reject to retry.')
  })

  it('clears the notice on the next click and closes on success', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({})
    const onClose = vi.fn()
    renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={onClose} />)
    await userEvent.click(screen.getByTestId('approval-approve'))
    await screen.findByTestId('notif-approval-error')
    await userEvent.click(screen.getByTestId('approval-approve'))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('notif-approval-error')).toBeNull()
  })
})
