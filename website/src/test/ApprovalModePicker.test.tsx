import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock('../api/client', () => ({
  api: {
    chatMode: vi.fn().mockResolvedValue({}),
    viewerOwnerOk: vi.fn().mockResolvedValue(null),
    yoloAck: vi.fn().mockResolvedValue({ acknowledged: false }),
    setYoloAck: vi.fn().mockResolvedValue({ acknowledged: true }),
  },
}))

import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import ApprovalModePicker from '../components/ApprovalModePicker'
import { createTestStore } from './helpers'
import { namedCeiling } from './namedCeiling'
import { api } from '../api/client'
import { isTrustRootTarget } from '../guide/findTargetPolicy'
import { queryClient } from '../api/queryClient'
import { sharedCrewKeys } from '../hooks/sharedCrew/sharedCrewSocket'
import type { ApprovalModeRemote } from '../components/ApprovalModePicker'

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

// Every case starts from the mock defaults above, empty storage and an empty
// cache, so no case passes only because of the order the cases ran in.
beforeEach(() => {
  localStorage.clear()
  queryClient.clear()
  vi.mocked(api.chatMode).mockReset().mockResolvedValue({})
  vi.mocked(api.viewerOwnerOk).mockReset().mockResolvedValue(null)
  vi.mocked(api.yoloAck).mockReset().mockResolvedValue({ acknowledged: false })
  vi.mocked(api.setYoloAck).mockReset().mockResolvedValue({ acknowledged: true })
})

