/**
 * Settings > Developer > Configuration: the "Run quick sub-agents inside the chat" switch.
 *
 * Stored as `agent.subagents` = "engine" (on) or "crew" (off). Only a KAS session
 * reads it, so on any other harness the row says the setting has no effect and
 * offers no switch that could read as on.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import KiroCrewCfgTab from '../pages/overview/KiroCrewCfgTab'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

const leaveGuard = vi.hoisted(() => vi.fn())
vi.mock('../components/SidePanelLayout', () => ({ useSidePanelLeaveGuard: leaveGuard }))

const LABEL = 'Run quick sub-agents inside the chat'
const DESCRIPTION = 'On: quick sub-agents finish faster and use much less memory. Off: each sub-agent runs as its own session that you can open and stop in the Subagents panel. Applies to your next chat.'

function cfg(agent: Record<string, unknown>) {
  return {
    agents: {},
    default_agent: '',
    workspaces: {},
    default_workspace: '',
    memory_stores: {},
    default_memory_store: '',
    agent: {
      default_agent: 'kirocrew',
      provider: 'acp',
      model: 'auto',
      approval_mode: 'auto',
      sandbox: 'auto',
      tool_search: true,
      max_channels: 7,
      max_channel_agents: 5,
      ...agent,
    },
    session: { timeout_secs: 3600, pool_size: 2, pool_agent: '', pool_ttl_secs: 600 },
    memory: { embedding_provider: 'e' },
    auto_update: true,
  }
}

function seed(body: unknown) {
  const m = vi.mocked(api)
  m.kirocrewConfig = vi.fn().mockResolvedValue(body)
  m.patchConfig = vi.fn().mockResolvedValue(body)
  m.themeBoot = vi.fn().mockResolvedValue({})
  return m
}

function rowFor(label: string): HTMLElement {
  const span = screen.getByText(
    (_content, el) => el?.tagName === 'SPAN' && el.firstChild?.nodeValue?.trim() === label,
  )
  // The row is the nearest ancestor that also holds the control or the locked note.
  return span.closest('.justify-between') as HTMLElement
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Run quick sub-agents inside the chat', () => {
  it('reads on by default on KAS and writes "crew" when switched off', async () => {
    const m = seed(cfg({ acp_backend: 'kas' }))
    renderWithProviders(<KiroCrewCfgTab />)
    const toggle = await waitFor(() =>
      within(rowFor(LABEL)).getByRole('button', { name: /^(on|off)$/ }),
    )
    expect(toggle).toHaveTextContent('on')
    // What On and Off do is on the row itself, not behind an info icon.
    expect(within(rowFor(LABEL)).getByText(DESCRIPTION)).toBeVisible()
    // A block of its own, so the label and the description never read as one glued run.
    expect(within(rowFor(LABEL)).getByText(DESCRIPTION).tagName).toBe('P')
    expect(within(rowFor(LABEL)).queryByRole('button', { name: 'More information' })).toBeNull()
    fireEvent.click(toggle)
    await waitFor(() => expect(m.patchConfig).toHaveBeenCalledWith('agent.subagents', 'crew'))
  })

  it('reads off on "crew" and writes "engine" when switched on', async () => {
    const m = seed(cfg({ acp_backend: '', subagents: 'crew' }))
    renderWithProviders(<KiroCrewCfgTab />)
    const toggle = await waitFor(() =>
      within(rowFor(LABEL)).getByRole('button', { name: /^(on|off)$/ }),
    )
    expect(toggle).toHaveTextContent('off')
    fireEvent.click(toggle)
    await waitFor(() => expect(m.patchConfig).toHaveBeenCalledWith('agent.subagents', 'engine'))
  })

  it('shows a refused save through the error notice', async () => {
    const m = seed(cfg({ acp_backend: 'kas' }))
    m.patchConfig = vi.fn().mockRejectedValue(new Error('agent.subagents is set in config.local.json'))
    renderWithProviders(<KiroCrewCfgTab />)
    const toggle = await waitFor(() =>
      within(rowFor(LABEL)).getByRole('button', { name: /^(on|off)$/ }),
    )
    fireEvent.click(toggle)
    const notice = await screen.findByTestId('cfg-save-error')
    expect(notice).toHaveTextContent('agent.subagents is set in config.local.json')
  })

  it.each(['kiro', 'claude'])('says it has no effect on %s and offers no switch', async (backend) => {
    seed(cfg({ acp_backend: backend, subagents: 'engine' }))
    renderWithProviders(<KiroCrewCfgTab />)
    const note = await screen.findByText('Works only when Settings → Agent Harness is KAS. It has no effect on this backend.')
    const row = note.parentElement as HTMLElement
    expect(row).toHaveTextContent(LABEL)
    expect(within(row).queryByRole('button', { name: /^(on|off)$/ })).toBeNull()
    expect(within(row).queryByText('on', { exact: true })).toBeNull()
  })
})
