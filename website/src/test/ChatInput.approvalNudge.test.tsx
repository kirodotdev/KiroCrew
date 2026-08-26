import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { APPROVAL_MODE_ADJUSTED_LS_KEY } from '../components/ApprovalModePicker'
import { api } from '../api/client'
import { sseChatMessage } from '../store/chatSlice'
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
      chatMode: vi.fn(() => Promise.resolve({})),
    },
    ApiError: MockApiError,
  }
})

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  approvalMode: 'normal',
}

function permissionMsg(n: number, meta: Record<string, unknown> = {}) {
  return {
    role: 'permission',
    content: `Running: ls /tmp/${n}`,
    meta: {
      approval_id: `ap-${n}`,
      request_id: `req-${n}`,
      tool_input: `{"command":"ls /tmp/${n}"}`,
      tool_title: `Running: ls /tmp/${n}`,
      full_command: `ls /tmp/${n}`,
      base_command: 'ls',
      tool_call_id: `tc-${n}`,
      ...meta,
    },
  }
}

function stateWithApproval(meta: Record<string, unknown> = {}): Partial<RootState> {
  return {
    chat: {
      activeSlot: 'slot-1',
      messages: [
        { role: 'user', content: 'list files' },
        permissionMsg(1, meta),
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
  localStorage.clear()
})

describe('approval bar discoverability hint (A1)', () => {
  it('renders the hint with a link to the approval mode picker', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    expect(screen.getByText('Tired of confirming every step?')).toBeInTheDocument()
    expect(screen.getByText('Adjust approval mode')).toBeInTheDocument()
  })

  it('clicking the hint opens the approval mode picker menu (A2)', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
    fireEvent.click(screen.getByText('Adjust approval mode'))
    const items = screen.getAllByRole('menuitem')
    expect(items.map(i => i.textContent || '').some(t => t.includes('YOLO'))).toBe(true)
  })

  it('is withheld once the user has ever adjusted the mode', () => {
    localStorage.setItem(APPROVAL_MODE_ADJUSTED_LS_KEY, '1')
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    expect(screen.queryByText('Tired of confirming every step?')).not.toBeInTheDocument()
    // the approval bar itself still renders
    expect(screen.getByText('Allow once')).toBeInTheDocument()
  })

  it('is withheld for unattended sources', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval({ source: 'cron' })) })
    expect(screen.getByText('Allow once')).toBeInTheDocument()
    expect(screen.queryByText('Tired of confirming every step?')).not.toBeInTheDocument()
  })
})

describe('approval nudge trigger (B2)', () => {
  async function approveN(store: ReturnType<typeof renderWithProviders>['store'], n: number) {
    for (let i = 1; i <= n; i++) {
      fireEvent.click(screen.getByText('Allow once'))
      await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledTimes(i))
      if (i < n) {
        store.dispatch(sseChatMessage({ slot: 'slot-1', ...permissionMsg(i + 1) } as never))
        await waitFor(() => expect(screen.getByText('Allow once')).toBeInTheDocument())
      }
    }
  }

  it('appears after the third manual approval in one slot', async () => {
    const { store } = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    await approveN(store, 2)
    expect(screen.queryByRole('dialog', { name: 'Want fewer approval prompts?' })).not.toBeInTheDocument()
    store.dispatch(sseChatMessage({ slot: 'slot-1', ...permissionMsg(3) } as never))
    await waitFor(() => expect(screen.getByText('Allow once')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Want fewer approval prompts?' })).toBeInTheDocument())
  })

  it('does not appear when the mode is no longer normal', async () => {
    const { store } = renderWithProviders(<ChatInput {...defaultProps} approvalMode="trust_reads" />, { store: createTestStore(stateWithApproval()) })
    await approveN(store, 3)
    expect(screen.queryByRole('dialog', { name: 'Want fewer approval prompts?' })).not.toBeInTheDocument()
  })

  it('does not appear once the discovery flag is set', async () => {
    localStorage.setItem(APPROVAL_MODE_ADJUSTED_LS_KEY, '1')
    const { store } = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    await approveN(store, 3)
    expect(screen.queryByRole('dialog', { name: 'Want fewer approval prompts?' })).not.toBeInTheDocument()
  })

  it('"Got it" dismisses and writes the permanent flag', async () => {
    const { store } = renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    await approveN(store, 3)
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Want fewer approval prompts?' })).toBeInTheDocument())
    fireEvent.click(screen.getByText('Got it'))
    expect(screen.queryByRole('dialog', { name: 'Want fewer approval prompts?' })).not.toBeInTheDocument()
    // One flag carries both retirements (nudge + A1 hint).
    expect(localStorage.getItem(APPROVAL_MODE_ADJUSTED_LS_KEY)).toBe('1')
  })
})

describe('review-round fixes (hint retirement + coexistence)', () => {
  it('clicking the hint permanently retires it via the adjusted flag', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithApproval()) })
    fireEvent.click(screen.getByText('Adjust approval mode'))
    expect(localStorage.getItem(APPROVAL_MODE_ADJUSTED_LS_KEY)).toBe('1')
  })

  it('the hint row is suppressed while the nudge callout is up', async () => {
    const store = createTestStore(stateWithApproval())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    for (let i = 1; i <= 3; i++) {
      fireEvent.click(screen.getByText('Allow once'))
      await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledTimes(i))
      store.dispatch(sseChatMessage({ slot: 'slot-1', ...permissionMsg(i + 1) } as never))
      await waitFor(() => expect(screen.getByText('Allow once')).toBeInTheDocument())
    }
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Want fewer approval prompts?' })).toBeInTheDocument())
    // A fresh approval card is on screen, but the hint yields to the nudge.
    expect(screen.queryByText('Tired of confirming every step?')).not.toBeInTheDocument()
  })
})
