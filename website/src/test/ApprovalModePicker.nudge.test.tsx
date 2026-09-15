import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock('../api/client', () => ({
  api: { chatMode: vi.fn().mockResolvedValue({}) },
}))

import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import ApprovalModePicker, { APPROVAL_MODE_ADJUSTED_LS_KEY } from '../components/ApprovalModePicker'
import { createTestStore } from './helpers'
import { api } from '../api/client'

function renderPicker(props: Partial<React.ComponentProps<typeof ApprovalModePicker>> = {}) {
  const store = createTestStore()
  const view = render(
    <Provider store={store}>
      <MemoryRouter>
        <ApprovalModePicker mode="normal" slotKey="dashboard:1" {...props} />
      </MemoryRouter>
    </Provider>,
  )
  const rerender = (next: Partial<React.ComponentProps<typeof ApprovalModePicker>> = {}) =>
    view.rerender(
      <Provider store={store}>
        <MemoryRouter>
          <ApprovalModePicker mode="normal" slotKey="dashboard:1" {...next} />
        </MemoryRouter>
      </Provider>,
    )
  return { store, rerender }
}

describe('ApprovalModePicker openSignal (A2)', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(api.chatMode).mockClear()
  })

  it('an openSignal bump opens the menu and spotlights the trigger', () => {
    const { rerender } = renderPicker({ openSignal: 0 })
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
    rerender({ openSignal: 1 })
    expect(screen.getAllByRole('menuitem').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('Approval mode: Normal').className).toContain('ring-accent')
  })

  it('the spotlight ring clears after its timeout', () => {
    vi.useFakeTimers()
    try {
      const { rerender } = renderPicker({ openSignal: 0 })
      rerender({ openSignal: 1 })
      expect(screen.getByLabelText('Approval mode: Normal').className).toContain('ring-accent')
      act(() => { vi.advanceTimersByTime(2100) })
      expect(screen.getByLabelText('Approval mode: Normal').className).not.toContain('ring-accent')
    } finally {
      vi.useRealTimers()
    }
  })

  it('an unchanged mount-time signal does not open the menu (slot-switch remount)', () => {
    renderPicker({ openSignal: 5 })
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
  })

  it('picking a mode records the adjusted flag for the approval-bar hint', () => {
    renderPicker()
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[2]) // Trust
    expect(localStorage.getItem(APPROVAL_MODE_ADJUSTED_LS_KEY)).toBe('1')
  })
})

describe('ApprovalModePicker nudge (B2)', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(api.chatMode).mockClear()
  })

  it('renders the callout with title, body, and both actions when nudge is set', () => {
    renderPicker({ nudge: true })
    const dialog = screen.getByRole('dialog', { name: 'Want fewer approval prompts?' })
    expect(dialog).toBeInTheDocument()
    expect(dialog.textContent).toContain('without asking first')
    expect(screen.getByText('See options')).toBeInTheDocument()
    expect(screen.getByText('Got it')).toBeInTheDocument()
  })

  it('does not render the callout when nudge is unset', () => {
    renderPicker({ nudge: false })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('"See options" dismisses via callback and opens the real menu', () => {
    const onNudgeDismiss = vi.fn()
    renderPicker({ nudge: true, onNudgeDismiss })
    fireEvent.click(screen.getByText('See options'))
    expect(onNudgeDismiss).toHaveBeenCalledTimes(1)
    expect(screen.getAllByRole('menuitem').length).toBeGreaterThan(0)
  })

  it('"Got it" dismisses without opening the menu', () => {
    const onNudgeDismiss = vi.fn()
    renderPicker({ nudge: true, onNudgeDismiss })
    fireEvent.click(screen.getByText('Got it'))
    expect(onNudgeDismiss).toHaveBeenCalled()
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
  })

  it('picking a mode while nudged also dismisses the nudge', () => {
    const onNudgeDismiss = vi.fn()
    renderPicker({ nudge: true, onNudgeDismiss })
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[2]) // Trust
    expect(onNudgeDismiss).toHaveBeenCalled()
  })
})

describe('review-round fixes', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(api.chatMode).mockClear()
  })

  it('Escape routes to the session-scoped hide, not the permanent dismissal', () => {
    const onNudgeDismiss = vi.fn()
    const onNudgeHide = vi.fn()
    renderPicker({ nudge: true, onNudgeDismiss, onNudgeHide })
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onNudgeHide).toHaveBeenCalledTimes(1)
    expect(onNudgeDismiss).not.toHaveBeenCalled()
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
  })

  it('Escape falls back to the permanent dismissal when no hide handler is wired', () => {
    const onNudgeDismiss = vi.fn()
    renderPicker({ nudge: true, onNudgeDismiss })
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onNudgeDismiss).toHaveBeenCalledTimes(1)
  })

  it('does not steal focus from an editable element the user is typing in', () => {
    const ta = document.createElement('textarea')
    document.body.appendChild(ta)
    ta.focus()
    renderPicker({ nudge: true })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(document.activeElement).toBe(ta)
    ta.remove()
  })

  it('merely OPENING the picker retires the approval-bar hint (discovery achieved)', () => {
    renderPicker() // mode="normal"
    expect(localStorage.getItem(APPROVAL_MODE_ADJUSTED_LS_KEY)).toBeNull()
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    expect(localStorage.getItem(APPROVAL_MODE_ADJUSTED_LS_KEY)).toBe('1')
  })

  it('the nudge dismiss label uses its own catalog key, not markdownPanel', () => {
    renderPicker({ nudge: true })
    // en value equals markdownPanel's, so assert via the rendered text plus
    // the component no longer referencing the borrowed namespace at runtime:
    // a missing own-key would render the raw key string instead.
    expect(screen.getByText('Got it')).toBeInTheDocument()
    expect(screen.queryByText('components.approvalModePicker.nudge_dismiss')).not.toBeInTheDocument()
  })
})

describe('nudge retirement on any open (round-2 fixes)', () => {
  beforeEach(() => { localStorage.clear() })

  it('opening the menu via the trigger dismisses an active nudge (no resurrection on close)', () => {
    const onNudgeDismiss = vi.fn()
    renderPicker({ nudge: true, onNudgeDismiss })
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    expect(onNudgeDismiss).toHaveBeenCalledTimes(1)
  })

  it('the trigger wears the spotlight ring while the callout is up (deictic anchor)', () => {
    renderPicker({ nudge: true })
    expect(screen.getByLabelText('Approval mode: Normal').className).toContain('ring-accent')
  })
})