describe('ApprovalModePicker', () => {
  it('renders trigger with current mode label', () => {
    renderPicker('trust')
    expect(screen.getByText('Trust')).toBeInTheDocument()
    expect(screen.queryByText('YOLO mode is an app-wide setting')).not.toBeInTheDocument()
  })

  it('marks its trigger as the agent\'s own ceiling, so no guide resolves or points at it', () => {
    window.history.replaceState(null, '', '/chat')
    renderPicker('normal')
    expect(isTrustRootTarget(screen.getByLabelText('Approval mode: Normal'), 'Approval mode')).toBe(true)
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

  it('says Reads and Trust also cover the sessions this chat starts', () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const [, reads, trust] = screen.getAllByRole('menuitem').map(i => i.textContent || '')
    expect(reads).toContain('the sessions it starts')
    expect(trust).toContain('the sessions it starts')
  })

  // Read-only shell commands are auto-approved in every mode, so Normal must not
  // promise to ask first and Reads must not claim them as its difference.
  it('says Normal runs read-only shell commands on its own', () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    const [normal, reads] = screen.getAllByRole('menuitem').map(i => i.textContent || '')
    expect(normal).toContain('running read-only shell commands')
    expect(normal).not.toContain('before doing anything')
    expect(reads).not.toContain('read-only shell commands')
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

  it("ticking Don't show again then Enable persists mc-yolo-ack", async () => {
    renderPicker('normal')
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[3])
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByText('Enable'))
    expect(localStorage.getItem('mc-yolo-ack')).toBe('1')
    await waitFor(() => expect(queryClient.getQueryData(['chat-yolo-ack'])).toEqual({ acknowledged: true }))
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


/** The machine owner's *Don't show again* is also kept on the gateway
 *  (`/api/chat/yolo-ack`), so a fresh browser profile with an empty localStorage
 *  still skips the confirmation for the owner. A teammate and a remote session
 *  never read or write that record. */
describe('ApprovalModePicker — the owner acknowledgement kept on the gateway', () => {
  const ACK_KEY = ['chat-yolo-ack']
  const OWNER_KEY = [...sharedCrewKeys.viewer, 'owner-ok']
  // The first record read waits for the owner check: owner-ok answers, then chat-yolo-ack.
  const ACK_READ = () => namedCeiling('ACK_READ_AFTER_OWNER_OK', 5000)

  beforeEach(() => {
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(true)
  })
  afterEach(() => {
    cleanup()
    queryClient.clear()
  })

  function renderWith(remote?: ApprovalModeRemote) {
    render(
      <Provider store={createTestStore()}>
        <MemoryRouter>
          <ApprovalModePicker mode="normal" slotKey="dashboard:1" remote={remote} />
        </MemoryRouter>
      </Provider>,
    )
  }
  const pickYolo = () => {
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    fireEvent.click(screen.getAllByRole('menuitem')[3])
  }
  const ownerAnswered = () => waitFor(() => expect(queryClient.getQueryState(OWNER_KEY)?.status).toBe('success'))
  // The notices' own copy; the raw error text must never reach the reader.
  const READ_FAILED = "Couldn't check whether you turned this confirmation off, so it is showing. Enable still works."
  const SAVE_FAILED = '“Don\'t show again” was saved only in this browser, so other browsers will still ask before YOLO turns on. The switch to YOLO itself is not affected.'

  it('an owner whose record is acknowledged switches to YOLO without the confirmation', async () => {
    vi.mocked(api.yoloAck).mockResolvedValue({ acknowledged: true })
    renderWith()
    await waitFor(() => expect(queryClient.getQueryData(ACK_KEY)).toEqual({ acknowledged: true }), ACK_READ())
    pickYolo()
    expect(screen.queryByText('YOLO mode is an app-wide setting')).not.toBeInTheDocument()
    expect(api.chatMode).toHaveBeenCalledWith('yolo', 'dashboard:1')
    expect(api.yoloAck).toHaveBeenCalledTimes(1)
  })

  it('a teammate never fetches the record and sees the confirmation', async () => {
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(false)
    renderWith()
    await ownerAnswered()
    pickYolo()
    expect(screen.getByText('YOLO mode is an app-wide setting')).toBeInTheDocument()
    expect(api.yoloAck).not.toHaveBeenCalled()
    expect(api.chatMode).not.toHaveBeenCalled()
  })

  it('a record that cannot be read counts as absent: the confirmation shows', async () => {
    vi.mocked(api.yoloAck).mockRejectedValue(new Error('gateway unreachable'))
    renderWith()
    await waitFor(() => expect(queryClient.getQueryState(ACK_KEY)?.status).toBe('error'), ACK_READ())
    pickYolo()
    expect(screen.getByText('YOLO mode is an app-wide setting')).toBeInTheDocument()
    expect(api.chatMode).not.toHaveBeenCalled()
    // The failed read is reported in the menu while the confirmation shows.
    expect(screen.getByText(READ_FAILED)).toBeInTheDocument()
    expect(screen.queryByText('gateway unreachable')).not.toBeInTheDocument()
  })

  it('Enable with the box ticked as the owner writes the record and still writes localStorage', async () => {
    renderWith()
    await waitFor(() => expect(queryClient.getQueryData(ACK_KEY)).toEqual({ acknowledged: false }), ACK_READ())
    pickYolo()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByText('Enable'))
    expect(api.setYoloAck).toHaveBeenCalledWith(true)
    expect(localStorage.getItem('mc-yolo-ack')).toBe('1')
    expect(api.chatMode).toHaveBeenCalledWith('yolo', 'dashboard:1')
    await waitFor(() => expect(queryClient.getQueryData(ACK_KEY)).toEqual({ acknowledged: true }))
  })

  it('a failed write neither blocks nor undoes the switch', async () => {
    vi.mocked(api.setYoloAck).mockRejectedValue(new Error('write failed'))
    renderWith()
    await waitFor(() => expect(queryClient.getQueryData(ACK_KEY)).toEqual({ acknowledged: false }), ACK_READ())
    pickYolo()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByText('Enable'))
    expect(api.setYoloAck).toHaveBeenCalledWith(true)
    expect(api.chatMode).toHaveBeenCalledWith('yolo', 'dashboard:1')
    expect(localStorage.getItem('mc-yolo-ack')).toBe('1')
    // The menu re-opens to report the failed save; the switch itself stands.
    // Awaited first, so the rejection's state updates land inside a wait.
    expect(await screen.findByText(SAVE_FAILED)).toBeInTheDocument()
    await expect(vi.mocked(api.setYoloAck).mock.results[0].value).rejects.toThrow('write failed')
    expect(screen.queryByText('write failed')).not.toBeInTheDocument()
    // A partial failure, read politely: the switch itself worked.
    expect(screen.getByText(SAVE_FAILED).closest('[role]')).toHaveAttribute('role', 'status')
    expect(queryClient.getQueryData(ACK_KEY)).toEqual({ acknowledged: false })
    expect(screen.getByRole('menu')).toBeInTheDocument()
    expect(api.chatMode).toHaveBeenCalledWith('yolo', 'dashboard:1')
  })

  it('an owner check that failed still sends the write, for the gateway to decide', async () => {
    vi.mocked(api.viewerOwnerOk).mockRejectedValue(new Error('owner check failed'))
    renderWith()
    await waitFor(() => expect(queryClient.getQueryState(OWNER_KEY)?.status).toBe('error'))
    pickYolo()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByText('Enable'))
    expect(api.setYoloAck).toHaveBeenCalledWith(true)
    expect(localStorage.getItem('mc-yolo-ack')).toBe('1')
    expect(api.chatMode).toHaveBeenCalledWith('yolo', 'dashboard:1')
    await waitFor(() => expect(queryClient.getQueryData(ACK_KEY)).toEqual({ acknowledged: true }))
  })

  it('a failed owner check is reported while the confirmation shows', async () => {
    vi.mocked(api.viewerOwnerOk).mockRejectedValue(new Error('owner check failed'))
    renderWith()
    await waitFor(() => expect(queryClient.getQueryState(OWNER_KEY)?.status).toBe('error'))
    pickYolo()
    expect(screen.getByText('YOLO mode is an app-wide setting')).toBeInTheDocument()
    expect(api.chatMode).not.toHaveBeenCalled()
    expect(api.yoloAck).not.toHaveBeenCalled()
    expect(screen.getByText(READ_FAILED)).toBeInTheDocument()
    expect(screen.queryByText('owner check failed')).not.toBeInTheDocument()
  })

  it('Enable with the box ticked as a teammate writes localStorage only', async () => {
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(false)
    renderWith()
    await ownerAnswered()
    pickYolo()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByText('Enable'))
    expect(localStorage.getItem('mc-yolo-ack')).toBe('1')
    expect(api.setYoloAck).not.toHaveBeenCalled()
    expect(api.chatMode).toHaveBeenCalledWith('yolo', 'dashboard:1')
  })

  it('a remote session never fetches the record', async () => {
    renderWith({ write: vi.fn().mockResolvedValue({}), trustScoped: false, note: '' })
    await ownerAnswered()
    fireEvent.click(screen.getByLabelText('Approval mode: Normal'))
    expect(api.viewerOwnerOk).toHaveBeenCalled()
    expect(api.yoloAck).not.toHaveBeenCalled()
  })
})
