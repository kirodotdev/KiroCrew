import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { api, ApiError } from '../api/client'
import { i18nT } from '../i18n/t'
import { queryPendingApprovalAction } from '../pages/chat/composerFocus'
import { sseSubagentPending, sseSubagentSpawn, sseSubagentTool } from '../store/chatSlice'
import type { RootState } from '../store'
import { reconcileGoneSubagent, sseChatMessage } from '../store/chatSlice'
import { SlotProvider } from '../providers/SlotContext'

vi.mock('../api/client', () => {
  // Declared INSIDE the factory: vi.mock is hoisted above module-level
  // declarations, so a top-level class here would be a TDZ error.
  //
  // Must be a real class: ChatInput narrows the rejection with
  // `err instanceof ApiError` to tell an orphaned approval (404) from a
  // genuine transport failure. A bare object stub makes that check false.
  class MockApiError extends Error {
    readonly status: number
    readonly body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  }
  return {
    api: {
      resolveApproval: vi.fn(() => Promise.resolve({})),
      decideApproval: vi.fn(() => Promise.resolve({})),
      approveChatSlot: vi.fn(() => Promise.resolve({})),
      spawnList: vi.fn(() => Promise.resolve({ agents: [] })),
      // A personal Crew by default: no Shared Crew capability is reported.
      sharedCrewCapabilities: vi.fn(() => Promise.resolve(null)),
      sharedCrewMode: vi.fn(() => Promise.resolve(null)),
      sharedCrewPeople: vi.fn(() => Promise.resolve(null)),
      sharedCrewViewer: vi.fn(() => Promise.resolve(null)),
    },
    ApiError: MockApiError,
  }
})

/** The request the fixture row names: the runner's own, in slot-1. */
const NATIVE = { origin: 'native', id: 'ap-123', slot: 'slot-1', mid: 'mid-123' }
/** A pending spawn's coordinator request. */
const spawnTarget = (agent: string) => ({ origin: 'coordinator' as const, id: `spawn:${agent}`, slot: 'slot-1', instance: `inst-${agent}` })

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

/** Open the reject dropdown and click one of its two tiers. */
function rejectVia(label: 'Reject once' | 'Reject all') {
  fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
  const item = screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith(label))!
  expect(item).toBeTruthy()
  fireEvent.click(item)
}

/** The approval row's controls in order, by visible label. */
function barControls(): string[] {
  const row = document.querySelector('[data-approval-actions]')!
  return Array.from(row.querySelectorAll('button')).map(b => b.textContent || '')
}

/** The bar's Trust control holds its slot, disabled, and grants nothing. */
function expectTrustHeldDisabled() {
  expect(screen.getByRole('button', { name: 'Trust' })).toBeDisabled()
  expect(barControls()).toEqual(['Reject', 'Trust', 'Allow once'])
}

/** Open the Trust menu and return its scope rows (the duration choices are
 *  `menuitemradio`, so they are not among them). */
function openTrust(): HTMLElement[] {
  fireEvent.click(screen.getByRole('button', { name: 'Trust' }))
  return screen.getAllByRole('menuitem')
}

/** Choose a lifetime in the open Trust menu by the start of its label. */
function chooseDuration(label: '1 hour' | 'Until midnight' | 'This session') {
  fireEvent.click(screen.getAllByRole('menuitemradio').find(b => b.textContent?.startsWith(label))!)
}

/** Click the open Trust menu's row with exactly this text. */
function clickTrustRow(text: string) {
  fireEvent.click(screen.getAllByRole('menuitem').find(b => b.textContent === text)!)
}

/** Grant all tools: the widest row takes a second click. */
function trustAllTools() {
  openTrust()
  clickTrustRow('All tools — for this session')
  clickTrustRow('Click again to confirm — every tool runs without asking')
}

function stateWithApproval(meta: Record<string, unknown> = {}): Partial<RootState> {
  return {
    chat: {
      activeSlot: 'slot-1',
      messages: [
        { role: 'user', content: 'list files' },
        {
          role: 'permission',
          content: 'Running: ls /tmp',
          meta: {
            approval_id: 'ap-123',
            // The runner's row identity: with the slot, it names the request.
            mid: 'mid-123',
            request_id: 'req-123',
            tool_input: '{"command":"ls /tmp"}',
            is_read_only: '1',
            tool_title: 'Running: ls /tmp',
            is_shell: '1',
            full_command: 'ls /tmp',
            base_command: 'ls',
            trust_command_grantable: '1',
            trust_base_grantable: '1',
            trust_grantable: '1',
            tool_call_id: 'tc-1',
          },
          ...meta,
        },
      ],
      toolLog: [],
      slotStatusDetail: {},
    } as unknown as RootState['chat'],
    dashboard: {
      slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true, waiting_for_input: false, last_activity_ts: undefined }],
      approvalMode: 'normal',
      connected: true,
      channelTrusted: false,
      refreshTrigger: 0,
      unreadSlots: [],
      updateProgress: null,
    } as unknown as RootState['dashboard'],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue(null as never)
  vi.mocked(api.sharedCrewMode).mockResolvedValue(null as never)
  vi.mocked(api.sharedCrewPeople).mockResolvedValue(null as never)
})

