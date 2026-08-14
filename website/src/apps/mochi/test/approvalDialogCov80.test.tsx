/**
 * ApprovalDialog — the pet's tool-confirmation modal.
 *
 * The risk chip is the part with real logic: the raw discriminant used to render
 * straight through, so nine locales read an English risk word beside a translated
 * noun. Each level is asserted so a level dropped from the key map cannot render
 * `undefined` again.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { ApprovalDialog } from '../src/renderer/ApprovalDialog'
import type { ApprovalRequest } from '../src/shared/types'

function request(over: Partial<ApprovalRequest> = {}): ApprovalRequest {
  return {
    id: 'zzq-req',
    taskId: 'zzq-task',
    toolName: 'zzq_tool',
    paramsSummary: 'zzq params blob',
    riskLevel: 'medium',
    ...over,
  }
}

describe('ApprovalDialog', () => {
  it('shows which tool is asking and with what parameters', () => {
    render(<ApprovalDialog request={request()} onRespond={() => {}} />)
    expect(screen.getByText('Action Approval')).toBeTruthy()
    expect(screen.getByText('zzq_tool')).toBeTruthy()
    expect(screen.getByText('zzq params blob')).toBeTruthy()
  })

  it('localizes the risk word for every level the backend can send', () => {
    const { unmount } = render(<ApprovalDialog request={request({ riskLevel: 'low' })} onRespond={() => {}} />)
    expect(screen.getByText(/^low risk$/)).toBeTruthy()
    unmount()

    const mid = render(<ApprovalDialog request={request({ riskLevel: 'medium' })} onRespond={() => {}} />)
    expect(screen.getByText(/^medium risk$/)).toBeTruthy()
    mid.unmount()

    render(<ApprovalDialog request={request({ riskLevel: 'high' })} onRespond={() => {}} />)
    expect(screen.getByText(/^high risk$/)).toBeTruthy()
  })

  it('answers true from Approve and false from Reject', () => {
    const onRespond = vi.fn()
    render(<ApprovalDialog request={request()} onRespond={onRespond} />)

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(onRespond).toHaveBeenLastCalledWith(true)

    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(onRespond).toHaveBeenLastCalledWith(false)
    expect(onRespond).toHaveBeenCalledTimes(2)
  })
})
