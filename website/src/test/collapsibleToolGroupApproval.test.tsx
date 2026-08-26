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