describe('ChatInput approval flow', () => {
  it('shows approval bar when pending approval exists', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    await waitFor(() => expect(screen.getByText(/Waiting for approval/)).toBeInTheDocument())
  })

  it('shows Allow once button', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText('Allow once')).toBeInTheDocument()
  })

  it('shows Trust dropdown button', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByRole('button', { name: 'Trust' })).toBeEnabled()
    // A card that can take Trust needs no line explaining its absence.
  })

  it('offers both rejection tiers behind ONE Reject trigger', () => {
    // The row is already at the count AUTOSDE's max-two-buttons-per-row exempts
    // as pre-existing, so a second rejection BUTTON would breach the cap.
    // Both tiers therefore live in the dropdown, and the row does not grow.
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.queryByRole('button', { name: 'Reject once' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reject all' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    const items = screen.getAllByRole('menuitem').map(b => b.textContent || '')
    expect(items.some(x => x.startsWith('Reject once'))).toBe(true)
    expect(items.some(x => x.startsWith('Reject all'))).toBe(true)
  })

  it('a coordinator card is decided by its own target and offers no slot-scoped Trust', async () => {
    // The same id under the coordinator: the card names the record's instance,
    // so the decide reaches that request and never the runner's under the id.
    const state = stateWithApproval()
    const row = (state.chat!.messages as unknown as { meta: Record<string, unknown> }[])[1]
    const target = { origin: 'coordinator', id: 'ap-123', slot: 'slot-1', instance: 'inst-123' }
    row.meta = { ...row.meta, mid: undefined, registry: 'coordinator', approval_target: target }
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    expectTrustHeldDisabled()
    // Withheld as for any unattended source on the base: no extra line.
    expect(screen.queryByTestId('approval-trust-unavailable')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalledWith(target, 'approve', expect.stringMatching(/^r_/))
    })
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('keeps Reject, Trust and Allow once in one order on a sub-agent request (#34971)', () => {
    // A sub-agent's request is a coordinator card, which takes no standing
    // grant. Without the held slot Reject would sit where Trust sits on every
    // other bar, one reflex click from Allow once.
    const { unmount } = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const mainBar = barControls()
    unmount()
    const state = stateWithApproval()
    const row = (state.chat!.messages as unknown as { meta: Record<string, unknown> }[])[1]
    row.meta = { ...row.meta, mid: undefined, registry: 'coordinator', approval_target: { origin: 'coordinator', id: 'ap-123', slot: 'slot-1', instance: 'inst-123' } }
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    expect(barControls()).toEqual(mainBar)
    const trust = screen.getByRole('button', { name: 'Trust' })
    expect(trust).toBeDisabled()
    expect(trust).toHaveAccessibleDescription('Trust is not available for this request. Allow it once or reject it.')
    // The approve chord's probe still lands on Allow once.
    expect(queryPendingApprovalAction()).toBe(screen.getByRole('button', { name: 'Allow once' }))
  })

  it('after a takeover, Allow once settles the replacement and leaves the replaced row stale', async () => {
    // Request A's row was retired as stale when B took its id over; the bar
    // shows B. The decision names B, so A's row must not read as approved.
    const state = stateWithApproval()
    const messages = state.chat!.messages as unknown as { role: string; content: string; meta: Record<string, unknown> }[]
    const target = (instance: string) => ({ origin: 'coordinator', id: 'ap-123', slot: 'slot-1', instance })
    const rowA = messages[1]
    rowA.meta = { ...rowA.meta, mid: undefined, registry: 'coordinator', approval_target: target('inst-a'), resolved: 'stale' }
    messages.push({ ...rowA, meta: { ...rowA.meta, approval_target: target('inst-b'), resolved: undefined, tool_call_id: 'tc-2' } })
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalledWith(target('inst-b'), 'approve', expect.stringMatching(/^r_/))
    })
    await waitFor(() => expect(store.getState().chat.messages[2].meta!.resolved).toBe('approved'))
    expect(store.getState().chat.messages[1].meta!.resolved).toBe('stale')
  })

  it('a card that names no request sends nothing and says it is no longer pending', async () => {
    const state = stateWithApproval()
    const row = (state.chat!.messages as unknown as { meta: Record<string, unknown> }[])[1]
    row.meta = { ...row.meta, mid: undefined }
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
    })
    expect(screen.queryByTestId('approval-decision-error')).toBeNull()
    expect(api.decideApproval).not.toHaveBeenCalled()
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('Allow once calls decideApproval with approve', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalledWith(NATIVE, 'approve', expect.stringMatching(/^r_/))
    })
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('Allow once on a card row decides on the slot route with that row', async () => {
    // Request ids recur across sessions: the decide must not reach another
    // session's request that holds the same id.
    const state = stateWithApproval()
    const card = (state.chat!.messages as { meta?: Record<string, unknown> }[])[1]
    card.meta = { ...card.meta, mid: 'mid-7' }
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalledWith({ ...NATIVE, mid: 'mid-7' }, 'approve', expect.stringMatching(/^r_/))
    })
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('a reason typed in the reject menu travels with the rejection only where the server takes one', async () => {
    const state = stateWithApproval()
    const card = (state.chat!.messages as { meta?: Record<string, unknown> }[])[1]
    card.meta = { ...card.meta, mid: 'mid-7', rejection_reason_accepted: '1' }
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    const box = screen.getByRole('textbox', { name: i18nT('components.rejectDropdown.reason_placeholder') })
    fireEvent.change(box, { target: { value: '  edit b.py instead ' } })
    fireEvent.click(screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith('Reject once'))!)
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith('slot-1', 'rejected_once', {
        request_id: 'ap-123', response_id: expect.stringMatching(/^r_/), origin: 'native', request_mid: 'mid-7',
        reason: 'edit b.py instead',
      })
    })
  })

  it('a reason typed for one card row does not reach a replacement row reusing its id', async () => {
    const state = stateWithApproval()
    const card = (state.chat!.messages as { meta?: Record<string, unknown> }[])[1]
    card.meta = { ...card.meta, mid: 'mid-1', rejection_reason_accepted: '1' }
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    const name = i18nT('components.rejectDropdown.reason_placeholder')
    fireEvent.change(screen.getByRole('textbox', { name }), { target: { value: 'for the first row' } })
    // A replacement request reuses the id with a new card row while the menu is open.
    const first = store.getState().chat.messages.find(m => m.meta?.mid === 'mid-1')!
    act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: first.content, meta: { ...first.meta, mid: 'mid-2' } }))
    })
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(await screen.findByRole('textbox', { name })).toHaveValue('')
  })

  it('the reject menu asks for no reason when the server takes none', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(screen.queryByRole('textbox', { name: i18nT('components.rejectDropdown.reason_placeholder') })).not.toBeInTheDocument()
  })

  it('a coordinator card that names no target sends no bare-id decide', async () => {
    const state = stateWithApproval()
    const card = (state.chat!.messages as { meta?: Record<string, unknown> }[])[1]
    card.meta = { ...card.meta, mid: 'mid-7', registry: 'coordinator' }
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    rejectVia('Reject once')
    await waitFor(() => expect(store.getState().chat.messages.find(m => m.meta?.mid === 'mid-7')?.meta?.resolved).toBe('stale'))
    expect(api.resolveApproval).not.toHaveBeenCalled()
    expect(api.decideApproval).not.toHaveBeenCalled()
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it.each([
    ['Reject all', 'reject'],
    ['Reject once', 'reject_once'],
  ])('%s calls decideApproval with %s', async (label, decision) => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    rejectVia(label as 'Reject once' | 'Reject all')
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalledWith(NATIVE, decision, expect.stringMatching(/^r_/))
    })
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('Trust dropdown trust_command calls approveChatSlot with pattern', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    openTrust()
    clickTrustRow('This exact command — for this session')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_command', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123', pattern: 'ls /tmp' }
      )
    })
    expect(api.decideApproval).not.toHaveBeenCalled()
  })

  it('Trust dropdown trust_base calls approveChatSlot with glob pattern', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    openTrust()
    clickTrustRow('Any ls command — for this session')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_base', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123', pattern: 'ls *' }
      )
    })
    expect(api.decideApproval).not.toHaveBeenCalled()
  })

  it('Trust dropdown timed command row sends ttl_seconds and says when it ends', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    openTrust()
    chooseDuration('1 hour')
    clickTrustRow('This exact command — for 1 hour')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_command', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123', pattern: 'ls /tmp', ttl_seconds: 3600 }
      )
    })
    expect(api.resolveApproval).not.toHaveBeenCalled()
    const chip = await screen.findByText('ls /tmp', { selector: '[role="status"] span' })
    // A landed grant wears the Approved pill, never the warning lock of a refusal.
    const notice = chip.closest('[role="status"]')!
    expect(notice.textContent).toMatch(/^ApprovedTrusted ls \/tmp until .+, then it asks again$/)
    expect(within(notice as HTMLElement).getByText('Approved')).toBeInTheDocument()
    expect(notice.querySelector('svg.lucide-lock')).toBeNull()
  })

  it('Trust dropdown timed command row shows a command ending in a closing quote once (#23893)', async () => {
    const cmd = 'echo “hi”'
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.content = `Running: ${cmd}`
    msg.meta = { ...msg.meta, tool_input: JSON.stringify({ command: cmd }), tool_title: `Running: ${cmd}`, full_command: cmd, base_command: 'echo' }
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    openTrust()
    chooseDuration('1 hour')
    clickTrustRow('This exact command — for 1 hour')
    const chip = await screen.findByText(cmd, { selector: '[role="status"] span' })
    expect(chip.closest('[role="status"]')!.textContent).toMatch(/^ApprovedTrusted echo “hi” until .+, then it asks again$/)
  })

  it('Trust dropdown timed family row names the family it trusted', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    openTrust()
    chooseDuration('1 hour')
    clickTrustRow('Any ls command — for 1 hour')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_base', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123', pattern: 'ls *', ttl_seconds: 3600 }
      )
    })
    expect(await screen.findByText(/^Trusted any “ls” command until .+, then it asks again$/)).toBeInTheDocument()
  })

  it('Trust dropdown entire tool calls approveChatSlot with trust action', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    openTrust()
    // The widest grant takes a second click: the first only arms the row,
    // which then says what the next click does. Nothing is sent yet.
    clickTrustRow('All tools — for this session')
    expect(api.approveChatSlot).not.toHaveBeenCalled()
    expect(screen.getAllByRole('menuitem').map(b => b.textContent)).not.toContain('All tools — for this session')
    clickTrustRow('Click again to confirm — every tool runs without asking')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123' }
      )
    })
    expect(api.decideApproval).not.toHaveBeenCalled()
    expect(await screen.findByTestId('approval-receipt')).toHaveTextContent('ApprovedTrusted all tools for the rest of this session')
  })

  it('choosing a lifetime disarms the all-tools row', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    openTrust()
    clickTrustRow('All tools — for this session')
    chooseDuration('Until midnight')
    expect(screen.getAllByRole('menuitem').map(b => b.textContent)).toEqual([
      'This exact command — until midnight',
      'Any ls command — until midnight',
      // The lifetime above covers the two command rows only.
      'Read-only commands — for this session',
      'All tools — for this session',
    ])
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('the lone all-tools control also takes a second click', async () => {
    // One tier means no menu, so the control IS the grant. It arms first.
    const state = stateWithApproval()
    const meta = state.chat!.messages[1].meta!
    delete meta.trust_command_grantable
    delete meta.trust_base_grantable
    delete meta.full_command
    delete meta.base_command
    meta.is_read_only = ''
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    fireEvent.click(screen.getByRole('button', { name: 'Trust all tools for this session' }))
    expect(api.approveChatSlot).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Click again to confirm — every tool runs without asking' }))
    await waitFor(() => expect(api.approveChatSlot).toHaveBeenCalledWith('slot-1', 'trust', expect.objectContaining({ request_id: 'ap-123' })))
  })

  it.each([
    ['This exact command — for this session', 'ApprovedTrusted ls /tmp for the rest of this session'],
    ['Any ls command — for this session', 'ApprovedTrusted any “ls” command for the rest of this session'],
    ['Read-only commands — for this session', 'ApprovedTrusted read-only commands for the rest of this session'],
  ])('after %s, one line says what was granted and that it ends with the session', async (row, line) => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    openTrust()
    clickTrustRow(row)
    expect(await screen.findByTestId('approval-receipt')).toHaveTextContent(line)
  })

  it('Allow once and both rejections each leave a receipt under their own verdict', async () => {
    const first = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))
    expect(await screen.findByTestId('approval-receipt')).toHaveTextContent('ApprovedAllowed once — Kiro Crew asks again next time')
    first.unmount()
    const second = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    rejectVia('Reject once')
    expect(await screen.findByTestId('approval-receipt')).toHaveTextContent('RejectedRejected once — Kiro Crew keeps asking about the rest')
    second.unmount()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    rejectVia('Reject all')
    expect(await screen.findByTestId('approval-receipt')).toHaveTextContent('RejectedRejected all — Kiro Crew refuses the rest without asking')
  })

  it('a one-shot receipt gives way to the next card, a standing grant\'s stays', async () => {
    const next = (store: ReturnType<typeof createTestStore>) => act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: 'Running: ls /var', meta: { approval_id: 'ap-456', mid: 'mid-456', tool_input: '{"command":"ls /var"}', is_shell: '1', full_command: 'ls /var', base_command: 'ls', trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1', tool_call_id: 'tc-2' } }))
    })
    const oneShot = createTestStore(stateWithApproval())
    const first = renderWithProviders(<ChatInput {...defaultProps} />, { store: oneShot })
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))
    await screen.findByTestId('approval-receipt')
    next(oneShot)
    await waitFor(() => expect(screen.queryByTestId('approval-receipt')).not.toBeInTheDocument())
    first.unmount()
    const standing = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store: standing })
    openTrust()
    clickTrustRow('Any ls command — for this session')
    await screen.findByTestId('approval-receipt')
    next(standing)
    await waitFor(() => expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /var'))
    expect(screen.getByTestId('approval-receipt')).toHaveTextContent('Trusted any “ls” command')
  })

  it('a standing grant\'s receipt does not follow the composer into another session', async () => {
    // One composer serves whichever session is active: "for the rest of this
    // session" over a different session's composer would name the wrong one.
    const state = stateWithApproval()
    // The second session's own (empty) transcript and activity, which a
    // non-active slot is read from.
    Object.assign(state.chat!, { slotMessages: { 'slot-2': [] }, slotActivity: {} })
    const store = createTestStore(state)
    const { rerender } = renderWithProviders(<SlotProvider slotId="slot-1"><ChatInput {...defaultProps} /></SlotProvider>, { store })
    trustAllTools()
    expect(await screen.findByTestId('approval-receipt')).toHaveTextContent('Trusted all tools')
    rerender(<SlotProvider slotId="slot-2"><ChatInput {...defaultProps} /></SlotProvider>)
    await waitFor(() => expect(screen.queryByTestId('approval-receipt')).not.toBeInTheDocument())
  })

  it('heads the card with the request\'s own description, and falls back to the label without one', () => {
    // The permission row carries the agent's one-line purpose only sometimes.
    const state = stateWithApproval()
    state.chat!.messages[1].meta!.tool_purpose = 'List the temp directory'
    const first = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    expect(screen.getByTestId('approval-title')).toHaveTextContent('List the temp directory')
    // Prose in the body face; the command is the only thing set in mono.
    expect(screen.getByTestId('approval-title').className).toContain('font-body')
    expect(screen.getByTestId('approval-title').className).not.toContain('font-mono')
    first.unmount()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const title = screen.getByTestId('approval-title').textContent ?? ''
    expect(title).not.toBe('')
    expect(title).not.toContain('List the temp directory')
  })

  it('names the kind of request, and marks a read-only one', () => {
    const first = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    expect(screen.getByTestId('approval-kind')).toHaveTextContent('Shell command')
    expect(screen.getByTestId('approval-read-only')).toHaveTextContent('Read-only')
    first.unmount()
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.content = 'post_message'
    msg.meta = { ...msg.meta, is_shell: '', is_read_only: '', tool_name: 'post_message', mcp_server: 'slack-mcp', tool_input: '{"channel":"#team"}', full_command: undefined, base_command: undefined, trust_command_grantable: undefined, trust_base_grantable: undefined }
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    expect(screen.getByTestId('approval-kind')).toHaveTextContent('MCP tool · slack-mcp')
    expect(screen.queryByTestId('approval-read-only')).not.toBeInTheDocument()
    // No grantable command, so no command box: the tool's arguments show instead.
    expect(screen.queryByTestId('approval-command')).not.toBeInTheDocument()
    expect(screen.getByTestId('approval-input')).toHaveTextContent('#team')
  })

  it('opens with a status pill where the lock was', () => {
    const { container } = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    expect(screen.getByText('Waiting for approval')).toBeInTheDocument()
    expect(container.querySelector('svg.lucide-lock')).toBeNull()
  })

  it('Allow once is the primary control and the chord\'s landing spot, and grants nothing standing', async () => {
    // A trust grant stays two deliberate clicks away inside its own menu: the
    // control Enter lands on after the focus-approval chord only allows once.
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const allow = screen.getByRole('button', { name: 'Allow once' })
    expect(queryPendingApprovalAction()).toBe(allow)
    expect(allow).toHaveAttribute('data-approval-primary')
    expect(barControls().at(-1)).toBe('Allow once')
    fireEvent.click(allow)
    await waitFor(() => expect(api.decideApproval).toHaveBeenCalledWith(NATIVE, 'approve', expect.stringMatching(/^r_/)))
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('Trust reads calls approveChatSlot for read-only commands', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    // Trust-reads is a tier inside the one Trust dropdown, not a fourth button
    // on the row: `max-two-buttons-per-row` grandfathers three controls there
    // and forbids a fourth.
    openTrust()
    clickTrustRow('Read-only commands — for this session')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_reads', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123' }
      )
    })
  })

  it('keeps the approval row at three controls for a read-only command', () => {
    // The row that would have grown to four: Reject, Trust reads, Trust,
    // Allow once. Every standing grant lives in the dropdown instead.
    const store = createTestStore(stateWithApproval())
    const { container } = renderWithProviders(<ChatInput {...defaultProps} />, { store })
    const row = screen.getByRole('button', { name: 'Allow once' }).parentElement!
    expect(row.querySelectorAll('button')).toHaveLength(3)
    expect(container).toBeTruthy()
  })

  it('does not show approval bar without pending approval', () => {
    const store = createTestStore({
      chat: { activeSlot: 'slot-1', messages: [{ role: 'user', content: 'hi' }], toolLog: [], slotStatusDetail: {} } as unknown as RootState['chat'],
      dashboard: { slots: [{ key: 'slot-1', messages: 1, running: true, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }], approvalMode: 'normal', connected: true, channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null } as unknown as RootState['dashboard'],
    })
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.queryByText(/Waiting for approval/)).not.toBeInTheDocument()
  })

  it('shows Trust reads only for read-only commands', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(openTrust().map(b => b.textContent)).toContain('Read-only commands — for this session')
  })

  it('hides Trust reads for non-read-only commands', () => {
    const state = stateWithApproval()
    state.chat!.messages[1].meta!.is_read_only = ''
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(openTrust().map(b => b.textContent)).not.toContain('Read-only commands — for this session')
  })

  // #5486: with no slot to grant on, the Trust affordances are WITHHELD rather
  // than quietly resolved through the one-shot endpoint. `api.approveChatSlot`
  // is slot-scoped, so a Trust click used to fall through to
  // `api.resolveApproval` — which has no trust verb — running the tool once
  // while the composer reported a standing grant. Same fail-closed rule as
  // #5400 on the spawn card and #5434 on the collapsed tool row: offer only
  // trust verbs the resolve path honors. The mapping that performed the
  // downgrade is pinned separately in ChatInput.trustOneShot.test.tsx.
  //
  // `activeSlot: null` reaches this through `useSlotId`'s global fallback; a
  // `<SlotProvider slotId={null}>` cell (an intentionally empty pane) reaches
  // the same predicate through the provider branch.
  it('withholds the Trust dropdown when there is no slot to grant on (#5486)', () => {
    const state = stateWithApproval()
    state.chat!.activeSlot = null
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expectTrustHeldDisabled()
    // Withholding the trust tier must not take the approval bar with it: the
    // decisions the one-shot endpoint CAN honor are still offered.
    expect(screen.getByText('Allow once')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
  })

  it('withholds Trust reads when there is no slot to grant on (#5486)', () => {
    const state = stateWithApproval()
    state.chat!.activeSlot = null
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    // is_read_only is '1' in this fixture, so the button renders whenever the
    // gate allows it — its absence here is the gate, not a missing precondition.
    expect(screen.queryByText('Trust read-only commands')).not.toBeInTheDocument()
  })

  it('handles API error gracefully without crashing', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new Error('network'))
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    // Should not throw — error is caught internally
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalled()
    })
  })

  it('handles approveChatSlot error gracefully', async () => {
    vi.mocked(api.approveChatSlot).mockRejectedValueOnce(new Error('network'))
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    trustAllTools()
    // Should not throw
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalled()
    })
  })

  it('shows the whole command on the card, once, from the first frame', () => {
    // No ghost and no settle guard between the request and its command: the
    // card states the command itself, whether or not the transcript's row for
    // the call is in view.
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /tmp')
    // A shell call's input is its command, so it is not printed a second time.
    expect(screen.queryByTestId('approval-input')).not.toBeInTheDocument()
  })

  it('still shows what else a shell call carries beside its command', () => {
    // A working directory changes what the command does, so it stays on the
    // card; the command itself is not repeated there.
    const state = stateWithApproval()
    state.chat!.messages[1].meta!.tool_input = JSON.stringify({ command: 'ls /tmp', working_dir: '/srv/app' })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    const extra = screen.getByTestId('approval-input')
    expect(extra).toHaveTextContent('working_dir')
    expect(extra).toHaveTextContent('/srv/app')
    expect(extra).not.toHaveTextContent('ls /tmp')
  })

  it('keeps the exact command on the card while the Trust menu names no command', () => {
    // The exact-command grant is a string match, and its row now reads "This
    // exact command". The string consented to is therefore read off the card:
    // whole, whitespace intact, still mounted with the menu open, and the row
    // is described by it for a screen-reader user.
    const cmd = 'grep "a  b" /tmp/' + 'x'.repeat(300) + '/end'
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.meta = { ...msg.meta, tool_input: JSON.stringify({ command: cmd }), full_command: cmd, base_command: 'grep' }
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    const rows = openTrust()
    const exact = rows.find(b => b.textContent === 'This exact command — for this session')!
    const shown = screen.getByTestId('approval-command')
    expect(shown.textContent).toBe(cmd)
    expect(shown.className).toContain('whitespace-pre-wrap')
    expect(shown.className).not.toContain('truncate')
    expect(exact).toHaveAttribute('aria-describedby', shown.id)
    // No row repeats, or shortens, the command.
    expect(rows.some(b => b.textContent?.includes('/tmp/'))).toBe(false)
  })

  it('uses approvalBaseCommand for TrustDropdown base option', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(openTrust().some(b => b.textContent === 'Any ls command — for this session')).toBe(true)
  })

  it('chat surface offers every trust tier for a read-only shell command (regression guard)', () => {
    // The channels surface deliberately drops the command-scoped tiers
    // (hasCommand={false}); the chat surface must keep every tier — a future
    // change must not silently strip trust_command / trust_base here (#4421).
    // One row per scope, because this fixture is also read-only, under the
    // three lifetimes the two command-scoped rows take.
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(openTrust().map(b => b.textContent)).toEqual([
      'This exact command — for this session',   // trust_command
      'Any ls command — for this session',       // trust_base
      'Read-only commands — for this session',   // trust_reads
      'All tools — for this session',            // trust
    ])
    expect(screen.getAllByRole('menuitemradio').map(b => b.getAttribute('aria-checked'))).toEqual(['false', 'false', 'true'])
  })

  it('uses the server shell flag instead of the display title', () => {
    const state = stateWithApproval()
    state.chat!.messages[1].meta!.tool_title = 'harmless display title'
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    // Should show base command option (only for shell)
    expect(openTrust().some(b => /^Any .+ command/.test(b.textContent ?? ''))).toBe(true)
  })

  it('non-shell tool hides base command option', () => {
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.meta!.tool_title = 'TaskeiGetTask'
    msg.meta!.is_shell = ''
    msg.meta!.full_command = 'TaskeiGetTask'
    msg.meta!.base_command = 'TaskeiGetTask'
    msg.content = 'TaskeiGetTask'
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    const rows = openTrust()
    // Match the base tier's whole shape, not the bare word "commands": the
    // read-only tier also says "commands", so a substring test would pass
    // whatever the base tier does.
    expect(rows.some(b => /^Any .+ command/.test(b.textContent || ''))).toBe(false)
    // Any tool but a shell command is trusted as an exact CALL.
    expect(rows.some(b => b.textContent === 'This exact call — for this session')).toBe(true)
  })

  it('keeps the session tier when the server cannot prove a command scope', () => {
    // A redacted or uncanonicalizable command withholds the tiers that NAME
    // that command. The session grant names none, so it stays: the whole menu
    // used to vanish here and a plain `cd` card offered allow-once only.
    const state = stateWithApproval()
    const meta = state.chat!.messages[1].meta!
    delete meta.trust_command_grantable
    delete meta.trust_base_grantable
    delete meta.full_command
    delete meta.base_command
    // The reported card was a `cd`, which is not read-only; a read-only one
    // would also carry the reads tier and blur what this test pins.
    meta.is_read_only = ''
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    // One tier means no menu: the control carries the tier's own label, so the
    // scope is on the thing the user clicks.
    expect(screen.queryByRole('button', { name: 'Trust' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Trust all tools for this session' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem')).not.toBeInTheDocument()
  })

  it('offers only the reads tier when a read-only card carries no grant proof', () => {
    // trust_reads needs no server-side scope proof, so it survives. The
    // session tier does need one and is withheld: offering it would name a
    // decision the endpoint refuses.
    const state = stateWithApproval()
    const meta = state.chat!.messages[1].meta!
    delete meta.trust_command_grantable
    delete meta.trust_base_grantable
    delete meta.trust_grantable
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.queryByRole('button', { name: 'Trust' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Trust read-only commands' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem')).not.toBeInTheDocument()
  })

  it('offers no Trust grant when the card carries no grant proof at all', () => {
    const state = stateWithApproval()
    const meta = state.chat!.messages[1].meta!
    delete meta.trust_command_grantable
    delete meta.trust_base_grantable
    delete meta.trust_grantable
    meta.is_read_only = ''
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expectTrustHeldDisabled()
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
  })
})

