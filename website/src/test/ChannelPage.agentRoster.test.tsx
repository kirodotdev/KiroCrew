/**
 * Channels' Add Agent picker reports a failed roster load too (#5990).
 *
 * `ChannelPage`'s `AddAgentForm` calls `useAgents(0)` with the same constant
 * trigger `SchedulePage` did, so it had the same defect for the same reason: a
 * rejected `/api/agents` left the roster empty, the picker captioned it "No
 * matches", and nothing re-fetched for the life of the mount. Two reviewers
 * flagged it as the one remaining `AgentSelector` call site with that shape, so
 * it is wired here rather than deferred -- and pinned, because the wiring is
 * three props that a refactor could silently drop.
 *
 * Mock conventions follow `ChannelPageCoverage.test.tsx`: the api client is the
 * single seam, auto-mocked, with every method this page touches pointed at a
 * resolved value -- except `kirocrewAgents`, which is the failure under test.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})

type Raw = Record<string, unknown>

const member = (over: Raw = {}): Raw => ({
  id: 'a1', role: 'Researcher', agent_name: 'kirocrew',
  state: 'listening', listen_mode: 'mention', approval_policy: 'writes', ...over,
})

const channel = (over: Raw = {}): Raw => ({
  id: 'ch1', topic: 'Gamma rollout', members: { a1: member() }, messages: [], ...over,
})

const ROSTER = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'paging', source: 'package' },
]

beforeEach(() => {
  vi.clearAllMocks()
  const channels = [channel()]
  vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels })
  vi.mocked(api).channelGet = vi.fn().mockResolvedValue(channels[0])
  vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({})
  vi.mocked(api).channelAddAgent = vi.fn().mockResolvedValue({ ok: true })
  vi.mocked(api).syncKirocrewAgents = vi.fn().mockResolvedValue({ ok: true })
  // The failure under test: the roster endpoint is down.
  vi.mocked(api).kirocrewAgents = vi.fn().mockRejectedValue(new Error('gateway restarting'))
})

/** Render, wait past the loading gate, then open the agents sidebar's add form. */
async function openAddAgentPicker() {
  renderWithProviders(<ChannelPage />)
  await waitFor(() => expect(screen.queryByText('Loading channels...')).not.toBeInTheDocument())
  await userEvent.click(await screen.findByRole('button', { name: '1 agent' }))
  await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
  await userEvent.click(await screen.findByLabelText('Switch agent'))
}

describe('ChannelPage Add Agent roster failure (#5990)', () => {
  it('names the failed roster load and offers a retry', async () => {
    await openAddAgentPicker()

    await waitFor(() => {
      expect(screen.getByText("Couldn't load the agent list.")).toBeInTheDocument()
    })
    // The misleading empty-state that made this indistinguishable from a
    // one-agent install must be gone here too, not just on the schedule form.
    expect(screen.queryByText('No matches')).not.toBeInTheDocument()
    expect(screen.getByText('Retry')).toBeInTheDocument()
  })

  it('recovers the roster when the retry succeeds', async () => {
    await openAddAgentPicker()
    await waitFor(() => expect(screen.getByText('Retry')).toBeInTheDocument())

    vi.mocked(api).kirocrewAgents = vi.fn().mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
    fireEvent.click(screen.getByText('Retry'))

    // Constant trigger, so before this change there was no path back at all.
    await waitFor(() => expect(screen.getByText('oncall')).toBeInTheDocument())
    expect(screen.queryByText("Couldn't load the agent list.")).not.toBeInTheDocument()
  })
})
