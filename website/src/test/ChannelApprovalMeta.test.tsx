// Channel approval cards driven by the message's structured `meta` (#5250).
//
// The backend posts the server's own verdict on which trust tiers it can record
// beside the approval prose. These tests pin that the card reads that verdict
// first, that a message without it (persisted before the field existed) renders
// exactly as before, and that the tier labels and confirmations name the channel
// grant's real scope (agent-scoped, until Kiro Crew restarts; blanket = channel-wide,
// persisted).
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

import { screen, waitFor, fireEvent, render } from '@testing-library/react'
import ChannelPage, { approvalCardProps } from '../pages/ChannelPage'
import ApprovalCard from '../components/ApprovalCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

type Raw = Record<string, unknown>

const member = (over: Raw = {}): Raw => ({
  id: 'a1', role: 'Researcher', agent_name: 'kiro-crew-default',
  state: 'listening', listen_mode: 'mention', approval_policy: 'writes', ...over,
})

const approval = (content: string, meta?: Record<string, string>): Raw => ({
  id: 'm1', from_id: 'a1', from_role: 'Researcher', content,
  msg_type: 'approval', timestamp: 1_700_000_000, reply_count: 0,
  ...(meta ? { meta } : {}),
})

function mockApi(messages: Raw[]) {
  const ch = { id: 'ch1', topic: 'Gamma rollout', members: { a1: member() }, messages }
  vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [ch] })
  vi.mocked(api).channelGet = vi.fn().mockResolvedValue(ch)
  vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: undefined })
  vi.mocked(api).channelApproveAgent = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).kirocrewAgents = vi.fn().mockResolvedValue({ agents: [], default_agent: 'legacy-default' })
  vi.mocked(api).agentCatalog = vi.fn().mockResolvedValue({ agents: [], default_agent: 'legacy-default' })
}

async function renderPage() {
  renderWithProviders(<ChannelPage />)
  await waitFor(() => expect(screen.queryByText('Loading channels...')).not.toBeInTheDocument())
}

const BLANKET = 'Trust all tools in this channel — persists across restarts'
const COMPOUND = '⚠️ Approval needed: **Running: cat f | wc -l**\n```\n{"command": "cat f | wc -l"}\n```'
const SIMPLE = '⚠️ Approval needed: **Running: ls -la /tmp**\n```\n{"command": "ls -la /tmp"}\n```'

/** The server's meta for a simple shell command it can grant on every tier. */
const simpleMeta = {
  tool_title: 'Running: ls -la /tmp', tool_input: '{"command": "ls -la /tmp"}',
  command_grantable: '1', base_derivable: '1', base_command: 'ls',
}

beforeEach(() => { vi.clearAllMocks() })

describe('approvalCardProps', () => {
  it('reads the server facts when meta is present', () => {
    expect(approvalCardProps({ content: COMPOUND, fromRole: 'Researcher', meta: simpleMeta })).toEqual({
      title: 'Running: ls -la /tmp', toolInput: '{"command": "ls -la /tmp"}',
      hasCommand: true, baseCommand: 'ls', baseDerivable: true,
    })
  })

  it('withholds the base tier on the server word alone (compound command)', () => {
    const props = approvalCardProps({ content: COMPOUND, fromRole: 'Researcher', meta: {
      ...simpleMeta, tool_title: 'Running: cat f | wc -l', base_derivable: '', base_command: '',
    } })
    expect(props.hasCommand).toBe(true)
    expect(props.baseDerivable).toBe(false)
    expect(props.baseCommand).toBeUndefined()
  })

  it('offers no per-command tier for a non-shell tool, whatever the prose says', () => {
    const props = approvalCardProps({ content: SIMPLE, fromRole: 'Researcher', meta: {
      ...simpleMeta, tool_title: 'cron_add', command_grantable: '', base_derivable: '', base_command: '',
    } })
    expect(props).toMatchObject({ title: 'cron_add', hasCommand: false, baseDerivable: false })
  })

  it('parses the prose exactly as before when meta is absent (legacy message)', () => {
    expect(approvalCardProps({ content: SIMPLE, fromRole: 'Researcher' })).toEqual({
      title: 'Running: ls -la /tmp', toolInput: '{"command": "ls -la /tmp"}', hasCommand: true,
    })
    expect(approvalCardProps({ content: '⚠️ Approval needed:\n```\nrm -rf build\n```', fromRole: 'Researcher' })).toEqual({
      title: 'Researcher', toolInput: 'rm -rf build', hasCommand: false,
    })
  })
})