/**
 * Orphaned approval cards: the backend no longer holds a future for the id
 * (turn stopped / timed out / process replaced), so it answers 404. The card
 * must clear instead of lingering on screen with every button dead.
 */
describe('ChatInput orphaned approval (404)', () => {
  const notFound = () => new ApiError(404, 'no pending approval')

  it('clears the approval bar when Trust 404s', async () => {
    vi.mocked(api.approveChatSlot).mockRejectedValueOnce(notFound())
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    trustAllTools()
    await waitFor(() => {
      expect(screen.queryByText('Allow once')).not.toBeInTheDocument()
    })
    expect(store.getState().chat.messages[1].meta!.resolved).toBe('stale')
  })

  it('a lost race adopts the winner and names who answered in a shared Crew', async () => {
    // The Shared Crew preview on and a teammate joined: names show only then.
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ attribution: 'available', shared_crew_preview: true } as never)
    vi.mocked(api.sharedCrewMode).mockResolvedValue({ schema_version: 1, mode: 'network', crew_id: 'c1' } as never)
    vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: { id: 'p_b', kind: 'human', display: { name: 'Bob', login: '', captured_at: null } }, sources: ['observed'], is_viewer: false }] } as never)
    const winner = {
      decision_id: 'approval-state:ap-123', kind: 'approval', session_key: 's1', state: 'resolved', revision: 2,
      initiator: { id: 'p_a', kind: 'human', display: { name: 'Alice', login: '', captured_at: null } },
      responder: { id: 'p_b', kind: 'human', display: { name: 'Bob', login: '', captured_at: null } },
      outcome: 'rejected', resolved_at: 10,
    }
    vi.mocked(api.decideApproval).mockRejectedValueOnce(
      new ApiError(409, 'already answered', JSON.stringify({ code: 'already_resolved', decision: winner })),
    )
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    // The approval bar reads capabilities, then the Crew mode, before it can
    // name anyone: wait for the mode read before clicking.
    await waitFor(() => expect(api.sharedCrewMode).toHaveBeenCalled(), { timeout: 5000 })
    await act(async () => {})
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('Bob rejected')
    })
    expect(store.getState().chat.messages[1].meta!.resolved).toBe('rejected')
  })

  it('a lost race in a personal Crew says it was settled, naming no one', async () => {
    const winner = {
      decision_id: 'approval-state:ap-123', kind: 'approval', session_key: 's1', state: 'resolved', revision: 2,
      initiator: { id: 'p_l', kind: 'human', display: { name: 'local-app', login: '', captured_at: null } },
      responder: { id: 'p_l', kind: 'human', display: { name: 'local-app', login: '', captured_at: null } },
      outcome: 'rejected', resolved_at: 10,
    }
    vi.mocked(api.decideApproval).mockRejectedValueOnce(
      new ApiError(409, 'already answered', JSON.stringify({ code: 'already_resolved', decision: winner })),
    )
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('Already rejected elsewhere')
    })
    expect(screen.queryByText(/local-app/)).toBeNull()
    expect(store.getState().chat.messages[1].meta!.resolved).toBe('rejected')
  })

  it('a late lost-race reply settles only the clicked row, not a newer request with the same id', async () => {
    const winner = {
      decision_id: 'approval-state:ap-123', kind: 'approval', session_key: 's1', state: 'resolved', revision: 2,
      initiator: { id: 'p_a', kind: 'human', display: { name: 'Alice', login: '', captured_at: null } },
      responder: { id: 'p_b', kind: 'human', display: { name: 'Bob', login: '', captured_at: null } },
      outcome: 'approved', resolved_at: 10,
    }
    let reject: (e: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockImplementationOnce(() => new Promise((_, r) => { reject = r }))
    const base = stateWithApproval()
    const first = base.chat!.messages[1]
    const store = createTestStore({ ...base, chat: { ...base.chat!, messages: [base.chat!.messages[0], { ...first, meta: { ...first.meta, mid: 'm-old' } }] } })
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    // A replacement process asks again under the same approval id.
    store.dispatch({ type: 'chat/appendMessage', payload: { role: 'permission', content: 'Running: ls /tmp', meta: { ...first.meta, mid: 'm-new' } } })
    expect(store.getState().chat.messages.filter(m => m.meta?.approval_id === 'ap-123')).toHaveLength(2)
    reject(new ApiError(409, 'already answered', JSON.stringify({ code: 'already_resolved', decision: winner })))
    await waitFor(() => expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-old')?.meta?.resolved).toBe('approved'))
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm-new')?.meta?.resolved).toBeUndefined()
  })

  it('clears the approval bar when Allow once 404s', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(notFound())
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.queryByText('Allow once')).not.toBeInTheDocument()
    })
  })

  it('explains why the click did nothing instead of failing silently', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(notFound())
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    rejectVia('Reject all')
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/expired/i)
    })
  })

  it('a 404 on a row-scoped Allow once retires only the clicked row, not a newer one reusing its id', async () => {
    let rejectCall: (e: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockImplementationOnce(() => new Promise((_res, rej) => { rejectCall = rej }))
    const state = stateWithApproval()
    const card = (state.chat!.messages as { meta?: Record<string, unknown> }[])[1]
    card.meta = { ...card.meta, mid: 'mid-1' }
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => expect(api.decideApproval).toHaveBeenCalledTimes(1))
    // A replacement request reuses the id with a new card row before the reply.
    const first = store.getState().chat.messages.find(m => m.meta?.mid === 'mid-1')!
    store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: first.content, meta: { ...first.meta, mid: 'mid-2' } }))
    rejectCall(notFound())
    await waitFor(() => {
      const rows = store.getState().chat.messages.filter(m => m.meta?.approval_id === 'ap-123')
      expect(rows.find(m => m.meta?.mid === 'mid-1')?.meta?.resolved).toBe('stale')
    })
    const replacement = store.getState().chat.messages.find(m => m.meta?.mid === 'mid-2')
    expect(replacement).toBeTruthy()
    expect(replacement?.meta?.resolved).toBeUndefined()
  })

  it('keeps the bar up on a non-404 failure so the user can retry', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(503, 'busy'))
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      // A rejected decision submit is an error surface (ErrorNotice, role=alert),
      // distinct from the role=status copy an EXPIRED approval gets above.
      expect(screen.getByTestId('approval-decision-error')).toBeInTheDocument()
    })
    expect(screen.getByTestId('approval-decision-error')).toHaveAttribute('role', 'alert')
    // A transient server error is not evidence the approval is gone — the
    // buttons must remain live rather than dismissing a still-valid request.
    expect(screen.getByText('Allow once')).toBeInTheDocument()
    expect(store.getState().chat.messages[1].meta!.resolved).toBeUndefined()
  })
})

