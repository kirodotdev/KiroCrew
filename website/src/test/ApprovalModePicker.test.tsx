import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock('../api/client', () => ({
  api: { chatMode: vi.fn().mockResolvedValue({}) },
}))

import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import ApprovalModePicker from '../components/ApprovalModePicker'
import { createTestStore } from './helpers'
import { api } from '../api/client'

function renderPicker(mode = 'normal', compact = false) {
  const store = createTestStore()
  // MemoryRouter: the picker links to the duration setting via useNavigate.
  render(
    <Provider store={store}>
      <MemoryRouter>
        <ApprovalModePicker mode={mode} slotKey="dashboard:1" compact={compact} />
      </MemoryRouter>
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

/** The trigger pill is CHROME: "Normal" / "Reads" / "Trust" / "YOLO" are labels,
 *  so the pill must follow the user's Font Family choice (`--font-body`).
 *  Tailwind's `font-mono` resolves to `var(--mono)`, a token that setting never
 *  writes, so a `font-mono` here would pin JetBrains Mono in every mode. */
describe('ApprovalModePicker — trigger follows the Font Family setting', () => {
  beforeEach(() => { localStorage.clear() })

  it('does not pin the trigger to font-mono in any mode', () => {
    for (const [mode, label] of [['normal', 'Normal'], ['trust_reads', 'Reads'], ['trust', 'Trust'], ['yolo', 'YOLO']] as const) {
      renderPicker(mode)
      expect(screen.getByLabelText(`Approval mode: ${label}`).className).not.toContain('font-mono')
      cleanup()
    }
  })
})


/** The `approval_modes` policy scope surfaces its deny-list on the status frame
 *  as `disabled_approval_modes`. The picker HIDES each listed mode outright
 *  (maintainer feedback: hide, don't grey) — a mode that cannot be chosen is
 *  simply absent, and `normal` (never deniable) keeps the menu non-empty.
 *
 *  ONE exception: a denied mode that is STILL THE ACTIVE one keeps its row,
 *  disabled and labelled with the reason. The trigger renders `mode` unfiltered,
 *  so hiding that row too would show "Trust" on the button with no Trust row and
 *  no checkmark anywhere — a control contradicting itself with no explanation. */
describe('ApprovalModePicker — policy-disabled modes are hidden, not greyed', () => {
  beforeEach(() => { localStorage.clear() })

  function renderWithDisabled(disabled: string[], mode = 'normal') {
    const store = createTestStore({
      dashboard: { status: { disabled_approval_modes: disabled } } as never,
    })
    render(
      <Provider store={store}>
        <MemoryRouter>
          <ApprovalModePicker mode={mode} slotKey="dashboard:1" />
        </MemoryRouter>
      </Provider>,
    )
    return store
  }

  it('hides each mode listed in disabled_approval_modes', () => {
    renderWithDisabled(['yolo', 'trust_reads'])
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const items = screen.getAllByRole('menuitem')
    const texts = items.map(i => i.textContent || '')
    expect(items).toHaveLength(2)
    expect(texts.some(t => t.includes('Normal'))).toBe(true)
    expect(texts.some(t => t.includes('Trust'))).toBe(true)
    expect(texts.some(t => t.includes('YOLO'))).toBe(false)
    expect(texts.some(t => t.includes('Reads'))).toBe(false)
    // Hidden, not greyed: no policy-reason line for a mode that isn't active.
    expect(screen.queryByText(/Disabled by your organization/i)).not.toBeInTheDocument()
  })

  it('shows all four modes when disabled_approval_modes is empty', () => {
    renderWithDisabled([])
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    expect(screen.getAllByRole('menuitem')).toHaveLength(4)
  })

  it('leaves only Normal selectable when every auto-approve mode is denied', () => {
    renderWithDisabled(['trust_reads', 'trust', 'yolo'])
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toContain('Normal')
  })

  it('keeps the ACTIVE denied mode visible, disabled, with the policy reason', () => {
    // Policy landed while this slot was already in `trust`.
    renderWithDisabled(['trust', 'yolo'], 'trust')
    fireEvent.click(screen.getByLabelText('Approval mode: Trust'))
    const items = screen.getAllByRole('menuitem')
    const texts = items.map(i => i.textContent || '')
    // Normal + Reads (not denied) + the active-but-denied Trust; YOLO is hidden.
    expect(items).toHaveLength(3)
    expect(texts.some(t => t.includes('Trust'))).toBe(true)
    expect(texts.some(t => t.includes('YOLO'))).toBe(false)
    // The reason is on the row, and the row cannot be chosen.
    expect(screen.getByText(/Disabled by your organization/i)).toBeInTheDocument()
    // And it cannot be chosen. Asserted behaviorally rather than on Radix's
    // `data-disabled`, because this suite stubs @radix-ui/react-dropdown-menu —
    // the stub forwards `disabled` without Radix's pointer-events handling, so
    // the guard inside `onSelect` is what has to hold here (and in production it
    // is the second of the two).
    vi.mocked(api.chatMode).mockClear()
    const trustRow = items.find(i => (i.textContent || '').includes('Trust'))!
    fireEvent.click(trustRow)
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('reports a 403 policy refusal instead of failing silently', async () => {
    // The load race: before the first status frame `disabled_approval_modes` is
    // undefined, so every mode renders and a denied pick reaches the gateway.
    const { ApiError } = await import('../api/apiError')
    vi.mocked(api.chatMode).mockRejectedValueOnce(
      new ApiError(403, 'denied', JSON.stringify({ code: 'mode_disabled_by_policy' })),
    )
    renderWithDisabled([])
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const reads = screen.getAllByRole('menuitem').find(i => (i.textContent || '').includes('Reads'))!
    fireEvent.click(reads)
    expect(await screen.findByText(/Disabled by your organization/i)).toBeInTheDocument()
  })
})
