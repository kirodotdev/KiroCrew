import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock('../api/client', () => ({
  api: { chatMode: vi.fn().mockResolvedValue({}) },
}))

import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import ApprovalModePicker from '../components/ApprovalModePicker'
import { createTestStore } from './helpers'
import { api } from '../api/client'

function renderPicker(mode = 'normal', compact = false) {
  const store = createTestStore()
  render(
    <Provider store={store}>
      <ApprovalModePicker mode={mode} slotKey="dashboard:1" compact={compact} />
    </Provider>,
  )
  return store
}

describe('ApprovalModePicker', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(api.chatMode).mockClear()
  })

  it('renders trigger with current mode label', () => {
    renderPicker('trust')
    expect(screen.getByText('Trust')).toBeInTheDocument()
    expect(screen.queryByText('YOLO mode is an app-wide setting')).not.toBeInTheDocument()
  })

  it('compact trigger hides the label but keeps the aria-label', () => {
    renderPicker('normal', true)
    const trigger = screen.getByLabelText('Approval mode: Normal')
    expect(trigger.textContent).not.toContain('Normal')
  })

  it('opens with all four modes and marks the current one', () => {
    renderPicker('trust_reads')
    fireEvent.click(screen.getByLabelText('Approval mode: Reads'))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(4)
    const texts = items.map(i => i.textContent || '')
    expect(texts.some(t => t.includes('Normal'))).toBe(true)
    expect(texts.some(t => t.includes('YOLO'))).toBe(true)
  })

  it('selecting a non-yolo mode dispatches changeApprovalMode with the slot', () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[2]) // Trust
    expect(api.chatMode).toHaveBeenCalledWith('trust', 'dashboard:1')
  })

  it('selecting YOLO without ack shows the confirm card and does not dispatch', () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[3]) // YOLO
    expect(screen.getByText('YOLO mode is an app-wide setting')).toBeInTheDocument()
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('Enable in the confirm card dispatches yolo; ack persisted only when checkbox ticked', () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[3])
    // no checkbox tick -> Enable must NOT persist the ack
    fireEvent.click(screen.getByText('Enable'))
    expect(api.chatMode).toHaveBeenCalledWith('yolo', 'dashboard:1')
    expect(localStorage.getItem('mc-yolo-ack')).toBeNull()
  })

  it("ticking Don't show again then Enable persists mc-yolo-ack", () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[3])
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByText('Enable'))
    expect(localStorage.getItem('mc-yolo-ack')).toBe('1')
  })

  it('check-then-Cancel does NOT persist the ack (gate cannot be silently disabled)', () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[3])
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByText('Cancel'))
    expect(localStorage.getItem('mc-yolo-ack')).toBeNull()
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('with stored ack, selecting YOLO dispatches immediately without confirm card', () => {
    localStorage.setItem('mc-yolo-ack', '1')
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[3])
    expect(screen.queryByText('YOLO mode is an app-wide setting')).not.toBeInTheDocument()
    expect(api.chatMode).toHaveBeenCalledWith('yolo', 'dashboard:1')
  })

  it('selecting YOLO while already in yolo mode is a no-op', () => {
    renderPicker('yolo')
    fireEvent.click(screen.getByLabelText('Approval mode: YOLO'))
    fireEvent.click(screen.getAllByRole('menuitem')[3])
    expect(api.chatMode).not.toHaveBeenCalled()
    expect(screen.queryByText('YOLO mode is an app-wide setting')).not.toBeInTheDocument()
  })
})
