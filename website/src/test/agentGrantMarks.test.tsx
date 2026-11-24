/**
 * The tool-approval prompt marks which of its controls grant an agent permission.
 *
 * A surface where a press can be a slip reads the mark and does not pass a plain press on a granting control (the XR
 * page's live panels; the gateway's guard reads the same two attributes). So a grant button that loses its mark
 * would be pressed by a stray pinch: this test is what holds the marks in place.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

import { render, screen, fireEvent, within } from '@testing-library/react'
import ApprovalCard from '../components/ApprovalCard'
import DenyOverrideDropdown from '../components/DenyOverrideDropdown'
import { ApprovalActions } from '../components/chat-input/ApprovalActions'
import type { useToolApproval } from '../components/chat-input/approval'
import { AGENT_GRANT_ATTR, GUIDE_TRUST_ROOT_ATTR } from '../guide/trustRoot'

/** The rule a guarded surface applies: the nearest mark decides; with none, a trust-root region counts as granting. */
function grants(el: Element): boolean {
  for (let n: Element | null = el; n; n = n.parentElement) {
    if (n.hasAttribute(AGENT_GRANT_ATTR)) return n.getAttribute(AGENT_GRANT_ATTR) !== 'no'
    if (n.hasAttribute(GUIDE_TRUST_ROOT_ATTR)) return true
  }
  return false
}

const button = (name: string | RegExp) => screen.getByRole('button', { name })

function actions(over: Partial<ReturnType<typeof useToolApproval>> = {}) {
  const approval = {
    approvalId: 'a1', approvalMid: 'm1', approvalSubmitting: false, approvalDenyRule: '', approvalDenyFamily: '',
    approvalTrustGrantable: true, approvalTrustCommandGrantable: true, approvalTrustAllGrantable: true,
    approvalIsReadOnly: false, approvalAlwaysAllowRef: null, approvalAlwaysAllowRefusal: null,
    approvalFullCommand: 'ls /tmp', approvalBaseCommand: 'ls', approvalIsShell: true, approvalTrustBaseGrantable: true,
    approvalTakesReason: false, handleApprovalAction: vi.fn(), approvalActionsRef: () => {},
    ...over,
  } as unknown as ReturnType<typeof useToolApproval>
  return render(<ApprovalActions approval={approval} slotId="s1" />)
}

describe('the grant marks of the tool-approval prompt', () => {
  it('marks Allow once and Trust as granting, and Reject as not, in the composer prompt', () => {
    actions()
    expect(button(/Allow once/)).toHaveAttribute(AGENT_GRANT_ATTR, 'yes')
    expect(button(/^Trust/)).toHaveAttribute(AGENT_GRANT_ATTR, 'yes')
    expect(button(/Reject/)).toHaveAttribute(AGENT_GRANT_ATTR, 'no')
  })

  it('marks every choice in the Trust menu as granting', () => {
    actions()
    fireEvent.click(button(/^Trust/))
    const items = within(screen.getByRole('menu')).getAllByRole('menuitem')
    expect(items.length).toBeGreaterThan(1)
    for (const item of items) expect(grants(item)).toBe(true)
  })

  it('marks the choices in the Reject menu as not granting', () => {
    actions()
    fireEvent.click(button(/Reject/))
    const items = within(screen.getByRole('menu')).getAllByRole('menuitem')
    expect(items).toHaveLength(2)
    for (const item of items) expect(grants(item)).toBe(false)
  })

  it('marks a lone Trust tier, which is its own button, as granting', () => {
    actions({ approvalTrustCommandGrantable: false, approvalTrustAllGrantable: false, approvalIsReadOnly: true, approvalIsShell: false, approvalFullCommand: '', approvalBaseCommand: '' })
    const grantsHere = screen.getAllByRole('button').filter(b => b.getAttribute(AGENT_GRANT_ATTR) === 'yes')
    // Allow once, and the Trust control in whichever form it takes.
    expect(grantsHere.length).toBeGreaterThanOrEqual(2)
  })

  it('marks the override of a refused command, and every scope in its menu, as granting', () => {
    render(<DenyOverrideDropdown family="" onAction={() => {}} />)
    const trigger = screen.getByTestId('deny-override-trigger')
    expect(trigger).toHaveAttribute(AGENT_GRANT_ATTR, 'yes')
    fireEvent.click(trigger)
    for (const item of within(screen.getByRole('menu')).getAllByRole('menuitem')) expect(grants(item)).toBe(true)
  })

  it('marks Approve and Trust as granting, and Reject as not, on the transcript card', () => {
    const { container } = render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons onApprove={() => {}} />)
    expect(button(/Approve/)).toHaveAttribute(AGENT_GRANT_ATTR, 'yes')
    expect(button(/Reject/)).toHaveAttribute(AGENT_GRANT_ATTR, 'no')
    // The card is a trust-root region, so a control that loses its mark is still read as granting.
    expect(container.querySelector(`[${GUIDE_TRUST_ROOT_ATTR}]`)).not.toBeNull()
    for (const b of screen.getAllByRole('button')) if (b !== button(/Reject/)) expect(grants(b)).toBe(true)
    expect(grants(button(/Reject/))).toBe(false)
  })
})
