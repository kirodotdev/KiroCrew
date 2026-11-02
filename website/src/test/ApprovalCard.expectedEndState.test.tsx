import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))

// The card must take its status-vs-error choice from the named list, not from
// a local reading of the refusal. With the list emptied, a gone approval falls
// back to the error notice; with the real list, it is status.
const listed = vi.hoisted(() => ({ empty: false }))
vi.mock('../components/expectedEndStates', async (importOriginal) => {
  const real = await importOriginal<typeof import('../components/expectedEndStates')>()
  return { ...real, isExpectedEndState: (s: unknown) => !listed.empty && real.isExpectedEndState(s) }
})

import { render, screen, fireEvent } from '@testing-library/react'
import ApprovalCard from '../components/ApprovalCard'
import { ApiError } from '../api/client'

describe('ApprovalCard routes a refusal through the expected end-state list', () => {
  beforeEach(() => { listed.empty = false })

  it('renders the listed gone-approval 404 as status', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(404, 'not found')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    expect((await screen.findByRole('status')).textContent).toContain('nothing is waiting on it')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders the same 404 as an error notice once it is not on the list', async () => {
    listed.empty = true
    const onApprove = vi.fn(() => Promise.reject(new ApiError(404, 'not found')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    await screen.findByRole('alert')
    expect(screen.queryByTestId('approval-card-gone')).not.toBeInTheDocument()
  })

  it('keeps a non-listed failure (500) an error notice', async () => {
    const onApprove = vi.fn(() => Promise.reject(new ApiError(500, 'internal error')))
    render(<ApprovalCard title="ls" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByText('Approve'))
    expect((await screen.findByRole('alert')).textContent).toContain('internal error')
    expect(screen.queryByTestId('approval-card-gone')).not.toBeInTheDocument()
  })
})
