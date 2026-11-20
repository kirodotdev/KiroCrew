import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { act, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { COMMAND_WRAP } from '../components/chat-input/CuedScrollBox'
import { renderWithProviders, renderHookWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { useToolApproval } from '../components/chat-input/approval'
import { api } from '../api/client'
import { resolveApprovalRow, sseChatMessage } from '../store/chatSlice'
import { SlotProvider } from '../providers/SlotContext'
import { defaultProps, rejectVia, openTrust, rowLabel, chooseDuration, clickTrustRow, trustAllTools, stateWithApproval, reduceMotion, CARD_FADE_SETTLED } from './approvalCardFixtures'

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

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue(null as never)
  vi.mocked(api.sharedCrewMode).mockResolvedValue(null as never)
  vi.mocked(api.sharedCrewPeople).mockResolvedValue(null as never)
})

// The Trust menu's rows and what each sends, and the receipt a decision leaves.
// Split from ChatInput.approval.test.tsx, which shares its fixtures.
describe('ChatInput approval Trust menu and receipts', () => {
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
    const receipt = await screen.findByTestId('approval-receipt')
    expect(receipt).toHaveTextContent(/Trusted any ls command until .+, then it asks again$/)
    // The family as the menu row prints it: in mono, not in quotes.
    expect(receipt.querySelector('.font-mono')).toHaveTextContent(/^ls$/)
  })

  it('Trust dropdown entire tool calls approveChatSlot with trust action', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    openTrust()
    // The widest grant takes a second click: the first only arms the row,
    // which then says what the next click does. Nothing is sent yet.
    clickTrustRow('All tools')
    expect(api.approveChatSlot).not.toHaveBeenCalled()
    expect(screen.getAllByRole('menuitem').map(b => b.textContent)).not.toContain('All tools')
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
    // A rule under the rows the duration covers, and a heading over the session-long ones.
    const menu = screen.getByRole('menu')
    const order = Array.from(menu.querySelectorAll('[role="menuitem"], [role="separator"], [data-testid="trust-session-rows"]')).map(el => el.getAttribute('role') === 'separator' ? '---' : el.getAttribute('role') === 'menuitem' ? rowLabel(el) : el.textContent)
    expect(order).toEqual(['This exact command — for this session', 'Any ls command — for this session', '---', 'Until this chat ends', 'Read-only commands', 'All tools'])
    clickTrustRow('All tools')
    chooseDuration('Until midnight')
    expect(screen.getAllByRole('menuitem').map(rowLabel)).toEqual([
      'This exact command — until midnight',
      'Any ls command — until midnight',
      // The lifetime above covers the two command rows only.
      'Read-only commands',
      'All tools',
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
    ['Any ls command — for this session', 'ApprovedTrusted any ls command for the rest of this session'],
    ['Read-only commands', 'ApprovedTrusted read-only commands for the rest of this session'],
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
    const receiptPill = await waitFor(() => screen.getByTestId('approval-receipt').querySelector('[data-layout-id]')!)
    next(standing)
    await waitFor(() => expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /var'))
    expect(screen.getByTestId('approval-receipt')).toHaveTextContent('Trusted any ls command')
    // The receipt's pill and the next card's pill answer different requests, so
    // they never share a layout stack: Framer Motion would fly the new pill out
    // of the receipt and hide the receipt's verdict.
    const ids = Array.from(document.querySelectorAll('[data-layout-id]')).map(el => el.getAttribute('data-layout-id'))
    expect(ids).toHaveLength(2)
    expect(new Set(ids).size).toBe(2)
    expect(receiptPill.getAttribute('data-layout-id')).toBe('approval-status-pill:slot-1:ap-123:mid-123')
  })

  it('a card pill and the receipt for the same request share one layout id', async () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const cardId = document.querySelector('[data-layout-id]')!.getAttribute('data-layout-id')
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))
    const receipt = await screen.findByTestId('approval-receipt')
    expect(receipt.querySelector('[data-layout-id]')!.getAttribute('data-layout-id')).toBe(cardId)
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
    // Gone in the render that moves the composer: never drawn, even fading, over slot-2.
    expect(screen.queryByTestId('approval-receipt')).not.toBeInTheDocument()
  })

  /** An MCP call whose heading is the agent's own description. */
  function mcpStateWithPurpose() {
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.content = 'post_message'
    msg.meta = { ...msg.meta, is_shell: '', is_read_only: '', tool_name: 'post_message', mcp_server: 'slack-mcp', tool_purpose: 'Say hello to the team', tool_input: '{"channel":"#team"}', full_command: undefined, base_command: undefined, trust_command_grantable: undefined, trust_base_grantable: undefined }
    return state
  }

  it('names the tool itself on the card, not only the agent\'s description of it', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(mcpStateWithPurpose()) })
    expect(screen.getByTestId('approval-title')).toHaveTextContent('Say hello to the team')
    // The gateway's own name for the tool, beside the server it belongs to.
    expect(screen.getByTestId('approval-tool')).toHaveTextContent('post_message')
    expect(screen.getByTestId('approval-tool').className).toContain('font-mono')
  })

  it('heads the card with the tool label when simplified names are off', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ simplifiedToolNames: false }))
    try {
      renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(mcpStateWithPurpose()) })
      expect(screen.getByTestId('approval-title')).not.toHaveTextContent('Say hello to the team')
    } finally {
      localStorage.removeItem('mc-chat-config')
    }
  })

  it('wraps the card\'s command and tool name at spaces, never mid-word', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    expect(screen.getByTestId('approval-command').className).toContain(COMMAND_WRAP)
    expect(screen.getByTestId('approval-command').className).not.toContain('break-all')
    cleanup()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(mcpStateWithPurpose()) })
    expect(screen.getByTestId('approval-tool').className).toContain(COMMAND_WRAP)
  })

  it('with simplified names off, shows the agent\'s description as a second line under the label', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ simplifiedToolNames: false }))
    onTestFinished(() => localStorage.removeItem('mc-chat-config'))
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(mcpStateWithPurpose()) })
    expect(screen.getByTestId('approval-purpose')).toHaveTextContent('Say hello to the team')
  })

  it('shows no second line when the heading already is the description, or there is none', () => {
    const { unmount } = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(mcpStateWithPurpose()) })
    expect(screen.getByTestId('approval-title')).toHaveTextContent('Say hello to the team')
    expect(screen.queryByTestId('approval-purpose')).not.toBeInTheDocument()
    unmount()
    localStorage.setItem('mc-chat-config', JSON.stringify({ simplifiedToolNames: false }))
    onTestFinished(() => localStorage.removeItem('mc-chat-config'))
    const state = mcpStateWithPurpose()
    state.chat!.messages[1].meta!.tool_purpose = ''
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    expect(screen.queryByTestId('approval-purpose')).not.toBeInTheDocument()
  })

  it('Trust dropdown until-midnight command row sends the seconds left in the day', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    onTestFinished(() => { vi.useRealTimers() })
    vi.setSystemTime(new Date(2026, 10, 17, 22, 0, 0))
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    openTrust()
    chooseDuration('Until midnight')
    clickTrustRow('This exact command — until midnight')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_command', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123', pattern: 'ls /tmp', ttl_seconds: 7200 }
      )
    })
    expect(api.decideApproval).not.toHaveBeenCalled()
  })

  it('Trust dropdown always-allow row grants the ref the server stamped on the card', async () => {
    const state = mcpStateWithPurpose()
    Object.assign(state.chat!.messages[1].meta!, { always_allow_tool_grantable: '1', always_allow_tool_ref: '@slack-mcp/post_message' })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    const row = openTrust().find(b => b.textContent?.startsWith('Always allow this tool'))!
    // The row names the ref it grants for good: the card's tool chips are under the menu.
    expect(within(row).getByTestId('trust-always-ref')).toHaveTextContent('@slack-mcp/post_message')
    clickTrustRow('Always allow this tool @slack-mcp/post_message')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'always_allow_tool', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123', pattern: '@slack-mcp/post_message' }
      )
    })
    expect(api.decideApproval).not.toHaveBeenCalled()
  })

  it('Trust dropdown shows a refused always-allow row disabled, with its reason', () => {
    const state = mcpStateWithPurpose()
    Object.assign(state.chat!.messages[1].meta!, { always_allow_tool_refusal: 'asks_every_time' })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    const row = openTrust().find(b => b.textContent?.startsWith('Always allow this tool'))!
    // Radix marks a disabled row aria-disabled; the test mock forwards the prop.
    expect(row.getAttribute('aria-disabled') === 'true' || row.hasAttribute('disabled')).toBe(true)
    expect(row.textContent!.length).toBeGreaterThan('Always allow this tool'.length)
    fireEvent.click(row)
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('keeps the command box under a description heading', () => {
    // The heading is agent-written; the exact command below it is what is consented to.
    const state = stateWithApproval()
    state.chat!.messages[1].meta!.tool_purpose = 'List the temp directory'
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    expect(screen.getByTestId('approval-title')).toHaveTextContent('List the temp directory')
    expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /tmp')
  })

  it('an armed all-tools row does not carry over to another session\'s card', async () => {
    // One composer serves whichever session is active, so the Trust menu's
    // first-click consent must not survive a switch to another session's card.
    const state = stateWithApproval()
    const row = (state.chat!.messages as unknown as { meta: Record<string, unknown> }[])[1]
    const other = { ...row, meta: { ...row.meta, approval_id: 'ap-456', mid: 'mid-456', request_id: 'req-456', tool_call_id: 'tc-2' } }
    Object.assign(state.chat!, { slotMessages: { 'slot-2': [{ role: 'user', content: 'list files' }, other] }, slotActivity: {} })
    ;(state.dashboard!.slots as unknown[]).push({ key: 'slot-2', messages: 2, running: true, pending_approval: true, waiting_for_input: false, last_activity_ts: undefined })
    const store = createTestStore(state)
    const { rerender } = renderWithProviders(<SlotProvider slotId="slot-1"><ChatInput {...defaultProps} /></SlotProvider>, { store })
    openTrust()
    clickTrustRow('All tools')
    expect(screen.getByText('Click again to confirm — every tool runs without asking')).toBeInTheDocument()
    rerender(<SlotProvider slotId="slot-2"><ChatInput {...defaultProps} /></SlotProvider>)
    expect(screen.getByRole('button', { name: 'Trust' })).toBeEnabled()
    expect(screen.queryByText('Click again to confirm — every tool runs without asking')).not.toBeInTheDocument()
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('names an MCP tool grant by the tool the server keys it on', async () => {
    // A tool called with no arguments is grantable by its server and name, so
    // the row says that tool, never this one call.
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.content = 'list_channels'
    msg.meta = { ...msg.meta, is_shell: '', is_read_only: '', tool_name: 'list_channels', mcp_server: 'slack-mcp', tool_input: '', full_command: 'mcp__slack-mcp__list_channels', base_command: undefined, trust_base_grantable: undefined }
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    const texts = openTrust().map(b => b.textContent)
    expect(texts).toContain('mcp__slack-mcp__list_channels — for this session')
    expect(texts.some(t => t?.includes('exact'))).toBe(false)
    clickTrustRow('mcp__slack-mcp__list_channels — for this session')
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_command', { response_id: expect.stringMatching(/^r_/), request_id: 'ap-123', origin: 'native', request_mid: 'mid-123', pattern: 'mcp__slack-mcp__list_channels' }
      )
    })
  })

  it('settles a decision with a calm fade by default', async () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    expect(screen.getByTestId('approval-card-motion')).toHaveAttribute('data-motion', 'settle')
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))
    expect(await screen.findByTestId('approval-receipt')).toHaveAttribute('data-motion', 'settle')
  })

  it('swaps the card for its receipt at once when the reader asked for less motion', async () => {
    reduceMotion()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    expect(screen.getByTestId('approval-card-motion')).toHaveAttribute('data-motion', 'instant')
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))
    expect(await screen.findByTestId('approval-receipt')).toHaveAttribute('data-motion', 'instant')
  })

  it('drops a receipt whose decision lands after the composer moved to another session', async () => {
    // The receipt names "this session"; over another session's composer it would name the wrong one.
    let land: (v: unknown) => void = () => {}
    vi.mocked(api.approveChatSlot).mockImplementationOnce(() => new Promise(r => { land = r }) as never)
    const state = stateWithApproval()
    Object.assign(state.chat!, { slotMessages: { 'slot-2': [] }, slotActivity: {} })
    const store = createTestStore(state)
    const { rerender } = renderWithProviders(<SlotProvider slotId="slot-1"><ChatInput {...defaultProps} /></SlotProvider>, { store })
    openTrust()
    clickTrustRow('This exact command — for this session')
    rerender(<SlotProvider slotId="slot-2"><ChatInput {...defaultProps} /></SlotProvider>)
    await act(async () => { land({}) })
    expect(screen.queryByTestId('approval-receipt')).not.toBeInTheDocument()
  })

  it('a trust verb sent for a coordinator request is a one-shot allow, and its receipt says so', async () => {
    // Defence in depth: the menu is not offered there, so drive the decision directly.
    const state = stateWithApproval()
    const row = (state.chat!.messages as unknown as { meta: Record<string, unknown> }[])[1]
    row.meta = { ...row.meta, mid: undefined, registry: 'coordinator', approval_target: { origin: 'coordinator', id: 'ap-123', slot: 'slot-1', instance: 'inst-123' } }
    const store = createTestStore(state)
    const { result } = renderHookWithProviders(() => useToolApproval({ slotId: 'slot-1', slotApprovalChrome: true, approvalMode: 'normal', dispatch: store.dispatch }), { store })
    await act(async () => { result.current.handleApprovalAction('trust') })
    expect(api.approveChatSlot).not.toHaveBeenCalled()
    expect(api.decideApproval).toHaveBeenCalledWith(expect.objectContaining({ origin: 'coordinator', id: 'ap-123' }), 'approve', expect.stringMatching(/^r_/))
    await waitFor(() => expect(result.current.approvalNoticeKind).toBe('granted'))
    renderWithProviders(<>{result.current.approvalNotice}</>)
    expect(document.body.textContent).toContain('Allowed once')
    expect(document.body.textContent).not.toContain('Trusted')
  })

  it('a one-shot receipt does not show under the next request when the resolve frame lands first', async () => {
    // Two requests in the slot; the card shows the last. This tab's own
    // approval_resolved frame moves the card to the other one before the
    // HTTP reply, so "Allowed once" would sit under a card still waiting.
    const state = stateWithApproval()
    const msgs = state.chat!.messages as unknown as { role: string; content: string; meta: Record<string, unknown> }[]
    const r1 = msgs[1]
    const r0 = { ...r1, content: 'Running: ls /var', meta: { ...r1.meta, approval_id: 'ap-100', mid: 'mid-100', request_id: 'req-100', tool_call_id: 'tc-0', tool_input: '{"command":"ls /var"}', full_command: 'ls /var' } }
    msgs.splice(1, 0, r0)
    const store = createTestStore(state)
    vi.mocked(api.decideApproval).mockImplementationOnce(async () => {
      act(() => { store.dispatch(resolveApprovalRow({ target: { origin: 'native', id: 'ap-123', slot: 'slot-1', mid: 'mid-123' }, decision: 'approved' })) })
      return {} as never
    })
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /tmp')
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))
    await waitFor(() => expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /var'))
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('approval-receipt')).not.toBeInTheDocument()
  })

  it('a coordinator request that takes the card under the same id before the reply lands gets no receipt from the last one', async () => {
    // Coordinator rows carry no mid: each request is its own instance, so the
    // instance is what tells A's reply from B's card.
    const target = (instance: string) => ({ origin: 'coordinator', id: 'ap-123', slot: 'slot-1', instance })
    const state = stateWithApproval()
    const row = (state.chat!.messages as unknown as { meta: Record<string, unknown> }[])[1]
    row.meta = { ...row.meta, mid: undefined, registry: 'coordinator', approval_target: target('inst-a') }
    const store = createTestStore(state)
    let land: (v: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockImplementationOnce(() => new Promise(r => { land = r }) as never)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))
    act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: 'Running: ls /var', meta: { ...row.meta, tool_input: '{"command":"ls /var"}', full_command: 'ls /var', approval_target: target('inst-b'), tool_call_id: 'tc-2' } }))
    })
    expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /var')
    await act(async () => { land({}) })
    expect(screen.queryByTestId('approval-receipt')).not.toBeInTheDocument()
    expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /var')
  })

  it('a session switch drops the last session\'s card at once, not in a fade over the new composer', () => {
    const state = stateWithApproval()
    Object.assign(state.chat!, { slotMessages: { 'slot-2': [] }, slotActivity: {} })
    const store = createTestStore(state)
    const { rerender } = renderWithProviders(<SlotProvider slotId="slot-1"><ChatInput {...defaultProps} /></SlotProvider>, { store })
    expect(screen.getByTestId('approval-card-motion')).toBeInTheDocument()
    rerender(<SlotProvider slotId="slot-2"><ChatInput {...defaultProps} /></SlotProvider>)
    // Slot-2 has no request: slot-1's card, buttons and all, is gone in this render.
    expect(screen.queryByTestId('approval-card-motion')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Allow once' })).not.toBeInTheDocument()
  })

  it('an armed all-tools row does not carry over to the next request in the same session', () => {
    // The card stays mounted when the next request arrives in the same slot, so
    // the menu's first-click consent must belong to the request it was given on.
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    openTrust()
    clickTrustRow('All tools')
    expect(screen.getByText('Click again to confirm — every tool runs without asking')).toBeInTheDocument()
    act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: 'Running: ls /var', meta: { approval_id: 'ap-456', mid: 'mid-456', tool_input: '{"command":"ls /var"}', is_shell: '1', full_command: 'ls /var', base_command: 'ls', trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1', tool_call_id: 'tc-2' } }))
    })
    expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /var')
    expect(screen.queryByText('Click again to confirm — every tool runs without asking')).not.toBeInTheDocument()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('a late reply for the last session leaves this session\'s notice in place', async () => {
    // Decide on slot-1, move the composer to slot-2, which shows its own
    // notice, then slot-1's reply lands: it must not overwrite slot-2's line.
    let land: (v: unknown) => void = () => {}
    vi.mocked(api.approveChatSlot).mockImplementationOnce(() => new Promise(r => { land = r }) as never)
    const state = stateWithApproval()
    Object.assign(state.chat!, { slotMessages: { 'slot-2': [] }, slotActivity: {} })
    const store = createTestStore(state)
    let slot = 'slot-1'
    const { result, rerender } = renderHookWithProviders(() => useToolApproval({ slotId: slot, slotApprovalChrome: true, approvalMode: 'normal', dispatch: store.dispatch }), { store })
    act(() => { result.current.handleApprovalAction('trust_command', 'ls /tmp') })
    slot = 'slot-2'
    rerender()
    act(() => { result.current.showApprovalStatus('slot-2 notice') })
    await act(async () => { land({}) })
    expect(result.current.approvalNotice).toBe('slot-2 notice')
  })

  it('a standing grant\'s receipt does not come back when the composer returns to its session', async () => {
    const state = stateWithApproval()
    Object.assign(state.chat!, { slotMessages: { 'slot-2': [] }, slotActivity: {} })
    const store = createTestStore(state)
    const { rerender } = renderWithProviders(<SlotProvider slotId="slot-1"><ChatInput {...defaultProps} /></SlotProvider>, { store })
    trustAllTools()
    expect(await screen.findByTestId('approval-receipt')).toHaveTextContent('Trusted all tools')
    rerender(<SlotProvider slotId="slot-2"><ChatInput {...defaultProps} /></SlotProvider>)
    rerender(<SlotProvider slotId="slot-1"><ChatInput {...defaultProps} /></SlotProvider>)
    expect(screen.queryByTestId('approval-receipt')).not.toBeInTheDocument()
  })

  /** Make the named boxes report content taller than the box. */
  function clipCommandBox(ids: string[] = ['approval-command', 'trust-command-full', 'approval-input']) {
    const box = (el: Element) => ids.includes((el as HTMLElement).dataset?.testid ?? '')
    const sh = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    const ch = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get() { return box(this) ? 600 : 0 } })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get() { return box(this) ? 200 : 0 } })
    onTestFinished(() => {
      // Restore an own descriptor, or drop the override to fall back to Element's.
      if (sh) Object.defineProperty(HTMLElement.prototype, 'scrollHeight', sh)
      else delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight
      if (ch) Object.defineProperty(HTMLElement.prototype, 'clientHeight', ch)
      else delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight
    })
  }

  it('a command taller than its box fades at the foot and is printed whole in the exact-command row', () => {
    clipCommandBox()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    // The box says more sits below the fold.
    expect(screen.getByTestId('approval-command')).toHaveAttribute('data-more-below')
    openTrust()
    // The row the grant is chosen from carries the string it grants.
    const row = screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith('This exact command — for this session'))!
    const reprint = within(row).getByTestId('trust-command-full')
    expect(reprint).toHaveTextContent('ls /tmp')
    // The reprint is bounded too, and cues its own clip, so the rows stay in view.
    expect(reprint).toHaveAttribute('data-more-below')
  })

  it('a clipped command box cues the edge above once scrolled', () => {
    clipCommandBox()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const box = screen.getByTestId('approval-command')
    expect(box).not.toHaveAttribute('data-more-above')
    act(() => { box.scrollTop = 150; fireEvent.scroll(box) })
    expect(box).toHaveAttribute('data-more-above')
    expect(box).toHaveAttribute('data-more-below')
  })

  it('the next request opens its command box afresh, not at the last one\'s scroll', () => {
    clipCommandBox()
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    const first = screen.getByTestId('approval-command')
    act(() => { first.scrollTop = 400; fireEvent.scroll(first) })
    act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: 'Running: ls /var', meta: { approval_id: 'ap-456', mid: 'mid-456', tool_input: '{"command":"ls /var"}', is_shell: '1', full_command: 'ls /var', base_command: 'ls', trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1', tool_call_id: 'tc-2' } }))
    })
    const next = screen.getByTestId('approval-command')
    expect(next).toHaveTextContent('ls /var')
    expect(next).not.toBe(first)
    expect(next.scrollTop).toBe(0)
    expect(next).not.toHaveAttribute('data-more-above')
    expect(next).toHaveAttribute('data-more-below')
  })

  it('a call\'s arguments taller than their box cue the clip', () => {
    clipCommandBox()
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.meta = { ...msg.meta, is_shell: '', is_read_only: '', tool_name: 'post_message', mcp_server: 'slack-mcp', tool_input: '{"channel":"#team"}', full_command: undefined, base_command: undefined, trust_command_grantable: undefined, trust_base_grantable: undefined }
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    expect(screen.getByTestId('approval-input')).toHaveAttribute('data-more-below')
  })

  /** A short pane: a 60px title overlay over a pane whose top is at 0, with the
   *  card's region and card boxes placed as the dock would put them. */
  function shortPane({ cardTop, regionTop, regionBottom }: { cardTop: number; regionTop: number; regionBottom: number }) {
    const box = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 400, width: 400, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect
    const overlay = document.createElement('div')
    overlay.setAttribute('data-transcript-top-overlay', '')
    let overlayBottom = 60
    overlay.getBoundingClientRect = () => box(0, overlayBottom)
    document.body.appendChild(overlay)
    const orig = HTMLElement.prototype.getBoundingClientRect
    HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
      if (this.dataset.testid === 'approval-request-region') return box(regionTop, regionBottom)
      if (this.querySelector?.(':scope > [data-testid="approval-request-region"]')) return box(cardTop, regionBottom + 40)
      return orig.call(this)
    }
    // The region's content fills its box, as a request taller than the room does.
    const sh = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get(this: HTMLElement) { return this.dataset?.testid === 'approval-request-region' ? regionBottom - regionTop : (sh?.get?.call(this) ?? 0) },
    })
    onTestFinished(() => {
      HTMLElement.prototype.getBoundingClientRect = orig
      overlay.remove()
      if (sh) Object.defineProperty(HTMLElement.prototype, 'scrollHeight', sh)
      else delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight
    })
    return { setOverlayBottom: (px: number) => { overlayBottom = px } }
  }

  it('on a short pane the card\'s request region is capped below the title overlay', () => {
    // Card top at 20px would sit under the 60px overlay: the heading is 40px,
    // the region ends at 500px, so the region may be 500 - (60 + 8) - 40 = 392px.
    shortPane({ cardTop: 20, regionTop: 60, regionBottom: 500 })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const region = screen.getByTestId('approval-request-region')
    expect(region.style.maxHeight).toBe('min(40svh, 392px)')
    // The command Allow once runs lives inside the capped, cued region.
    expect(region).toContainElement(screen.getByTestId('approval-command'))
  })

  it('on a short pane the exact-command row prints the command whatever the card\'s box shows', () => {
    // Room 200 - 68 - 40 = 92px, above the floor: the card's controls stay live.
    shortPane({ cardTop: -40, regionTop: 0, regionBottom: 200 })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const row = openTrust().find(b => rowLabel(b) === 'This exact command — for this session')!
    expect(within(row).getByTestId('trust-command-full')).toHaveTextContent('ls /tmp')
  })

  it('on a pane too short for the request, Allow once and Trust hold and say why; Reject stays', () => {
    // Room 120 - 68 - 40 = 12px, under the 64px floor: the region still takes
    // 64px, so the card's top is under the overlay, where no edge cue reaches.
    shortPane({ cardTop: -200, regionTop: -160, regionBottom: 120 })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const reason = screen.getByTestId('approval-cramped')
    expect(reason).toHaveTextContent('Too little room to show this request whole. Make the window taller or close the on-screen keyboard to allow it.')
    const allow = screen.getByRole('button', { name: 'Allow once' })
    expect(allow).toBeDisabled()
    expect(allow).toHaveAttribute('aria-describedby', reason.id)
    expect(screen.getByRole('button', { name: 'Trust' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled()
  })

  it('a cramped card exposes its held controls as disabled and draws Allow once without its fill', () => {
    shortPane({ cardTop: -200, regionTop: -160, regionBottom: 120 })
    const state = stateWithApproval()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    const allow = screen.getByRole('button', { name: 'Allow once' })
    // Native `disabled`, so assistive tech reads it as unavailable, not a live button.
    expect(allow).toHaveAttribute('disabled')
    expect(screen.getByRole('button', { name: 'Trust' })).toHaveAttribute('disabled')
    // Disabled, the filled control takes the outlined look the other controls have.
    expect(allow.className).toContain('disabled:bg-transparent')
    expect(allow.className).toContain('disabled:text-text')
  })

  it('a cramped override card holds Override too, exposed as disabled', () => {
    shortPane({ cardTop: -200, regionTop: -160, regionBottom: 120 })
    const state = stateWithApproval()
    Object.assign(state.chat!.messages[1].meta!, { deny_override_rule: 'curl .*', deny_override_command: 'ls /tmp', deny_override_reason: 'Blocked by security policy' })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    expect(screen.getByTestId('approval-cramped')).toBeInTheDocument()
    expect(screen.getByTestId('deny-override-trigger')).toHaveAttribute('disabled')
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled()
  })

  it('a top overlay that grows or moves on scroll re-measures the cap', () => {
    const pane = shortPane({ cardTop: 20, regionTop: 60, regionBottom: 500 })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const region = screen.getByTestId('approval-request-region')
    expect(region.style.maxHeight).toBe('min(40svh, 392px)')
    // The pinned-prompt banner appears as the transcript scrolls: 500 - (100 + 8) - 40.
    pane.setOverlayBottom(100)
    act(() => { fireEvent.scroll(document.body) })
    expect(region.style.maxHeight).toBe('min(40svh, 352px)')
  })

  it('the next request opens the request region at its top, with its cue measured afresh', () => {
    clipCommandBox(['approval-request-region'])
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    const first = screen.getByTestId('approval-request-region')
    act(() => { first.scrollTop = 400; fireEvent.scroll(first) })
    expect(first).toHaveAttribute('data-more-above')
    act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: 'Running: ls /var', meta: { approval_id: 'ap-456', mid: 'mid-456', tool_input: '{"command":"ls /var"}', is_shell: '1', full_command: 'ls /var', base_command: 'ls', trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1', tool_call_id: 'tc-2' } }))
    })
    const next = screen.getByTestId('approval-request-region')
    expect(next).not.toBe(first)
    expect(next.scrollTop).toBe(0)
    expect(next).not.toHaveAttribute('data-more-above')
    expect(next).toHaveAttribute('data-more-below')
  })

  it('the Trust menu cues its own scroll on a short screen', () => {
    clipCommandBox(['trust-menu'])
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    openTrust()
    const menu = screen.getByTestId('trust-menu')
    expect(menu).toHaveAttribute('data-more-below')
    act(() => { menu.scrollTop = 150; fireEvent.scroll(menu) })
    expect(menu).toHaveAttribute('data-more-above')
  })

  it('the card takes no input until its fade-in ends', async () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const motion = screen.getByTestId('approval-card-motion')
    // From the first frame, while the card is still near-transparent.
    expect(motion).toHaveAttribute('data-settling')
    expect(motion.inert).toBe(true)
    await waitFor(() => expect(motion).not.toHaveAttribute('data-settling'), CARD_FADE_SETTLED)
    expect(motion.inert).toBe(false)
  })

  it('under reduced motion the card takes input at once', () => {
    reduceMotion()
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const motion = screen.getByTestId('approval-card-motion')
    expect(motion).not.toHaveAttribute('data-settling')
    expect(motion.inert).toBe(false)
  })

  it('a card that comes back while fading out (the next request lands) waits for its fade-in again', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    const motion = screen.getByTestId('approval-card-motion')
    await waitFor(() => expect(motion).not.toHaveAttribute('data-settling'), CARD_FADE_SETTLED)
    act(() => { store.dispatch(resolveApprovalRow({ target: { origin: 'native', id: 'ap-123', slot: 'slot-1', mid: 'mid-123' }, decision: 'approved' })) })
    // Fading out: the old card's controls take nothing.
    expect(screen.getByTestId('approval-card-motion')).toHaveAttribute('data-settling')
    act(() => {
      store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: 'Running: ls /var', meta: { approval_id: 'ap-456', mid: 'mid-456', tool_input: '{"command":"ls /var"}', is_shell: '1', full_command: 'ls /var', base_command: 'ls', trust_command_grantable: '1', trust_base_grantable: '1', trust_grantable: '1', tool_call_id: 'tc-2' } }))
    })
    const back = screen.getByTestId('approval-card-motion')
    expect(screen.getByTestId('approval-command')).toHaveTextContent('ls /var')
    expect(back).toHaveAttribute('data-settling')
    expect(back.inert).toBe(true)
    await waitFor(() => expect(back).not.toHaveAttribute('data-settling'), CARD_FADE_SETTLED)
    expect(back.inert).toBe(false)
  })

  it('the Trust menu re-cues its edge when a row grows at the menu\'s cap', () => {
    // A menu at its height cap scrolled to its foot: nothing below. Arming the
    // all-tools row lengthens its label past the edge.
    let content = 400
    const sh = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    const ch = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
    const menu = (el: Element) => (el as HTMLElement).dataset?.testid === 'trust-menu'
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get() { return menu(this) ? content : 0 } })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get() { return menu(this) ? 200 : 0 } })
    onTestFinished(() => {
      if (sh) Object.defineProperty(HTMLElement.prototype, 'scrollHeight', sh)
      else delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight
      if (ch) Object.defineProperty(HTMLElement.prototype, 'clientHeight', ch)
      else delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight
    })
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    openTrust()
    const el = screen.getByTestId('trust-menu')
    act(() => { el.scrollTop = 200; fireEvent.scroll(el) })
    expect(el).not.toHaveAttribute('data-more-below')
    content = 440
    clickTrustRow('All tools')
    expect(el).toHaveAttribute('data-more-below')
  })

  it('the Trust menu names the tool a call with no command runs', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(mcpCallWithMenu('')) })
    openTrust()
    const call = screen.getByTestId('trust-pending-call')
    expect(call).toHaveTextContent('MCP tool · slack-mcp')
    expect(within(call).getByTestId('trust-pending-call-tool')).toHaveTextContent('post_message')
  })

  it('PageDown on the menu\'s call pages through its box', () => {
    clipCommandBox(['trust-pending-call-text'])
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(mcpCallWithMenu('{"channel":"#team","text":"ship it"}')) })
    openTrust()
    const item = screen.getByTestId('trust-pending-call')
    const box = screen.getByTestId('trust-pending-call-text')
    fireEvent.keyDown(item, { key: 'PageDown' })
    expect(box.scrollTop).toBeGreaterThan(0)
    // Choosing it grants nothing.
    fireEvent.click(item)
    expect(api.approveChatSlot).not.toHaveBeenCalled()
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('PageDown on the menu\'s call reaches the rest of the input once the command is paged through', () => {
    // Both boxes clipped: the first PageDown pages the command, and once it is
    // at its foot, the next ones page the working directory and the rest.
    clipCommandBox(['trust-pending-call-command', 'trust-pending-call-text'])
    const state = stateWithApproval()
    state.chat!.messages[1].meta!.tool_input = '{"command":"ls /tmp","cwd":"/srv"}'
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    openTrust()
    const item = screen.getByTestId('trust-pending-call')
    const command = screen.getByTestId('trust-pending-call-command')
    const rest = screen.getByTestId('trust-pending-call-text')
    fireEvent.keyDown(item, { key: 'PageDown' })
    expect(command.scrollTop).toBeGreaterThan(0)
    expect(rest.scrollTop).toBe(0)
    act(() => { command.scrollTop = 400 })
    fireEvent.keyDown(item, { key: 'PageDown' })
    expect(rest.scrollTop).toBeGreaterThan(0)
  })

  it('PageDown on the exact-command row pages through the command', () => {
    clipCommandBox(['trust-command-full'])
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    const row = openTrust().find(b => rowLabel(b) === 'This exact command — for this session')!
    const box = within(row).getByTestId('trust-command-full')
    fireEvent.keyDown(row, { key: 'PageDown' })
    expect(box.scrollTop).toBeGreaterThan(0)
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  /** A call whose Trust menu has two or more rows, with this input. */
  function mcpCallWithMenu(toolInput: string) {
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.meta = { ...msg.meta, is_shell: '', is_read_only: '', tool_name: 'post_message', mcp_server: 'slack-mcp', tool_input: toolInput, full_command: undefined, base_command: undefined, trust_command_grantable: undefined, trust_base_grantable: undefined, always_allow_tool_grantable: '1', always_allow_tool_ref: '@slack-mcp/post_message' }
    return state
  }

  it('the Trust menu prints the call each row also runs, since it covers the card', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(mcpCallWithMenu('{"channel":"#team","text":"ship it"}')) })
    openTrust()
    const call = screen.getByTestId('trust-pending-call')
    expect(call).toHaveTextContent('Each option also runs this now')
    expect(within(call).getByTestId('trust-pending-call-text').textContent).toBe('{"channel":"#team","text":"ship it"}')
  })

  it('the Trust menu prints a shell call\'s other fields with its command', () => {
    const state = stateWithApproval()
    state.chat!.messages[1].meta!.tool_input = '{"command":"ls /tmp","cwd":"/srv"}'
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(state) })
    openTrust()
    // Two blocks, as on the card: the working directory never reads as a second command line.
    expect(screen.getByTestId('trust-pending-call-command').textContent).toBe('ls /tmp')
    expect(screen.getByTestId('trust-pending-call-text').textContent).toBe('{"cwd":"/srv"}')
  })

  it('the Trust menu does not print a shell call twice when the exact-command row holds all of it', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    openTrust()
    expect(screen.queryByTestId('trust-pending-call')).not.toBeInTheDocument()
    expect(screen.getByTestId('trust-command-full')).toHaveTextContent('ls /tmp')
  })

})
