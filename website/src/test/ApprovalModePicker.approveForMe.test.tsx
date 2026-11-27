import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock('../api/client', () => ({
  api: {
    chatMode: vi.fn().mockResolvedValue({}),
    chatSlotApproveForMe: vi.fn().mockResolvedValue({ ok: true, approve_for_me: true }),
  },
}))

import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ApiError } from '../api/apiError'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import ApprovalModePicker from '../components/ApprovalModePicker'
import { createTestStore } from './helpers'
import { api } from '../api/client'
import { slotApprovalMode } from '../utils/slotApprovalMode'

function renderPicker(mode: string, approveForMe?: boolean) {
  render(
    <Provider store={createTestStore()}>
      <MemoryRouter>
        <ApprovalModePicker mode={mode} slotKey="dashboard:1" approveForMe={approveForMe} />
      </MemoryRouter>
    </Provider>,
  )
}

describe('ApprovalModePicker: Approve for me', () => {
  beforeEach(() => {
    vi.mocked(api.chatMode).mockClear()
    vi.mocked(api.chatSlotApproveForMe).mockClear()
  })

  it('offers no Approve for me row while the session offers no such option', () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const texts = screen.getAllByRole('menuitem').map(i => i.textContent || '')
    expect(texts.length).toBeGreaterThan(0)
    expect(texts.some(t => t.includes('Approve for me'))).toBe(false)
  })

  it('says KAS approves shell commands and file edits that skip the safety checks', () => {
    renderPicker('normal', false)
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const texts = screen.getAllByRole('menuitem').map(i => i.textContent || '')
    const row = texts.find(t => t.includes('Approve for me'))
    expect(row).toBeDefined()
    // Placed past Reads and before Trust, by how much it lets through.
    expect(texts.indexOf(row as string)).toBe(2)
    expect(row).toContain('KAS approves routine actions for you in this chat')
    expect(row).toContain('shell commands and file edits')
    expect(row).toContain("asks you when it isn't sure")
  })

  it('opens every caveat in place from a keyboard-reachable details item', () => {
    renderPicker('normal', false)
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const toggle = screen.getByTestId('approve-for-me-details-toggle')
    expect(toggle.getAttribute('role')).toBe('menuitem')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByTestId('approve-for-me-details')).toBeNull()
    fireEvent.click(toggle)
    const list = screen.getByTestId('approve-for-me-details')
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(toggle.getAttribute('aria-controls')).toBe(list.id)
    expect(toggle.getAttribute('aria-describedby')).toBe(list.id)
    const text = list.textContent || ''
    expect(text).toContain('Kiro Crew first checks each action KAS wants to approve for you: risky commands, sensitive files, your approval rules and hooks')
    expect(text).toContain('always-allow rules or a preset allow still run without Kiro Crew asking')
    expect(text).toContain('you get the usual approval card')
    expect(text).toContain('Blocked by KAS')
    expect(text).toContain('a channel, a schedule or anyone but you')
    expect(text).toContain('recorded in the security log')
    expect(text).not.toMatch(/every action|KAS's reason/)
    expect(text).not.toMatch(/PreToolUse|governance/)
    // The menu stays open, so the rows are still there to pick.
    expect(screen.getAllByRole('menuitem').some(i => (i.textContent || '').startsWith('Approve for me'))).toBe(true)
  })

  it('shows no details item while the row is not offered', () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    expect(screen.queryByTestId('approve-for-me-details-toggle')).toBeNull()
  })

  it('picking it writes the session option, not the approval mode', () => {
    renderPicker('normal', false)
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const row = screen.getAllByRole('menuitem').find(i => (i.textContent || '').includes('Approve for me'))!
    fireEvent.click(row)
    expect(api.chatSlotApproveForMe).toHaveBeenCalledWith('dashboard:1', true)
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('says why turning it on failed, in words with a next step', async () => {
    vi.mocked(api.chatSlotApproveForMe).mockRejectedValueOnce(
      new ApiError(409, 'a turn is in flight', JSON.stringify({ code: 'turn_in_flight' })),
    )
    renderPicker('normal', false)
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem').find(i => (i.textContent || '').includes('Approve for me'))!)
    await waitFor(() => expect(screen.getAllByText('Wait for the current reply to finish, then pick Approve for me again.').length).toBeGreaterThan(0))
    expect(screen.queryByText('a turn is in flight')).toBeNull()
  })

  it('names the chat kind it is not offered on, and falls back for anything else', async () => {
    vi.mocked(api.chatSlotApproveForMe).mockRejectedValueOnce(
      new ApiError(409, 'x', JSON.stringify({ code: 'approve_for_me_not_owners' })),
    )
    renderPicker('normal', false)
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem').find(i => (i.textContent || '').includes('Approve for me'))!)
    await waitFor(() => expect(screen.getAllByText(/only in a chat you drive from here/).length).toBeGreaterThan(0))
  })

  it('reads as the mode only when no Trust, Reads or YOLO holds the slot', () => {
    expect(slotApprovalMode(undefined, undefined, true)).toBe('approve_for_me')
    expect(slotApprovalMode(undefined, { trust: true, trust_scope: '', trust_reads: false }, true)).toBe('trust')
    expect(slotApprovalMode('yolo', undefined, true)).toBe('yolo')
    expect(slotApprovalMode(undefined, undefined, false)).toBe('normal')
  })
})
