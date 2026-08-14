import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { api, ApiError } from '../api/client'
import type { RootState } from '../store'

vi.mock('../api/client', () => {
  // Declared INSIDE the factory: vi.mock is hoisted above module-level
  // declarations, so a top-level class here would be a TDZ error.
  //
  // Must be a real class: ChatInput narrows the rejection with
  // `err instanceof ApiError` to tell an orphaned approval (404) from a
  // genuine transport failure. A bare object stub makes that check false.
  class MockApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    api: {
      resolveApproval: vi.fn(() => Promise.resolve({})),
      approveChatSlot: vi.fn(() => Promise.resolve({})),
    },
    ApiError: MockApiError,
  }
})

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
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
            request_id: 'req-123',
            tool_input: '{"command":"ls /tmp"}',
            is_read_only: '1',
            tool_title: 'Running: ls /tmp',
            full_command: 'ls /tmp',
            base_command: 'ls',
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
    expect(screen.getByText('Trust')).toBeInTheDocument()
  })

  it('shows Reject button', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText('Reject')).toBeInTheDocument()
  })

  it('Allow once calls resolveApproval with approve', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalledWith('ap-123', 'approve')
    })
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('Reject calls resolveApproval with reject', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Reject'))
    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalledWith('ap-123', 'reject')
    })
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('Trust dropdown trust_command calls approveChatSlot with pattern', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const cmdBtn = buttons.find(b => b.textContent?.includes('ls /tmp'))!
    fireEvent.click(cmdBtn)
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_command', { request_id: 'ap-123', pattern: 'ls /tmp' }
      )
    })
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('Trust dropdown trust_base calls approveChatSlot with glob pattern', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    const baseBtn = buttons.find(b => b.textContent?.includes('commands'))!
    fireEvent.click(baseBtn)
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_base', { request_id: 'ap-123', pattern: 'ls *' }
      )
    })
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('Trust dropdown entire tool calls approveChatSlot with trust action', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust', { request_id: 'ap-123' }
      )
    })
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('Trust reads calls approveChatSlot for read-only commands', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust reads'))
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalledWith(
        'slot-1', 'trust_reads', { request_id: 'ap-123' }
      )
    })
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
    expect(screen.getByText('Trust reads')).toBeInTheDocument()
  })

  it('hides Trust reads for non-read-only commands', () => {
    const state = stateWithApproval()
    state.chat!.messages[1].meta!.is_read_only = ''
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.queryByText('Trust reads')).not.toBeInTheDocument()
  })

  it('trust action falls back to resolveApproval when no activeSlot', async () => {
    const state = stateWithApproval()
    state.chat!.activeSlot = null
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalledWith('ap-123', 'approve')
    })
  })

  it('handles API error gracefully without crashing', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValueOnce(new Error('network'))
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    // Should not throw — error is caught internally
    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalled()
    })
  })

  it('handles approveChatSlot error gracefully', async () => {
    vi.mocked(api.approveChatSlot).mockRejectedValueOnce(new Error('network'))
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    // Should not throw
    await waitFor(() => {
      expect(api.approveChatSlot).toHaveBeenCalled()
    })
  })

  it('shows tool input preview in expanded approval bar', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    await waitFor(() => expect(screen.getByText(/command/)).toBeInTheDocument())
  })

  it('uses approvalFullCommand for TrustDropdown', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    // Should show the full command from meta
    expect(buttons.some(b => b.textContent?.includes('ls /tmp'))).toBe(true)
  })

  it('uses approvalBaseCommand for TrustDropdown base option', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    expect(buttons.some(b => b.textContent?.includes('ls') && b.textContent?.includes('commands'))).toBe(true)
  })

  it('detects shell command from tool_title prefix', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    // Should show base command option (only for shell)
    const buttons = screen.getAllByRole('menuitem')
    expect(buttons.some(b => b.textContent?.includes('commands'))).toBe(true)
  })

  it('non-shell tool hides base command option', () => {
    const state = stateWithApproval()
    const msg = state.chat!.messages[1]
    msg.meta!.tool_title = 'TaskeiGetTask'
    msg.meta!.full_command = 'TaskeiGetTask'
    msg.meta!.base_command = 'TaskeiGetTask'
    msg.content = 'TaskeiGetTask'
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Trust'))
    const buttons = screen.getAllByRole('menuitem')
    expect(buttons.some(b => b.textContent?.includes('commands'))).toBe(false)
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
    fireEvent.click(screen.getByText('Trust'))
    fireEvent.click(screen.getByText('Trust all tools'))
    await waitFor(() => {
      expect(screen.queryByText('Allow once')).not.toBeInTheDocument()
    })
    expect(store.getState().chat.messages[1].meta!.resolved).toBe('stale')
  })

  it('clears the approval bar when Allow once 404s', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValueOnce(notFound())
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.queryByText('Allow once')).not.toBeInTheDocument()
    })
  })

  it('explains why the click did nothing instead of failing silently', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValueOnce(notFound())
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Reject'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/expired/i)
    })
  })

  it('keeps the bar up on a non-404 failure so the user can retry', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValueOnce(new ApiError(503, 'busy'))
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toBeInTheDocument()
    })
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
    expect(api.resolveApproval).toHaveBeenCalledWith('spawn:a2', 'approve')
    expect(api.resolveApproval).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.subagents.a2.approving).toBe(true)
    expect(store.getState().chat.subagents.a1.approving).toBeFalsy()
  })

  it('clicking a per-agent Reject rejects only that sub-agent', () => {
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: 'Reject sub-agent: task 3' }))
    expect(api.resolveApproval).toHaveBeenCalledWith('spawn:a3', 'reject')
    expect(api.resolveApproval).toHaveBeenCalledTimes(1)
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
    expect(store.getState().chat.subagents.a1.error).toBe('rejected')
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
      expect(api.resolveApproval).toHaveBeenCalledWith('spawn:a1', 'approve')
    })
    // Approval must NOT synthesize a terminal state — the real run reports it.
    expect(store.getState().chat.subagents.a1.status).toBe('pending')
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
    expect(api.resolveApproval).toHaveBeenCalledWith('spawn:a1', 'approve')
    // Card is marked resolving so the buttons can't be double-submitted.
    expect(store.getState().chat.subagents.a1.approving).toBe(true)
  })

  it('clicking Reject rejects the pending spawn via the approvals API', () => {
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Reject$/ }))
    expect(api.resolveApproval).toHaveBeenCalledWith('spawn:a1', 'reject')
  })

  it('Approve all resolves every pending spawn', () => {
    const store = createTestStore(stateWithPendingSpawn(3))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Approve all/ }))
    expect(api.resolveApproval).toHaveBeenCalledWith('spawn:a1', 'approve')
    expect(api.resolveApproval).toHaveBeenCalledWith('spawn:a2', 'approve')
    expect(api.resolveApproval).toHaveBeenCalledWith('spawn:a3', 'approve')
    expect(api.resolveApproval).toHaveBeenCalledTimes(3)
  })

  it('clicking Review in panel opens the Subagents panel and resolves nothing', () => {
    const store = createTestStore(stateWithPendingSpawn(1))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /Review in panel/ }))
    expect(store.getState().chat.activityTab).toBe('subagents')
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })

  it('shows a Resolving state instead of buttons once all pending spawns are approving', () => {
    const base = stateWithPendingSpawn(1) as { chat: { subagents: Record<string, { approving?: boolean }> } }
    base.chat.subagents.a1.approving = true
    const store = createTestStore(base as Partial<RootState>)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText(/Resolving/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Approve$/ })).not.toBeInTheDocument()
  })

  it('does not render the banner when there are no pending spawns', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.queryByText(/awaiting your approval to run/)).not.toBeInTheDocument()
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
    expect(screen.queryByText('Trust')).not.toBeInTheDocument()
    expect(screen.queryByText('Trust reads')).not.toBeInTheDocument()
    // The actionable controls remain — the card is still answerable.
    expect(screen.getByText('Allow once')).toBeInTheDocument()
    expect(screen.getByText('Reject')).toBeInTheDocument()
  })

  it('keeps Trust for autonudge, which does run in this session', () => {
    const store = createTestStore(withSource('autonudge'))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText('Trust')).toBeInTheDocument()
  })

  it('keeps Trust for an ordinary in-session approval', () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.getByText('Trust')).toBeInTheDocument()
  })

  it('withholds Trust when the source survives only in the card label', () => {
    const store = createTestStore(withSource('cron', { viaLabel: true }))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    expect(screen.queryByText('Trust')).not.toBeInTheDocument()
  })

  it.each([
    ['Allow once', 'approve'],
    ['Reject', 'reject'],
  ])('answers an unattended card via %s without the slot-scoped grant', async (label, decision) => {
    // No control on an unattended card may route through approveChatSlot,
    // which grants on THIS slot. (handleApprovalAction also downgrades a trust
    // decision to a one-shot allow as defence in depth, but the UI withholds
    // those controls entirely, so that branch is not reachable from here.)
    const store = createTestStore(withSource('cron'))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText(label))
    await waitFor(() => {
      expect(api.resolveApproval).toHaveBeenCalledWith('ap-123', decision)
    })
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('names the source when an unattended request already timed out', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValueOnce(new ApiError(404, 'gone'))
    const store = createTestStore(withSource('cron'))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/cron request already timed out/i)
    })
  })

  it('keeps the generic copy for an ordinary expired approval', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValueOnce(new ApiError(404, 'gone'))
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/That approval expired/i)
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
    expect(screen.getByText('Reject')).toBeInTheDocument()
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
      expect(api.resolveApproval).toHaveBeenCalledWith('ap-123', 'approve')
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
