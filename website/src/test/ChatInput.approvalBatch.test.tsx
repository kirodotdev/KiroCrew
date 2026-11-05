/**
 * #29747: when one agent turn raises several tool approvals, the main-chat
 * ApprovalBar must offer "Approve all N / Reject all N" with a preview of all N
 * calls, and a batch reject must resolve EVERY pending approval — not just the
 * newest one, which left the other N-1 still prompting.
 *
 * Red without the fix: the bar showed only the single newest approval, so there
 * was no batch control and `resolveApproval` was called once.
 *
 * The single-approval (N=1) case must stay byte-for-byte as before: no batch
 * control renders, and nothing about the existing single row changes. The last
 * `it` pins that.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import type { RootState } from '../store'

vi.mock('../api/client', () => {
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

function permission(id: string, command: string) {
  return {
    role: 'permission',
    content: `Running: ${command}`,
    meta: {
      approval_id: id,
      request_id: `req-${id}`,
      tool_input: `{"command":"${command}"}`,
      is_read_only: '1',
      tool_title: `Running: ${command}`,
      is_shell: '1',
      full_command: command,
      base_command: command.split(' ')[0],
      tool_call_id: `tc-${id}`,
    },
  }
}

/** A slot whose last turn raised `ids.length` approvals, all still pending. */
function stateWithApprovals(ids: Array<[string, string]>): Partial<RootState> {
  return {
    chat: {
      activeSlot: 'slot-1',
      messages: [
        { role: 'user', content: 'do several things' },
        ...ids.map(([id, cmd]) => permission(id, cmd)),
      ],
      toolLog: [],
      slotStatusDetail: {},
    } as unknown as RootState['chat'],
    dashboard: {
      slots: [{ key: 'slot-1', messages: ids.length + 1, running: true, pending_approval: true, waiting_for_input: false, last_activity_ts: undefined }],
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

describe('ChatInput batch approvals (#29747)', () => {
  it('renders Approve all N / Reject all N with a preview of every pending call when N >= 2', async () => {
    const store = createTestStore(stateWithApprovals([['ap-1', 'ls /tmp'], ['ap-2', 'cat a.txt'], ['ap-3', 'rm b.txt']]))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })

    const batch = await screen.findByTestId('approval-batch')
    // The count-bearing batch buttons name all three.
    expect(screen.getByRole('button', { name: 'Approve all 3' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject all 3' })).toBeInTheDocument()
    // Preview shows every pending call's command, not just the newest.
    expect(batch).toHaveTextContent('ls /tmp')
    expect(batch).toHaveTextContent('cat a.txt')
    expect(batch).toHaveTextContent('rm b.txt')
  })

  it('Reject all N resolves EVERY pending approval, not just one', async () => {
    const store = createTestStore(stateWithApprovals([['ap-1', 'ls /tmp'], ['ap-2', 'cat a.txt'], ['ap-3', 'rm b.txt']]))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })

    fireEvent.click(await screen.findByRole('button', { name: 'Reject all 3' }))
    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledTimes(3))
    for (const id of ['ap-1', 'ap-2', 'ap-3']) {
      expect(api.resolveApproval).toHaveBeenCalledWith(id, 'reject', undefined, expect.stringMatching(/^r_/))
    }
  })

  it('Approve all N approves every pending approval', async () => {
    const store = createTestStore(stateWithApprovals([['ap-1', 'ls /tmp'], ['ap-2', 'cat a.txt']]))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })

    fireEvent.click(await screen.findByRole('button', { name: 'Approve all 2' }))
    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledTimes(2))
    expect(api.resolveApproval).toHaveBeenCalledWith('ap-1', 'approve', undefined, expect.stringMatching(/^r_/))
    expect(api.resolveApproval).toHaveBeenCalledWith('ap-2', 'approve', undefined, expect.stringMatching(/^r_/))
  })

  it('single pending approval (N=1) renders NO batch control and keeps the single row', () => {
    const store = createTestStore(stateWithApprovals([['ap-1', 'ls /tmp']]))
    renderWithProviders(<ChatInput {...defaultProps} />, { store })

    expect(screen.queryByTestId('approval-batch')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Approve all/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Reject all \d/ })).not.toBeInTheDocument()
    // The existing single-row path is untouched: Allow once + Reject stay.
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
  })
})
