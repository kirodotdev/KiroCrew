import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import type { RootState } from '../store'

vi.mock('../api/client', () => {
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

const defaultProps = { value: '', onChange: vi.fn(), onSend: vi.fn() }

/** A slot whose newest row is a pending approval carrying a tool call id. The
 *  id is what the focus control needs: without one the button is not rendered
 *  at all, because there is nothing for it to scroll to. */
function stateWithApproval(): Partial<RootState> {
  return {
    chat: {
      activeSlot: 'slot-1',
      messages: [
        { role: 'user', content: 'list files' },
        {
          role: 'permission',
          content: 'Running: ls /tmp',
          meta: {
            approval_id: 'ap-1',
            request_id: 'req-1',
            tool_input: '{"command":"ls /tmp"}',
            tool_title: 'Running: ls /tmp',
            is_shell: '1',
            full_command: 'ls /tmp',
            base_command: 'ls',
            tool_call_id: 'tc-1',
          },
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

describe('an approval card inside a thread names the surface it acts on', () => {
  it('says "chat" on an ordinary composer', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    await waitFor(() => expect(screen.getByText('Show in chat')).toBeInTheDocument())
    expect(screen.queryByText('Show this tool call in this thread')).not.toBeInTheDocument()
  })

  it('says "this thread" on a thread composer, where "chat" would read as the parent', async () => {
    // The control expands the call IN PLACE, in the transcript that owns it. In
    // the drawer that transcript is the thread's, one pane away from the parent's
    // own — so "Show in chat" there invites exactly the wrong reading.
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} inThread />, { store })
    await waitFor(() => expect(screen.getByText('Show this tool call in this thread')).toBeInTheDocument())
    expect(screen.queryByText('Show in chat')).not.toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Show the pending tool call in this thread' }),
    ).toBeInTheDocument()
  })
})
