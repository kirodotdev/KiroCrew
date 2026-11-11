/**
 * The MCP tool list's per-tool "Always allow" toggle and the approval card's
 * "Always allow this tool" row. Both read only server facts: the grant list and
 * refusal code from `/api/security/tool-grants`, and the card's
 * `always_allow_tool_ref`. `i18nT` is mocked to the key so assertions pin keys.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { useEffect, type ReactElement } from 'react'
import { createTestStore, renderWithProviders } from '../../test/helpers'
import { useAppDispatch } from '../../store'
import { useToolApproval } from '../../components/chat-input/approval'
import { McpStagedEditsProvider, useSetMcpStagedEdits } from '../connections/mcpStagedEditsContext'
import { setGatewayConnected, __resetGatewayConnectedForTests } from '../../utils/errorReport'

const { grants, setGrant, approveSlot } = vi.hoisted(() => ({ grants: vi.fn(), setGrant: vi.fn(), approveSlot: vi.fn() }))

vi.mock('../../api/client', () => ({
  api: {
    mcpToolGrants: (...a: unknown[]) => grants(...a),
    mcpToolGrantSet: (...a: unknown[]) => setGrant(...a),
    approveChatSlot: (...a: unknown[]) => approveSlot(...a),
    sharedCrewCapabilities: vi.fn().mockResolvedValue(null),
    sharedCrewMode: vi.fn().mockResolvedValue(null),
    sharedCrewPeople: vi.fn().mockResolvedValue(null),
    sharedCrewViewer: vi.fn().mockResolvedValue(null),
  },
}))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return { ...orig, i18nT: (key: string) => key }
})

import McpToolAlwaysAllow, { McpToolGrantsNote, toolGrantRef } from './McpToolAlwaysAllow'
import TrustDropdown from '../../components/TrustDropdown'

const wrap = (node: ReactElement) => renderWithProviders(node, { queryDefaults: { staleTime: Infinity } })

function StagedEdits() {
  const setStaged = useSetMcpStagedEdits()
  useEffect(() => { setStaged(true) }, [setStaged])
  return null
}

function ApprovalProbe() {
  const dispatch = useAppDispatch()
  const approval = useToolApproval({ slotId: 'slot-1', slotApprovalChrome: true, dispatch })
  return <TrustDropdown fullCommand="" baseCommand="" isShell={false} hasCommand={false} showTrustAll={false} alwaysAllowTool={approval.approvalAlwaysAllowRef} onAction={approval.handleApprovalAction} />
}

afterEach(() => { __resetGatewayConnectedForTests() })

describe('grant cache refreshes', () => {
  // A config refresh frame and a reconnect invalidate the ['mcp-tool-grants'] prefix,
  // so every server's cached grants: pinned in test/useWebSocket.frameTrace.test.ts,
  // since only the useWebSocket facade may import its owner modules.
  it('refreshes a cached tool list after the approval card saves its grant', async () => {
    grants.mockResolvedValue({ grants: [], server: 'notes', refusal: '' })
    approveSlot.mockImplementation(async () => {
      grants.mockResolvedValue({ grants: [{ ref: '@notes/lookup' }], server: 'notes', refusal: '' })
      return {}
    })
    const initial = createTestStore().getState()
    const store = createTestStore({ ...initial, chat: { ...initial.chat, activeSlot: 'slot-1', messages: [{
      role: 'permission', content: '@notes/lookup', meta: {
        approval_id: 'r1', mid: 'mid-1', request_id: 'r1',
        always_allow_tool_grantable: '1', always_allow_tool_ref: '@notes/lookup',
      },
    }] } })
    const view = renderWithProviders(<><ApprovalProbe /><McpToolAlwaysAllow server="notes" tool="lookup" /></>, {
      store, queryDefaults: { staleTime: Infinity },
    })
    view.queryClient.setQueryData(['mcp-tool-grants', 'calendar'], { grants: [] })
    await waitFor(() => expect(screen.getByRole('switch')).not.toHaveAttribute('aria-disabled', 'true'))
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(screen.getByRole('button', { name: /always_allow_this_tool/ }))
    await waitFor(() => expect(approveSlot).toHaveBeenCalledWith('slot-1', 'always_allow_tool', expect.objectContaining({ pattern: '@notes/lookup' })))
    await waitFor(() => expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true'))
    expect(view.queryClient.getQueryState(['mcp-tool-grants', 'calendar'])?.isInvalidated).toBe(true)
  })
})

describe('McpToolAlwaysAllow', () => {
  beforeEach(() => { grants.mockReset(); setGrant.mockReset() })

  it('turns the grant on with the exact @server/tool ref', async () => {
    grants.mockResolvedValue({ grants: [], server: 'notes', refusal: '' })
    setGrant.mockResolvedValue({ grants: [{ ref: '@notes/lookup' }], server: 'notes', refusal: '' })
    wrap(<McpToolAlwaysAllow server="notes" tool="lookup" />)
    const sw = await screen.findByRole('switch')
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled', 'true'))
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    await waitFor(() => expect(setGrant).toHaveBeenCalledWith('@notes/lookup', true))
    await waitFor(() => expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true'))
  })

  it.each(['@notes/lookup', '@notes/LOOKUP', '@NOTES/Lookup'])('turns an existing grant %s off', async ref => {
    grants.mockResolvedValue({ grants: [{ ref }], server: 'notes', refusal: '' })
    setGrant.mockResolvedValue({ grants: [], server: 'notes', refusal: '' })
    wrap(<McpToolAlwaysAllow server="notes" tool="lookup" />)
    const sw = await screen.findByRole('switch')
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    expect(sw).toHaveAccessibleDescription('pages.overview.mcpTab.always_allow_undo')
    expect(screen.getByText('pages.overview.mcpTab.always_allow_undo')).not.toHaveClass('sr-only')
    fireEvent.click(sw)
    await waitFor(() => expect(setGrant).toHaveBeenCalledWith('@notes/lookup', false))
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it('is disabled with the readable reason when the server refuses', async () => {
    grants.mockResolvedValue({ grants: [], server: 'notes', refusal: 'asks_every_time' })
    wrap(<><McpToolGrantsNote server="notes" /><McpToolAlwaysAllow server="notes" tool="lookup" /></>)
    expect(await screen.findByTestId('mcp-always-allow-refusal')).toHaveTextContent(
      'pages.overview.mcpTab.always_allow_refused_asks_every_time')
    expect(screen.getByRole('switch')).toHaveAttribute('aria-disabled', 'true')
  })

  it('does not offer revocation of a grant controlled by the overlay', async () => {
    grants.mockResolvedValue({ grants: [{ ref: '@notes/lookup' }], server: 'notes', refusal: 'set_elsewhere' })
    wrap(<><McpToolGrantsNote server="notes" /><McpToolAlwaysAllow server="notes" tool="lookup" /></>)
    const sw = screen.getByRole('switch')
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    expect(sw).toHaveAttribute('aria-disabled', 'true')
    expect(sw).toHaveAccessibleDescription('pages.overview.mcpTab.always_allow_refused_set_elsewhere')
    fireEvent.click(sw)
    expect(setGrant).not.toHaveBeenCalled()
  })

  it('still lets an operator revoke a base grant when new grants require confirmation', async () => {
    grants.mockResolvedValue({ grants: [{ ref: '@notes/lookup' }], server: 'notes', refusal: 'asks_every_time' })
    setGrant.mockResolvedValue({ grants: [], server: 'notes', refusal: 'asks_every_time' })
    wrap(<McpToolAlwaysAllow server="notes" tool="lookup" />)
    const sw = screen.getByRole('switch')
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    expect(sw).not.toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(sw)
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it.each([false, true])('shows a query error and protects staged MCP edits=%s', async staged => {
    setGatewayConnected(true)
    grants.mockRejectedValue(new Error('grant list unavailable'))
    wrap(<McpStagedEditsProvider>
      {staged && <StagedEdits />}
      <McpToolGrantsNote server="notes" />
      <McpToolAlwaysAllow server="notes" tool="lookup" />
    </McpStagedEditsProvider>)
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('grant list unavailable')
    expect(screen.getByRole('switch')).toHaveAttribute('aria-disabled', 'true')
    const handoff = within(notice).queryByRole('button', { name: 'components.askAgent.ask_the_agent' })
    if (staged) {
      expect(handoff).not.toBeInTheDocument()
      expect(notice).toHaveTextContent('pages.mcpManagement.handoff_hint_staged_edits')
    } else expect(handoff).toBeInTheDocument()
  })

  it.each([false, true])('shows a mutation error and protects staged MCP edits=%s', async staged => {
    setGatewayConnected(true)
    grants.mockResolvedValue({ grants: [], server: 'notes', refusal: '' })
    setGrant.mockRejectedValue(new Error('grant write refused'))
    wrap(<McpStagedEditsProvider>
      {staged && <StagedEdits />}
      <McpToolGrantsNote server="notes" />
      <McpToolAlwaysAllow server="notes" tool="lookup" />
    </McpStagedEditsProvider>)
    await waitFor(() => expect(screen.getByRole('switch')).not.toHaveAttribute('aria-disabled', 'true'))
    fireEvent.click(screen.getByRole('switch'))
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('pages.overview.mcpTab.always_allow_failed')
    const handoff = within(notice).queryByRole('button', { name: 'components.askAgent.ask_the_agent' })
    if (staged) {
      expect(handoff).not.toBeInTheDocument()
      expect(notice).toHaveTextContent('pages.mcpManagement.handoff_hint_staged_edits')
    } else expect(handoff).toBeInTheDocument()
  })

  it('offers nothing for a name that is not one exact tool', () => {
    expect(toolGrantRef('notes', 'look*')).toBe('')
    expect(toolGrantRef('a/b', 'x')).toBe('')
    expect(toolGrantRef('notes', 'lookup')).toBe('@notes/lookup')
  })
})

describe('TrustDropdown always-allow row', () => {
  it('fires always_allow_tool with the server-derived ref', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="" baseCommand="" isShell={false} hasCommand={false} showTrustAll={false} alwaysAllowTool="@notes/lookup" onAction={onAction} />)
    fireEvent.click(screen.getByRole('button', { name: /components.trustDropdown.always_allow_this_tool/ }))
    expect(onAction).toHaveBeenCalledWith('always_allow_tool', '@notes/lookup')
  })

  it('shows the refusal and does not fire when only a reason is given', () => {
    const onAction = vi.fn()
    render(<TrustDropdown fullCommand="" baseCommand="" isShell={false} hasCommand={false} showTrustAll={false} alwaysAllowRefusal="asks every time" onAction={onAction} />)
    const btn = screen.getByRole('button', { name: /always_allow_this_tool/ })
    expect(btn).toBeDisabled()
    expect(btn).toHaveTextContent('asks every time')
    fireEvent.click(btn)
    expect(onAction).not.toHaveBeenCalled()
  })
})