function stateWithPendingSpawn(count = 1): Partial<RootState> {
  const subagents: Record<string, unknown> = {}
  for (let i = 1; i <= count; i++) {
    subagents[`a${i}`] = {
      id: `a${i}`, task: `task ${i}`, agent: '', status: 'pending',
      streaming: '', lastTool: '', startedAt: Date.now(), elapsed: 0,
      approval_id: `spawn:a${i}`,
      approval_target: spawnTarget(`a${i}`),
    }
  }
  return {
    chat: {
      activeSlot: 'slot-1',
      messages: [{ role: 'user', content: 'go' }],
      toolLog: [],
      subagents,
      slotActivity: {},
      slotStatusDetail: {},
    } as unknown as RootState['chat'],
    dashboard: {
      slots: [{ key: 'slot-1', messages: 1, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
      approvalMode: 'normal', connected: true, channelTrusted: false,
      refreshTrigger: 0, unreadSlots: [], updateProgress: null,
    } as unknown as RootState['dashboard'],
  }
}

describe('ChatInput sub-agent spawn-approval banner', () => {
  it('surfaces a top-level banner with inline Approve/Reject when a sub-agent awaits approval', () => {
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText(/1 sub-agent is awaiting your approval to run/)).toBeInTheDocument()
    expect(screen.getByText('Review in panel')).toBeInTheDocument()
    // Inline controls let the user resolve without opening the side panel.
    expect(screen.getByRole('button', { name: /^Approve$/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Reject$/ })).toBeInTheDocument()
    // A single pending spawn stays a compact one-liner — no per-agent rows.
    expect(screen.queryByRole('button', { name: /^Approve sub-agent:/ })).not.toBeInTheDocument()
  })

  it('pluralizes the count and uses Approve all / Reject all for multiple pending spawns', () => {
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText(/3 sub-agents are awaiting your approval to run/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Approve all/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Reject all/ })).toBeInTheDocument()
  })

  it('renders a per-agent Approve/Reject row for each pending spawn when multiple', () => {
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getAllByRole('button', { name: /^Approve sub-agent:/ })).toHaveLength(3)
    expect(screen.getAllByRole('button', { name: /^Reject sub-agent:/ })).toHaveLength(3)
    // Each row is labelled with its own task.
    expect(screen.getByRole('button', { name: 'Approve sub-agent: task 2' })).toBeInTheDocument()
  })

  it('clicking a per-agent Approve resolves only that sub-agent', () => {
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Approve sub-agent: task 2' }))
    expect(api.decideApproval).toHaveBeenCalledWith(spawnTarget('a2'), 'approve')
    expect(api.decideApproval).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.subagents.a2.approving).toBe(true)
    expect(store.getState().chat.subagents.a1.approving).toBeFalsy()
  })

  it('clicking a per-agent Reject rejects only that sub-agent', () => {
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Reject sub-agent: task 3' }))
    expect(api.decideApproval).toHaveBeenCalledWith(spawnTarget('a3'), 'reject')
    expect(api.decideApproval).toHaveBeenCalledTimes(1)
  })

  /**
   * A rejected spawn never runs, so it emits no further spawn/chunk/done
   * stream, and the backend's `approval_resolved` frame carries no slot — so
   * the WS handler that would terminate the card never fires. Without an
   * explicit terminal dispatch the card stays pending+approving and the banner
   * sticks on "Resolving…" forever.
   */
  it('terminates the card after a successful rejection so the banner clears', async () => {
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Reject$/ }))
    await waitFor(() => {
      expect(store.getState().chat.subagents.a1.status).toBe('error')
    })
    // The optimistic terminate fills the same verbatim error slot the WS retire
    // path does, so it must carry the catalog sentence, not the raw token.
    const rejectedCopy = i18nT('hooks.useWebSocket.approval_rejected')
    expect(rejectedCopy).toBe('The approval was rejected, so the request was denied.')
    expect(store.getState().chat.subagents.a1.error).toBe(rejectedCopy)
    expect(store.getState().chat.subagents.a1.error).not.toBe('rejected')
    // No longer pending -> the banner unmounts.
    await waitFor(() => {
      expect(screen.queryByText(/awaiting your approval to run/)).not.toBeInTheDocument()
    })
  })

  it('leaves an approved spawn pending for its own spawn/done stream to resolve', async () => {
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalledWith(spawnTarget('a1'), 'approve')
    })
    // Approval must NOT synthesize a terminal state — the real run reports it.
    expect(store.getState().chat.subagents.a1.status).toBe('pending')
  })

  it('a late 404 from the losing tab leaves a spawn the winning tab already started running', async () => {
    let refuse: (e: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockReturnValueOnce(new Promise((_res, rej) => { refuse = rej }))
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    // The other tab won: its spawn frame marks the card running and the
    // stream starts, before this tab's POST answers.
    act(() => {
      store.dispatch(sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task 1', agent: 'kirocrew' }))
      store.dispatch(sseSubagentTool({ slot: 'slot-1', id: 'a1', tool: 'shell' }))
    })
    const before = store.getState().chat.subagents.a1
    await act(async () => { refuse(new ApiError(404, 'no pending approval')) })
    const after = store.getState().chat.subagents.a1
    expect(after.status).toBe(before.status)
    expect(after.status).not.toBe('error')
    expect(after.error).toBeUndefined()
    expect(after.streaming).toBe(before.streaming)
  })

  it('a 404 for a request the card no longer names leaves the newer pending card alone', async () => {
    let refuse: (e: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockReturnValueOnce(new Promise((_res, rej) => { refuse = rej }))
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    // A new request under the same spawn id replaces the card before the
    // old request's refusal answers.
    act(() => {
      store.dispatch(sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 'task 1', approval_id: 'spawn:a1', approval_target: { ...spawnTarget('a1'), instance: 'inst-a1-new' } }))
    })
    await act(async () => { refuse(new ApiError(404, 'no pending approval')) })
    expect(store.getState().chat.subagents.a1.status).toBe('pending')
    expect(store.getState().chat.subagents.a1.error).toBeUndefined()
  })

  it('per-agent rejection terminates only the rejected sub-agent', async () => {
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Reject sub-agent: task 2' }))
    await waitFor(() => {
      expect(store.getState().chat.subagents.a2.status).toBe('error')
    })
    expect(store.getState().chat.subagents.a1.status).toBe('pending')
    expect(store.getState().chat.subagents.a3.status).toBe('pending')
    // Banner stays up for the two still-pending spawns.
    expect(screen.getByText(/2 sub-agents are awaiting your approval to run/)).toBeInTheDocument()
  })

  it('a per-agent row shows Resolving and hides its buttons while that sub-agent is approving', () => {
    const base = stateWithPendingSpawn(3) as { chat: { subagents: Record<string, { approving?: boolean }> } }
    base.chat.subagents.a2.approving = true
    const store = createTestStore(base as Partial<RootState>)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    // a2's row is resolving; its per-agent buttons are gone, the others remain.
    expect(screen.queryByRole('button', { name: 'Approve sub-agent: task 2' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve sub-agent: task 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve sub-agent: task 3' })).toBeInTheDocument()
    expect(screen.getByText(/Resolving/)).toBeInTheDocument()
    // Header bulk controls stay available (not every spawn is resolving).
    expect(screen.getByRole('button', { name: /Approve all/ })).toBeInTheDocument()
  })

  it('clicking Approve resolves the pending spawn via the approvals API', () => {
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    expect(api.decideApproval).toHaveBeenCalledWith(spawnTarget('a1'), 'approve')
    // Card is marked resolving so the buttons can't be double-submitted.
    expect(store.getState().chat.subagents.a1.approving).toBe(true)
  })

  it('clicking Reject rejects the pending spawn via the approvals API', () => {
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Reject$/ }))
    expect(api.decideApproval).toHaveBeenCalledWith(spawnTarget('a1'), 'reject')
  })

  it('a press on a spawn that names no request sends nothing and withdraws it as gone', async () => {
    // Nothing live can be decided without a target, so the press takes the
    // same gone verdict a refused one does: the banner withdraws the spawn and
    // says why, and the spawn inventory settles whether it started.
    const base = stateWithPendingSpawn(1) as { chat: { subagents: Record<string, { approval_target?: unknown }> } }
    delete base.chat.subagents.a1.approval_target
    const store = createTestStore(base as Partial<RootState>)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    expect(api.decideApproval).not.toHaveBeenCalled()
    expect(store.getState().chat.subagents.a1.approvalGone).toBe('spawn:a1')
    await waitFor(() => expect(screen.queryByText(/awaiting your approval to run/)).not.toBeInTheDocument())
    // Gone is status, not an error (#18203): the composer's status strip.
    const gone = i18nT('components.approvalCard.approval_no_longer_pending')
    expect(screen.queryByTestId('approval-decision-error')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('status').filter(el => el.textContent?.includes(gone))).toHaveLength(1)
  })

  it('a refusal on request A does not hold back a replacement B under the same approval id', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'no pending approval'))
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    await waitFor(() => expect(screen.queryByTestId('spawn-approval-card')).not.toBeInTheDocument())
    // B replaces A: same approval id, a new instance.
    act(() => {
      store.dispatch(sseSubagentPending({
        slot: 'slot-1', id: 'a1', task: 'task 1', approval_id: 'spawn:a1',
        approval_target: { ...spawnTarget('a1'), instance: 'inst-b' },
      }))
    })
    expect(await screen.findByRole('button', { name: /^Approve$/ })).toBeInTheDocument()
  })

  it('Approve all resolves every pending spawn', () => {
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Approve all/ }))
    expect(api.decideApproval).toHaveBeenCalledWith(spawnTarget('a1'), 'approve')
    expect(api.decideApproval).toHaveBeenCalledWith(spawnTarget('a2'), 'approve')
    expect(api.decideApproval).toHaveBeenCalledWith(spawnTarget('a3'), 'approve')
    expect(api.decideApproval).toHaveBeenCalledTimes(3)
  })

  it('clicking Review in panel opens the Subagents panel and resolves nothing', () => {
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Review in panel/ }))
    expect(store.getState().chat.activityTab).toBe('subagents')
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(api.decideApproval).not.toHaveBeenCalled()
  })

  it('shows a Resolving state instead of buttons once all pending spawns are approving', () => {
    const base = stateWithPendingSpawn(1) as { chat: { subagents: Record<string, { approving?: boolean }> } }
    base.chat.subagents.a1.approving = true
    const store = createTestStore(base as Partial<RootState>)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText(/Resolving/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Approve$/ })).not.toBeInTheDocument()
  })

  /** One set of decision buttons on screen at a time: while a tool approval is
   *  also pending, the spawn card keeps its count and the panel link but
   *  withholds its own Approve/Reject and glow, and its line stops pointing at
   *  itself as the thing to approve, so the two panes cannot be taken for one
   *  request (UX review of 21b8e79b and 5932abb3). */
  it('withholds the spawn Approve/Reject and glow while a tool approval is also pending', () => {
    const spawn = stateWithPendingSpawn(2) as { chat: Record<string, unknown> }
    const tool = stateWithApproval() as { chat: { messages: unknown[] }; dashboard: unknown }
    const store = createTestStore({
      chat: { ...spawn.chat, messages: tool.chat.messages },
      dashboard: tool.dashboard,
    } as unknown as Partial<RootState>)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText('2 sub-agents pending — answer the request below first')).toBeInTheDocument()
    expect(screen.queryByText(/awaiting your approval/)).not.toBeInTheDocument()
    expect(screen.getByText('Review in panel')).toBeInTheDocument()
    // The tool bar's own buttons are the only decision on screen.
    expect(screen.getByText('Allow once')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Approve all$/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Approve sub-agent:/ })).not.toBeInTheDocument()
    expect(screen.getByTestId('spawn-approval-card').className).not.toContain('approval-glow')
    expect(screen.getByTestId('composer-dock').className).toContain('approval-glow')
  })

  it('does not render the banner when there are no pending spawns', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.queryByText(/awaiting your approval to run/)).not.toBeInTheDocument()
  })

  /**
   * A 404 means the backend holds no future for this approval: it was decided
   * or expired where this client could not see it (a missed frame across a
   * reconnect, a gateway restart). The activity panel already classifies that
   * through `isTerminalApprovalRefusal` (#11180); this banner resolves the SAME
   * id and must not keep offering a retry that can never succeed, silently.
   */
  it('withdraws the banner and says why when the spawn approval is already gone', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'not found or expired'))
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))

    await waitFor(() => {
      expect(screen.queryByText(/awaiting your approval to run/)).not.toBeInTheDocument()
    })
    // A gone approval is status, not an error (#18203): the status strip, as the
    // tool path's 404. This banner sent it, so it is the one surface that
    // reports it; the panel shows only neutral liveness.
    const gone = i18nT('components.approvalCard.approval_no_longer_pending')
    expect(screen.queryByTestId('approval-decision-error')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('status').filter(el => el.textContent?.includes(gone))).toHaveLength(1)
    expect(api.decideApproval).toHaveBeenCalledTimes(1)
    // The empty authoritative process inventory retires the card after the
    // controls disappear; it does not survive reconnect as pending.
    await waitFor(() => expect(store.getState().chat.subagents.a1.status).toBe('stopped'))
    expect(store.getState().chat.subagents.a1.approving).toBe(false)
  })

  it('reports a failed liveness read after a gone verdict and leaves the card unresolved', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'not found or expired'))
    vi.mocked(api.spawnList).mockRejectedValueOnce(new ApiError(503, 'unavailable'))
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))

    // The rejected reconciliation is observable, as an error notice of its own.
    await waitFor(() => {
      expect(screen.getByTestId('spawn-liveness-error')).toHaveTextContent(
        i18nT('pages.chat.subagentProgressBar.liveness_check_failed'),
      )
    })
    expect(screen.getByTestId('spawn-liveness-error')).toHaveAttribute('role', 'alert')
    // Beside the refusal, not over it: the failed read lands a microtask after
    // the refusal is shown, and one shared notice would leave no surface
    // reporting the refusal this banner sent.
    const gone = i18nT('components.approvalCard.approval_no_longer_pending')
    const goneRows = screen.getAllByRole('status').filter(el => el.textContent?.includes(gone))
    expect(goneRows).toHaveLength(1)
    expect(goneRows[0]).not.toHaveTextContent(
      i18nT('pages.chat.subagentProgressBar.liveness_check_failed'),
    )
    expect(api.spawnList).toHaveBeenCalledTimes(1)
    // A failed read settles nothing: still gone-and-unresolved, never retired,
    // and the stale approval controls do not come back.
    const card = store.getState().chat.subagents.a1
    expect(card.status).toBe('pending')
    expect(card.approvalGone).toBe('spawn:a1')
    expect(screen.queryByRole('button', { name: /^Approve$/ })).not.toBeInTheDocument()
  })

  it('drops the liveness notice once another reader settles the card, keeping the refusal', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'not found or expired'))
    vi.mocked(api.spawnList).mockRejectedValueOnce(new ApiError(503, 'unavailable'))
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    await waitFor(() => expect(screen.getByTestId('spawn-liveness-error')).toBeInTheDocument())

    // The chip's poll (or the card's re-read) answers: the inventory holds no
    // row, so the card retires and "couldn't check" is no longer true.
    await store.dispatch(reconcileGoneSubagent({ slot: 'slot-1', id: 'a1', approval_id: 'spawn:a1' }))
    await waitFor(() => expect(screen.queryByTestId('spawn-liveness-error')).not.toBeInTheDocument())
    expect(store.getState().chat.subagents.a1.status).toBe('stopped')
    const gone = i18nT('components.approvalCard.approval_no_longer_pending')
    expect(screen.getAllByRole('status').filter(el => el.textContent?.includes(gone))).toHaveLength(1)
  })

  it('dismisses the liveness notice on its own, leaving the refusal and the unresolved card', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'not found or expired'))
    vi.mocked(api.spawnList).mockRejectedValueOnce(new ApiError(503, 'unavailable'))
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    const notice = await screen.findByTestId('spawn-liveness-error')

    fireEvent.click(within(notice).getByRole('button'))
    expect(screen.queryByTestId('spawn-liveness-error')).not.toBeInTheDocument()
    const gone = i18nT('components.approvalCard.approval_no_longer_pending')
    expect(screen.getAllByRole('status').filter(el => el.textContent?.includes(gone))).toHaveLength(1)
    // Dismissing the notice settles nothing: the chip's poll still owns that.
    expect(store.getState().chat.subagents.a1.approvalGone).toBe('spawn:a1')
  })

  it('keeps the buttons and reports the failure after a refusal that is not terminal', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(500, 'boom'))
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))

    // The decision was not recorded, so the failure is an error surface
    // (AUTOSDE errors-use-error-notice) carrying the panel's sentence.
    await waitFor(() => {
      expect(screen.getByTestId('approval-decision-error')).toHaveAttribute('role', 'alert')
    })
    expect(screen.getByTestId('approval-decision-error')).toHaveTextContent(
      i18nT('components.approvalCard.decision_not_recorded_error', { error: 'boom' }),
    )
    expect(store.getState().chat.subagents.a1.approving).toBe(false)
    // Not evidence the approval is gone: no gone verdict, and a retry goes out.
    expect(store.getState().chat.subagents.a1.approvalGone).toBeUndefined()
    expect(screen.queryByText(i18nT('components.approvalCard.approval_no_longer_pending'))).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    expect(api.decideApproval).toHaveBeenCalledTimes(2)
    expect(store.getState().chat.subagents.a1.approving).toBe(true)
  })

  it('withdraws only the gone sub-agent from a multi-agent banner', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'not found or expired'))
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Approve sub-agent: task 2' }))

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Approve sub-agent: task 2' })).not.toBeInTheDocument()
    })
    expect(screen.getByText(/2 sub-agents are awaiting your approval to run/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve sub-agent: task 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve sub-agent: task 3' })).toBeInTheDocument()
  })
})

