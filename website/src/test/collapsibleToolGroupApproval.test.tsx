/**
 * CollapsibleToolGroup — approval row reachability across disclosure states (#5487).
 *
 * The approval row (command preview + Approve/Trust/Reject) must render in BOTH
 * disclosure states. ChatMessageList auto-expands recent groups while the agent
 * is running — exactly when a pending approval arrives — and grouped permission
 * messages render null inside the children, so an expanded pending group whose
 * approval row is gated on !expanded is a dead end: the agent is parked waiting
 * on a decision the user has no buttons to give. This file is the pinning test
 * named by docs/system-specs/modules/ops-mission-control.md.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import { i18nT } from '../i18n/t'

const T = (k: string, vars?: Record<string, unknown>) => i18nT(`pages.chat.collapsibleToolGroup.${k}`, vars)

/** The one header button, whatever label it is currently wearing. */
const header = () => screen.getAllByRole('button')[0]

afterEach(() => vi.restoreAllMocks())

describe('CollapsibleToolGroup approval row across disclosure states (#5487)', () => {
  it('keeps the approval buttons and command preview visible while auto-expanded', () => {
    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission autoExpand isRunning permissionMeta={{ tool_input: 'zzq --run' }} onApprove={() => {}}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    // The group arrived auto-expanded (children visible)…
    expect(header()).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('zzq-child')).toBeInTheDocument()
    // …and the approval row must still be actionable: preview + both decisions.
    expect(screen.getByText('zzq --run')).toBeInTheDocument()
    expect(screen.getByText(T('approve'))).toBeInTheDocument()
    expect(screen.getByText(T('reject'))).toBeInTheDocument()
  })

  it('keeps the approval row through a manual expand/collapse round-trip', () => {
    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission permissionMeta={{ tool_input: 'zzq --run' }} onApprove={() => {}}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    // Collapsed: row present (the pre-#5487 behavior).
    expect(screen.getByText(T('approve'))).toBeInTheDocument()
    // Expanded by the user: the row must not vanish.
    fireEvent.click(header())
    expect(header()).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText(T('approve'))).toBeInTheDocument()
    expect(screen.getByText(T('reject'))).toBeInTheDocument()
    // And back to collapsed: still present.
    fireEvent.click(header())
    expect(header()).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByText(T('approve'))).toBeInTheDocument()
  })

  it('dispatches a decision from the expanded state and reflects it in the header', async () => {
    const onApprove = vi.fn()
    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission autoExpand isRunning permissionMeta={{ tool_input: 'zzq --run' }} onApprove={onApprove}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    fireEvent.click(screen.getByText(T('approve')))
    // Dispatch rides a microtask (Promise.resolve().then in submitDecision).
    await waitFor(() => expect(onApprove).toHaveBeenCalledWith('approved'))
    // Optimistic resolution replaces the row: buttons gone, header says approved.
    expect(screen.queryByText(T('reject'))).not.toBeInTheDocument()
    expect(header().textContent).toContain(T('approved'))
  })

  it('offers Trust in the expanded state only on a canTrust mount (#5434 contract preserved)', () => {
    const { unmount } = renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission autoExpand isRunning canTrust permissionMeta={{ tool_input: 'zzq --run' }} onApprove={() => {}}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    expect(screen.getByText(T('trust'))).toBeInTheDocument()
    unmount()

    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission autoExpand isRunning permissionMeta={{ tool_input: 'zzq --run' }} onApprove={() => {}}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    expect(screen.queryByText(T('trust'))).not.toBeInTheDocument()
  })

  it('renders no approval row without an onApprove handler, expanded or not', () => {
    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission autoExpand isRunning permissionMeta={{ tool_input: 'zzq --run' }}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    expect(screen.queryByText(T('approve'))).not.toBeInTheDocument()
    expect(screen.queryByText('zzq --run')).not.toBeInTheDocument()
  })
})

