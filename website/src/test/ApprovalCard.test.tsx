import { describe, it, expect, vi } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))

import { render, screen, fireEvent } from '@testing-library/react'
import ApprovalCard from '../components/ApprovalCard'

describe('ApprovalCard', () => {
  it('renders tool title when no toolInput', () => {
    render(<ApprovalCard title="Running: ls /tmp" toolInput="" showButtons onApprove={() => {}} />)
    expect(screen.getByText('Running: ls /tmp')).toBeInTheDocument()
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
})
