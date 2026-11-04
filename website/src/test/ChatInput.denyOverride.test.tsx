import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import type { RootState } from '../store'
import { sseChatMessage } from '../store/chatSlice'

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

function stateWithOverrideCard(extraMeta: Record<string, string> = {}): Partial<RootState> {
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
            deny_override_command: 'rm -rf /',
            ...extraMeta,
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

  it('leads with one plain line on what the rule protects, and keeps the rule id behind Details', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithOverrideCard({ deny_override_family: 'wipe' })) })
    expect(screen.getByTestId('deny-override-protects').textContent)
      .toBe('This security rule stops commands that could erase files or whole disks on this computer.')
    const details = screen.getByTestId('deny-override-details')
    expect(details.tagName).toBe('DETAILS')
    expect(details).not.toHaveAttribute('open')
    expect(details.textContent).toContain('Rule: rm -rf /.*')
    expect(details.textContent).toContain('force-deletion rooted at the filesystem root')
  })

  it.each([[undefined], ['not-a-family']])('falls back to the generic line for family %s', (family) => {
    const meta: Record<string, string> = family ? { deny_override_family: family } : {}
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithOverrideCard(meta)) })
    expect(screen.getByTestId('deny-override-protects').textContent).toBe('A security rule stopped this command.')
  })

  it('shows the refused command and a trigger that only opens the menu', () => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithOverrideCard()) })
    // The person reads the exact command an allow would run, and that it has not run.
    expect(screen.getByTestId('deny-override-command').textContent).toContain('rm -rf /')
    expect(screen.getByTestId('deny-override-warning').textContent).toContain('It has not run.')
    const trigger = screen.getByTestId('deny-override-trigger')
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu')
    expect(trigger.textContent).toBe('Choose how to allow')
    fireEvent.click(trigger)
    // Opening the menu decides nothing; only a row does.
    expect(api.approveChatSlot).not.toHaveBeenCalled()
    expect(api.resolveApproval).not.toHaveBeenCalled()
    const session = screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith('Allow for this session'))!
    // No family on this card, so the row says it covers this same rule only.
    expect(session.textContent).toBe('Allow for this session: stop blocking any command this rule blocks, until you close this chat (Session options → Close session) or Kiro Crew restarts')
  })

  it.each([
    ['git_publish', 'Allow for this session: stop this rule from blocking pushes that could land straight on your main branch, until you close this chat (Session options → Close session) or Kiro Crew restarts'],
    ['wipe', 'Allow for this session: stop this rule from blocking commands that could erase files or whole disks on this computer, until you close this chat (Session options → Close session) or Kiro Crew restarts'],
    ['not-a-family', 'Allow for this session: stop blocking any command this rule blocks, until you close this chat (Session options → Close session) or Kiro Crew restarts'],
  ])('the "Allow for this session" row names what family %s lets through', (family, label) => {
    renderWithProviders(<ChatInput {...defaultProps} />, { store: createTestStore(stateWithOverrideCard({ deny_override_family: family })) })
    fireEvent.click(screen.getByTestId('deny-override-trigger'))
    const session = screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith('Allow for this session'))!
    expect(session.textContent).toBe(label)
  })

  it('names every family the warning line knows on the session row too', async () => {
    const { denyOverrideFamilyLine, denyOverrideSessionLabel } = await import('../components/DenyOverrideDropdown')
    const families = ['git_publish', 'self_protection', 'sandbox_escape', 'credentials', 'file_upload', 'remote_control',
      'cloud', 'wipe', 'permissions', 'discard_changes', 'download_run', 'database']
    const generic = denyOverrideSessionLabel(undefined)
    for (const f of families) {
      // A family the warning line names must not fall back to the generic row.
      expect(denyOverrideFamilyLine(f)).not.toBe(denyOverrideFamilyLine(undefined))
      expect(denyOverrideSessionLabel(f)).not.toBe(generic)
    }
  })

  it.each([
    ['Allow once', 'deny_override_once'],
    ['Allow for this session', 'deny_override_session'],
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

  it('a 404 on an override retires only the clicked row, not a newer one reusing its id', async () => {
    const { ApiError } = await import('../api/client')
    let rejectCall: (e: unknown) => void = () => {}
    vi.mocked(api.approveChatSlot).mockImplementationOnce(() => new Promise((_res, rej) => { rejectCall = rej }))
    const store = createTestStore(stateWithOverrideCard())
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByTestId('deny-override-trigger'))
    fireEvent.click(screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith('Allow once'))!)
    await waitFor(() => expect(api.approveChatSlot).toHaveBeenCalledTimes(1))
    // A replaced connection reuses the id for a new, still pending request.
    const first = store.getState().chat.messages.find(m => m.meta?.mid === 'mid-1')!
    store.dispatch(sseChatMessage({ slot: 'slot-1', role: 'permission', content: first.content, meta: { ...first.meta, mid: 'mid-2' } }))
    rejectCall(new (ApiError as unknown as new (s: number, m: string) => Error)(404, 'gone'))
    await waitFor(() => {
      const rows = store.getState().chat.messages.filter(m => m.meta?.approval_id === 'ap-1')
      expect(rows.find(m => m.meta?.mid === 'mid-1')?.meta?.resolved).toBe('stale')
    })
    const rows = store.getState().chat.messages.filter(m => m.meta?.approval_id === 'ap-1')
    expect(rows.find(m => m.meta?.mid === 'mid-2')).toBeTruthy()
    expect(rows.find(m => m.meta?.mid === 'mid-2')?.meta?.resolved).toBeUndefined()
  })

  it('settles the clicked override row when an older row reuses its request id', async () => {
    const state = stateWithOverrideCard()
    const chat = state.chat as unknown as { messages: Array<Record<string, unknown>> }
    // A replaced ACP connection reused the id: the older card is already settled.
    chat.messages.splice(1, 0, {
      role: 'permission',
      content: 'Running: ls',
      meta: { approval_id: 'ap-1', mid: 'mid-0', request_id: 'req-1', tool_input: '{"command":"ls"}', resolved: 'approved' },
    })
    const store = createTestStore(state)
    renderWithProviders(<ChatInput {...defaultProps} />, { store })
    fireEvent.click(screen.getByTestId('deny-override-trigger'))
    fireEvent.click(screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith('Allow once'))!)
    await waitFor(() => expect(api.approveChatSlot).toHaveBeenCalledTimes(1))
    await waitFor(() => {
      const rows = store.getState().chat.messages.filter(m => m.meta?.approval_id === 'ap-1')
      expect(rows.find(m => m.meta?.mid === 'mid-1')?.meta?.resolved).toBe('deny_override_once')
    })
    const older = store.getState().chat.messages.find(m => m.meta?.mid === 'mid-0')
    expect(older?.meta?.resolved).toBe('approved')
  })
})