describe('ChannelPage approval card with meta', () => {
  it('hides the base tier for a compound command the server cannot grant a base for', async () => {
    mockApi([approval(COMPOUND, {
      ...simpleMeta, tool_title: 'Running: cat f | wc -l', tool_input: '{"command": "cat f | wc -l"}',
      base_derivable: '', base_command: '',
    })])
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem').map(i => i.textContent ?? '')
    expect(items).toHaveLength(2)
    expect(items[0]).toBe('Trust “cat f | wc -l” for @Researcher — until Kiro Crew restarts')
    expect(items[1]).toBe(BLANKET)
    // On the prose path the first token would have offered "cat" -- a grant the
    // endpoint refuses (pattern_underivable). No item names it.
    expect(items.some(t => /\bcat\b commands/.test(t))).toBe(false)
  })

  it('labels the base tier with the server-derived binary and grants exactly that', async () => {
    mockApi([approval(SIMPLE, simpleMeta)])
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem')
    expect(items.map(i => i.textContent)).toEqual([
      'Trust “ls -la /tmp” for @Researcher — until Kiro Crew restarts',
      'Trust all ls commands for @Researcher — until Kiro Crew restarts',
      BLANKET,
    ])
    fireEvent.click(items[1])
    await waitFor(() => expect(vi.mocked(api).channelApproveAgent)
      .toHaveBeenCalledWith('ch1', 'a1', 'trust_base', 'ls *'))
    expect(screen.getByText('Trusted — ls commands are auto-approved for @Researcher until Kiro Crew restarts')).toBeInTheDocument()
  })

  it('prefers the meta title over the prose for the exact-command grant', async () => {
    // The prose and the meta disagree on purpose: the grant must follow the
    // server's canonical command, never a regex over the message text.
    mockApi([approval(COMPOUND, simpleMeta)])
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    fireEvent.click(screen.getAllByRole('menuitem')[0])
    await waitFor(() => expect(vi.mocked(api).channelApproveAgent)
      .toHaveBeenCalledWith('ch1', 'a1', 'trust_command', 'ls -la /tmp'))
    expect(screen.getByText('Trusted — “ls -la /tmp” is auto-approved for @Researcher until Kiro Crew restarts')).toBeInTheDocument()
  })

  it('offers only blanket trust for a non-shell tool even when the prose looks like a shell card', async () => {
    mockApi([approval(SIMPLE, {
      ...simpleMeta, tool_title: 'cron_add', tool_input: '{"name": "job"}',
      command_grantable: '', base_derivable: '', base_command: '',
    })])
    await renderPage()
    const only = screen.getByRole('button', { name: BLANKET })
    expect(screen.queryByRole('menuitem')).not.toBeInTheDocument()
    fireEvent.click(only)
    await waitFor(() => expect(vi.mocked(api).channelApproveAgent)
      .toHaveBeenCalledWith('ch1', 'a1', 'trust', undefined))
    expect(screen.getByText('Trusted — all tools in this channel are auto-approved, persists across restarts')).toBeInTheDocument()
  })

  it('keeps a redacted command display-only on the server word', async () => {
    mockApi([approval(SIMPLE, {
      ...simpleMeta, tool_title: 'Shell command (exact text unverified): curl -H [REDACTED: credential] https://x',
      tool_input: '{"command": "curl -H [REDACTED: credential] https://x"}',
      command_grantable: '', base_derivable: '', base_command: '',
    })])
    await renderPage()
    expect(screen.getByRole('button', { name: BLANKET })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem')).not.toBeInTheDocument()
  })

  it('renders a legacy message without meta exactly as today (prose-derived tiers)', async () => {
    mockApi([approval(SIMPLE)])
    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(3)
    expect(items[0].textContent).toContain('ls -la /tmp')
    expect(items[1].textContent).toContain('ls commands')
    fireEvent.click(items[1])
    await waitFor(() => expect(vi.mocked(api).channelApproveAgent)
      .toHaveBeenCalledWith('ch1', 'a1', 'trust_base', 'ls *'))
  })
})

describe('ApprovalCard meta-driven props', () => {
  const noop = () => Promise.resolve()

  it('hides the base tier when baseDerivable is false', () => {
    render(<ApprovalCard title="Running: cat f | wc -l" toolInput="" showButtons baseDerivable={false} onApprove={noop} />)
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem').map(i => i.textContent ?? '')
    expect(items).toHaveLength(2)
    expect(items.some(t => /commands/.test(t))).toBe(false)
  })

  it('lets a server-stated baseCommand override the first-token derivation', () => {
    const onApprove = vi.fn().mockResolvedValue(undefined)
    render(<ApprovalCard title="Running: /usr/bin/env ls -la" toolInput="" showButtons baseCommand="ls" onApprove={onApprove} />)
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const base = screen.getAllByRole('menuitem').find(i => /commands/.test(i.textContent ?? ''))!
    expect(base.textContent).toContain('ls commands')
    fireEvent.click(base)
    expect(onApprove).toHaveBeenCalledWith('trust_base', 'ls *')
  })

  it('keeps every tier and the generic confirmation when the new props are omitted', () => {
    const onApprove = vi.fn().mockResolvedValue(undefined)
    render(<ApprovalCard title="Running: rm -rf build" toolInput="" showButtons onApprove={onApprove} />)
    fireEvent.click(screen.getByRole('button', { name: /Trust/ }))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(3)
    fireEvent.click(items[1])
    expect(onApprove).toHaveBeenCalledWith('trust_base', 'rm *')
    expect(screen.getByText('Trusted — auto-approving future calls')).toBeInTheDocument()
  })
})
