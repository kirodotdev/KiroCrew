import { describe, it, expect, vi } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))

import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ApprovalCard from '../components/ApprovalCard'
import { ApiError } from '../api/client'

describe('ApprovalCard', () => {
  it('renders tool title when no toolInput', () => {
    render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons onApprove={() => {}} />)
    expect(screen.getByText('ls /tmp')).toBeInTheDocument()
  })

  it('does not double the Running: prefix for shell titles (#4396)', () => {
    const { container } = render(<ApprovalCard title="Running: gh api repos/o/r" toolInput="" showButtons onApprove={() => {}} />)
    const text = container.textContent || ''
    expect(text.match(/Running:/g)).toHaveLength(1)
    expect(text).toContain('gh api repos/o/r')
  })

  it('keeps the raw title in the label-less wrench branch', () => {
    const { container } = render(<ApprovalCard title="Running: gh api repos/o/r" toolInput="" showButtons={false} onApprove={() => {}} />)
    const text = container.textContent || ''
    expect(text.match(/Running:/g)).toHaveLength(1)
    expect(screen.getByText('Running: gh api repos/o/r')).toBeInTheDocument()
  })

  it('renders a non-shell title verbatim in the labeled branch (ChannelPage passes a role here)', () => {
    render(<ApprovalCard title="TaskeiGetTask" toolInput="" showButtons onApprove={() => {}} />)
    expect(screen.getByText('TaskeiGetTask')).toBeInTheDocument()
  })

  it('renders tool approval requested when toolInput present', () => {
    render(<ApprovalCard title="Running: ls" toolInput='{"command":"ls"}' showButtons onApprove={() => {}} />)
    expect(screen.getByText('Tool approval requested:')).toBeInTheDocument()
  })

  it('shows Approve and Reject buttons when showButtons=true', () => {
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={() => {}} />)
    expect(screen.getByText('Approve')).toBeInTheDocument()
    expect(screen.getByText('Reject')).toBeInTheDocument()
  })

  it('hides buttons when showButtons=false', () => {
    render(<ApprovalCard title="ls" toolInput="" showButtons={false} onApprove={() => {}} />)
    expect(screen.queryByText('Approve')).not.toBeInTheDocument()
  })

  it('calls onApprove with approved on Approve click', () => {
    const onApprove = vi.fn()
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    expect(onApprove).toHaveBeenCalledWith('approved', undefined)
  })

  it('calls onApprove with rejected on Reject click', () => {
    const onApprove = vi.fn()
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Reject'))
    expect(onApprove).toHaveBeenCalledWith('rejected', undefined)
  })

  it('shows TrustDropdown with 3 tiers for shell command', () => {
    render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getByText('Trust all tools')).toBeInTheDocument()
    const buttons = screen.getAllByRole('menuitem')
    expect(buttons.some(b => b.textContent?.includes('ls /tmp'))).toBe(true)
    expect(buttons.some(b => b.textContent?.includes('commands'))).toBe(true)
  })

  it('shows TrustDropdown with 2 tiers for non-shell tool', () => {
    render(<ApprovalCard title="TaskeiGetTask" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getByText('Trust all tools')).toBeInTheDocument()
    const buttons = screen.getAllByRole('menuitem')
    expect(buttons.some(b => b.textContent?.includes('commands'))).toBe(false)
  })

  it('calls onApprove with trust_command and pattern from TrustDropdown', () => {
    const onApprove = vi.fn()
    render(<ApprovalCard title="Running: grep -r foo ." toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const cmdBtn = buttons.find(b => b.textContent?.includes('grep -r foo'))!
    fireEvent.click(cmdBtn)
    expect(onApprove).toHaveBeenCalledWith('trust_command', 'grep -r foo .')
  })

  it('calls onApprove with trust_base from TrustDropdown', () => {
    const onApprove = vi.fn()
    render(<ApprovalCard title="Running: cat /etc/hosts" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const baseBtn = buttons.find(b => b.textContent?.includes('commands'))!
    fireEvent.click(baseBtn)
    expect(onApprove).toHaveBeenCalledWith('trust_base', 'cat *')
  })

  it('calls onApprove with trust from TrustDropdown entire tool', () => {
    const onApprove = vi.fn()
    render(<ApprovalCard title="Running: ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    expect(onApprove).toHaveBeenCalledWith('trust', undefined)
  })

  it('hides TrustDropdown when showTrust=false', () => {
    render(<ApprovalCard title="ls" toolInput="" showButtons showTrust={false} onApprove={() => {}} />)
    expect(screen.queryByText('Trust')).not.toBeInTheDocument()
  })

  // The channels surface passes hasCommand={false}: its card is titled with an
  // agent ROLE, and its backend accepts only approved/rejected/trust — so the
  // command-scoped tiers must not be offered there (#4421).
  it('offers only the plain trust action when hasCommand=false', () => {
    render(<ApprovalCard title="Researcher" toolInput="" showButtons hasCommand={false} onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toContain('Trust all tools')
  })

  it('hasCommand=false emits trust — a decision the channel backend accepts', () => {
    const onApprove = vi.fn()
    render(<ApprovalCard title="Researcher" toolInput="" showButtons hasCommand={false} onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    expect(onApprove).toHaveBeenCalledTimes(1)
    expect(onApprove).toHaveBeenCalledWith('trust', undefined)
  })

  it('hasCommand=false suppresses command tiers even for a shell-looking title', () => {
    render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons hasCommand={false} onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).not.toContain('commands')
  })

  it('keeps all three tiers when hasCommand is omitted (chat-surface regression guard)', () => {
    render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    expect(screen.getAllByRole('menuitem')).toHaveLength(3)
  })

  it('shows decided state after approval', () => {
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Approve'))
    expect(screen.getByText('Approved')).toBeInTheDocument()
    expect(screen.queryByText('Reject')).not.toBeInTheDocument()
  })

  it('shows trusted state after trust action', () => {
    render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    expect(screen.getByText(/auto-approving future calls/)).toBeInTheDocument()
  })

  it('shows trusted state for trust_command', () => {
    render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const cmdBtn = buttons.find(b => b.textContent?.includes('ls /tmp'))!
    fireEvent.click(cmdBtn)
    expect(screen.getByText(/auto-approving future calls/)).toBeInTheDocument()
  })

  it('shows rejected state', () => {
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Reject'))
    expect(screen.getByText('Rejected')).toBeInTheDocument()
  })

  it('applies ok border color for approved state', () => {
    const { container } = render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Approve'))
    expect(container.firstChild).toHaveClass('border-l-ok')
  })

  it('applies danger border color for rejected state', () => {
    const { container } = render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={() => {}} />)
    fireEvent.click(screen.getByText('Reject'))
    expect(container.firstChild).toHaveClass('border-l-danger')
  })

  it('applies warn border color initially', () => {
    const { container } = render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={() => {}} />)
    expect(container.firstChild).toHaveClass('border-l-warn')
  })

  it('rolls decided back and shows a failure state when the decision rejects (#5204)', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(500, 'internal error')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    await waitFor(() => expect(screen.queryByText('Approved')).not.toBeInTheDocument())
    expect(screen.getByRole('alert').textContent).toContain('internal error')
    // rollback re-renders the buttons so the user can retry
    expect(screen.getByText('Approve')).toBeInTheDocument()
    expect(screen.getByText('Reject')).toBeInTheDocument()
  })

  it('renders a terminal state without buttons when the approval is gone (400 no pending approval)', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(400, 'no pending approval')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('no longer waiting')
    expect(screen.queryByText('Approve')).not.toBeInTheDocument()
    expect(screen.queryByText('Reject')).not.toBeInTheDocument()
  })

  it('renders a terminal state without buttons on 404 (channel or agent gone)', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(404, 'not found')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('no longer waiting')
    expect(screen.queryByText('Approve')).not.toBeInTheDocument()
  })

  it('keeps a live approval retryable on other 400 refusals (e.g. invalid action)', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(400, 'invalid action')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('invalid action')
    expect(screen.getByText('Approve')).toBeInTheDocument()
  })

  it('returns focus to the Approve button after a failed approve', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(500, 'internal error')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    await screen.findByRole('alert')
    await waitFor(() => expect(document.activeElement?.textContent).toContain('Approve'))
  })

  it('returns focus to the Reject button after a failed reject (never Approve)', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(500, 'internal error')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Reject'))
    await screen.findByRole('alert')
    await waitFor(() => expect(document.activeElement?.textContent).toContain('Reject'))
    expect(document.activeElement?.textContent).not.toContain('Approve')
  })

  it('asserts the decision was not recorded when the server itself refused (ApiError)', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(400, 'invalid action')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain("This decision wasn't recorded: invalid action")
    expect(alert.textContent).not.toContain('may not have been recorded')
  })

  it('hedges with generic copy on a response-less transport error (no raw exception text)', async () => {
    const onApprove = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('may not have been recorded')
    expect(alert.textContent).not.toContain('Failed to fetch')
  })

  it('rolls a trust decision back on rejection instead of showing Trusted', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(404, 'channel gone')))
    render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    await waitFor(() => expect(screen.queryByText(/auto-approving future calls/)).not.toBeInTheDocument())
    expect(screen.getByRole('alert').textContent).toContain('no longer waiting')
  })

  it('shows a generic failure message when the rejection carries no message', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(500, '')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('the request failed')
  })

  it('clears the failure state on retry and keeps the decided state when the retry resolves', async () => {
    const onApprove = vi.fn()
      .mockRejectedValueOnce(new ApiError(500, 'internal error'))
      .mockResolvedValueOnce({ status: 'ok' })
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    await screen.findByRole('alert')
    fireEvent.click(screen.getByText('Approve'))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.getByText('Approved')).toBeInTheDocument()
  })

  it('keeps the decided state when the decision promise resolves', async () => {
    const onApprove = vi.fn(() => Promise.resolve({ status: 'ok' }))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    await waitFor(() => expect(onApprove).toHaveBeenCalled())
    expect(screen.getByText('Approved')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