/**
 * Unattended sources (cron / heartbeat / taskrunner) run with no human bound to
 * the conversation the card renders in. Session-scoped Trust is incoherent for
 * them: `api.approveChatSlot` grants on THIS slot, widening its auto-approval
 * surface for a job that is not this session — and doing nothing for the job.
 * So the Trust controls are withheld, and the expiry copy names the source
 * because these approvals deny-fast on a short window.
 */
describe('ChatInput unattended-source approvals', () => {
  const withSource = (source: string, opts: { viaLabel?: boolean } = {}) => {
    const state = stateWithApproval()
    if (opts.viaLabel) {
      // Rehydrated-from-content path: chatSlice's reconstruct carries no
      // `source`, so the `[source]` label prefix is the only signal.
      state.chat!.messages[1].content = `[${source}] Running: ls /tmp`
    } else {
      state.chat!.messages[1].meta!.source = source
    }
    return state
  }

  it.each(['cron', 'heartbeat', 'taskrunner'])('withholds Trust for %s', (source) => {
    const store = createTestStore(withSource(source))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expectTrustHeldDisabled()
    expect(screen.queryByText('Trust read-only commands')).not.toBeInTheDocument()
    // The actionable controls remain — the card is still answerable.
    expect(screen.getByText('Allow once')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
  })

  it('keeps Trust for autonudge, which does run in this session', () => {
    const store = createTestStore(withSource('autonudge'))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByRole('button', { name: 'Trust' })).toBeEnabled()
  })

  it('keeps Trust for an ordinary in-session approval', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByRole('button', { name: 'Trust' })).toBeEnabled()
  })

  it('withholds Trust when the source survives only in the card label', () => {
    const store = createTestStore(withSource('cron', { viaLabel: true }))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expectTrustHeldDisabled()
  })

  it.each([
    ['Allow once', 'approve'],
    ['Reject all', 'reject'],
    ['Reject once', 'reject_once'],
  ])('answers an unattended card via %s without the slot-scoped grant', async (label, decision) => {
    // No control on an unattended card may route through approveChatSlot,
    // which grants on THIS slot. (handleApprovalAction also downgrades a trust
    // decision to a one-shot allow as defence in depth, but the UI withholds
    // those controls entirely, so that branch is not reachable from here.)
    const store = createTestStore(withSource('cron'))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    if (label === 'Allow once') fireEvent.click(screen.getByText(label))
    else rejectVia(label as 'Reject once' | 'Reject all')
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalledWith(NATIVE, decision, expect.stringMatching(/^r_/))
    })
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('names the source when an unattended request already timed out', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'gone'))
    const store = createTestStore(withSource('cron'))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/cron request already timed out/i)
    })
  })

  it('reads the shared refused-press sentence for an ordinary gone approval', async () => {
    vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'gone'))
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
    })
  })
})