describe('CollapsibleToolGroup batch multi-select (Req 4.1-4.4)', () => {
  it('shows "approve/reject all N" labels and routes ONE click to onApproveBatch when >1 pending', async () => {
    const onApprove = vi.fn(() => Promise.resolve())
    const onApproveBatch = vi.fn(() => Promise.resolve())
    renderWithProviders(
      <CollapsibleToolGroup count={3} hasPermission autoExpand isRunning pendingPermCount={3}
        permissionMeta={{ tool_input: 'zzq --run' }} onApprove={onApprove} onApproveBatch={onApproveBatch}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    // Buttons advertise the batch scope with the pending count.
    const approveAll = screen.getByText(T('approve_all', { count: 3 }))
    expect(approveAll).toBeInTheDocument()
    expect(screen.getByText(T('reject_all', { count: 3 }))).toBeInTheDocument()
    // One click resolves the whole batch via onApproveBatch, NOT the single path.
    fireEvent.click(approveAll)
    await waitFor(() => expect(onApproveBatch).toHaveBeenCalledWith('approved'))
    expect(onApprove).not.toHaveBeenCalled()
    // Optimistic resolution collapses the row into the resolved header.
    expect(header().textContent).toContain(T('approved'))
  })

  it('uses the id-scoped onApprove path (NOT batch) when only one approval is pending', async () => {
    const onApprove = vi.fn(() => Promise.resolve())
    const onApproveBatch = vi.fn(() => Promise.resolve())
    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission autoExpand isRunning pendingPermCount={1}
        permissionMeta={{ tool_input: 'zzq --run' }} onApprove={onApprove} onApproveBatch={onApproveBatch}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    // Single pending -> plain labels, single-approve routing preserved (T3 untouched).
    fireEvent.click(screen.getByText(T('approve')))
    await waitFor(() => expect(onApprove).toHaveBeenCalledWith('approved'))
    expect(onApproveBatch).not.toHaveBeenCalled()
  })

  it('previews EVERY pending command when batching (not just the newest), closing the approve-unseen gap', () => {
    renderWithProviders(
      <CollapsibleToolGroup count={3} hasPermission autoExpand isRunning pendingPermCount={3}
        permissionMeta={{ tool_input: 'cmd-charlie' }}
        permissionMetas={[{ tool_input: 'cmd-alpha' }, { tool_input: 'cmd-bravo' }, { tool_input: 'cmd-charlie' }]}
        onApproveBatch={vi.fn(() => Promise.resolve())}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    // All three pending commands are shown so "Approve all 3" is not blind —
    // the earlier calls (alpha, bravo), not just the newest (charlie), render.
    expect(screen.getByText('cmd-alpha')).toBeInTheDocument()
    expect(screen.getByText('cmd-bravo')).toBeInTheDocument()
    expect(screen.getByText('cmd-charlie')).toBeInTheDocument()
  })

  it('renders a placeholder row for a pending call with no derivable command, so N rows == N approvals', () => {
    renderWithProviders(
      <CollapsibleToolGroup count={3} hasPermission autoExpand isRunning pendingPermCount={3}
        permissionMeta={{ tool_input: 'cmd-charlie' }}
        permissionMetas={[{ tool_input: 'cmd-alpha' }, {}, { tool_input: 'cmd-charlie' }]}
        onApproveBatch={vi.fn(() => Promise.resolve())}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    // The two extractable commands render; the meta with no derivable command
    // is NOT dropped — it shows the placeholder, so the user is never told
    // "all 3 below" while silently seeing only 2.
    expect(screen.getByText('cmd-alpha')).toBeInTheDocument()
    expect(screen.getByText('cmd-charlie')).toBeInTheDocument()
    expect(screen.getByText(T('batch_preview_none'))).toBeInTheDocument()
  })

  it('rolls the row back to actionable when a batch decision fails', async () => {
    const onApproveBatch = vi.fn(() => Promise.reject(new Error('boom')))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    renderWithProviders(
      <CollapsibleToolGroup count={2} hasPermission autoExpand isRunning pendingPermCount={2}
        permissionMeta={{ tool_input: 'zzq --run' }} onApproveBatch={onApproveBatch}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    fireEvent.click(screen.getByText(T('reject_all', { count: 2 })))
    await waitFor(() => expect(onApproveBatch).toHaveBeenCalledWith('rejected'))
    // Failure restores the buttons so the user can retry the decision.
    const rejectAll = await screen.findByText(T('reject_all', { count: 2 }))
    expect(rejectAll).toHaveFocus()
    expect(screen.getByRole('alert')).toHaveTextContent(
      i18nT('components.approvalCard.decision_failed'),
    )
  })
})
