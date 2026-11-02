import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import type { RootState } from '../store'

// A security-rule override card: the server refused the command by
// a deny rule and asks the owner whether to let that one rule step aside.

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
      spawnList: vi.fn(() => Promise.resolve({ agents: [] })),
      sharedCrewCapabilities: vi.fn(() => Promise.resolve(null)),
      sharedCrewMode: vi.fn(() => Promise.resolve(null)),
      sharedCrewPeople: vi.fn(() => Promise.resolve(null)),
      sharedCrewViewer: vi.fn(() => Promise.resolve(null)),
    },
    ApiError: MockApiError,
  }
})

const defaultProps = { value: '', onChange: vi.fn(), onSend: vi.fn() }

function stateWithOverrideCard(): Partial<RootState> {
  return {
    chat: {
      activeSlot: 'slot-1',
      messages: [
        { role: 'user', content: 'clean up' },
        {
          role: 'permission',
          content: 'Running: rm -rf /',
          meta: {
            approval_id: 'ap-1',
            mid: 'mid-1',
            request_id: 'req-1',
            tool_input: '{"command":"rm -rf /"}',
            tool_title: 'Running: rm -rf /',
            is_shell: '1',
            tool_call_id: 'tc-1',
            deny_override_rule: 'rm -rf /.*',
            deny_override_reason: 'Blocked by security policy: rm -rf /.*',
            deny_override_detail: 'Blocks recursive force-deletion rooted at the filesystem root.',
          },
        },
      ],
      toolLog: [],
      slotStatusDetail: {},
    } as unknown as RootState['chat'],
    dashboard: {
      slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true, waiting_for_input: false, last_activity_ts: undefined }],
      approvalMode: 'yolo',
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

describe('ChatInput security-rule override card', () => {
  it('names the rule and offers no Allow once or Trust control', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithOverrideCard()) })
    expect(screen.getByTestId('deny-override-warning').textContent).toContain('rm -rf /.*')
    // The plain reason leads, so the person can judge the command without knowing rule ids.
    expect(screen.getByTestId('deny-override-detail').textContent).toContain('force-deletion rooted at the filesystem root')
    expect(screen.queryByText('Trust')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Allow once' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
    // No approval mode answers this card, so the mode hint is not offered on it.
    expect(screen.queryByText('Adjust approval mode')).not.toBeInTheDocument()
  })

  it.each([
    ['Allow once', 'deny_override_once'],
    ['Allow in this chat', 'deny_override_session'],
  ])('sends %s as its own decision to the slot route', async (label, decision) => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithOverrideCard()) })
    fireEvent.click(screen.getByTestId('deny-override-trigger'))
    const item = screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith(label))!
    expect(item).toBeTruthy()
    fireEvent.click(item)
    await waitFor(() => expect(api.approveChatSlot).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.approveChatSlot).mock.calls[0][0]).toBe('slot-1')
    expect(vi.mocked(api.approveChatSlot).mock.calls[0][1]).toBe(decision)
    // It names the card row it was clicked on, so a recurring id cannot redirect it.
    expect(vi.mocked(api.approveChatSlot).mock.calls[0][2]).toMatchObject({ origin: 'native', request_mid: 'mid-1' })
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })
})
