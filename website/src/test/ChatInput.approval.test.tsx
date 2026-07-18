import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import type { RootState } from '../store'

vi.mock('../api/client', () => ({
  api: {
    resolveApproval: vi.fn(() => Promise.resolve({})),
    approveChatSlot: vi.fn(() => Promise.resolve({})),
  },
}))

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