/**
 * Regression: a steered user message must NOT remove or deadlock the approval
 * bar. The user must be able to steer the agent AND still answer a pending
 * approval (#1667).
 */
describe('ChatInput approval bar survives a steered user message (#1667)', () => {
  it('approval bar remains visible after a steered message is appended via sseChatMessage', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    // Verify the approval bar is shown initially
    await waitFor(() => expect(screen.getByText(/Waiting for approval/)).toBeInTheDocument())
    expect(screen.getByText('Allow once')).toBeInTheDocument()

    // Dispatch a steered user message (simulates the steer_push WS echo)
    const { sseChatMessage: sseCM } = await import('../store/chatSlice')
    store.dispatch(sseCM({ slot: 'slot-1', role: 'user', content: 'also check /var', meta: { steer: true } }))

    // The approval bar must still be visible and functional
    expect(screen.getByText(/Waiting for approval/)).toBeInTheDocument()
    expect(screen.getByText('Allow once')).toBeInTheDocument()
    expect(screen.getByText('Trust')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
  })

  it('approval buttons still resolve the same approval_id after a steer', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    await waitFor(() => expect(screen.getByText('Allow once')).toBeInTheDocument())

    // Dispatch the steer
    const { sseChatMessage: sseCM } = await import('../store/chatSlice')
    store.dispatch(sseCM({ slot: 'slot-1', role: 'user', content: 'try another approach', meta: { steer: true } }))

    // Click Allow once — it must still call with the original approval_id
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(api.decideApproval).toHaveBeenCalledWith(NATIVE, 'approve', expect.stringMatching(/^r_/))
    })
  })

  it('approval bar remains after an optimistic steer bubble via appendSlotMessage', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    await waitFor(() => expect(screen.getByText('Allow once')).toBeInTheDocument())

    // Dispatch the optimistic steer bubble (client-side, before WS echo)
    const { appendSlotMessage: asm } = await import('../store/chatSlice')
    store.dispatch(asm({ slot: 'slot-1', message: { role: 'user', content: 'steer text', cls: 'msg msg-u', meta: { steer: true, optimistic: true } } }))

    // Bar must remain
    await waitFor(() => {
      expect(screen.getByText(/Waiting for approval/)).toBeInTheDocument()
      expect(screen.getByText('Allow once')).toBeInTheDocument()
    })
  })
})
