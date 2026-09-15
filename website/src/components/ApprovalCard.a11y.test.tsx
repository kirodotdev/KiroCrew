import { render, screen, within } from '@testing-library/react'
import ApprovalCard from './ApprovalCard'

// Task 2 / Req 2.5: the approval controls must read as one keyboard-operable,
// ARIA-labeled decision cluster (operators batch-approve).
describe('ApprovalCard accessibility', () => {
  const noop = () => Promise.resolve()

  it('groups the decision controls under a labeled role=group', () => {
    render(<ApprovalCard title="ls -la" toolInput="" showButtons showTrust={false} onApprove={noop} />)
    const group = screen.getByRole('group', { name: /approval actions/i })
    expect(group).toBeInTheDocument()
    // Approve and Reject live inside the group and are individually labeled.
    expect(within(group).getByRole('button', { name: /^approve$/i })).toBeInTheDocument()
    expect(within(group).getByRole('button', { name: /^reject$/i })).toBeInTheDocument()
  })

  it('renders no action group once a decision has been made', () => {
    // No buttons requested -> no group at all (nothing to label).
    render(<ApprovalCard title="ls -la" toolInput="" showButtons={false} onApprove={noop} />)
    expect(screen.queryByRole('group', { name: /approval actions/i })).not.toBeInTheDocument()
  })
})
