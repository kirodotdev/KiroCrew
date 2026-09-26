/* The crew editor's Perpetual mode refusal offers the same "Ask the agent"
 * hand-off the Crewmates page side panel offers -- under the sheet's own
 * "nothing at stake" test. The hand-off navigates to /chat and unmounts the
 * sheet, so it is offered only while no pane holds unsaved work; an open,
 * typed schedule draft withholds it (ErrorNotice's opt-in contract: forgetting
 * to withhold would mean silent draft loss, forgetting to offer costs a button).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const REFUSED = Object.assign(new Error('HTTP 409'), {
  status: 409,
  body: JSON.stringify({ error: 'structured monitor', code: 'structured_monitor_not_convertible' }),
})

vi.mock('../api/client', () => ({
  api: {
    kirocrewAgents: vi.fn().mockResolvedValue({
      agents: [{ name: 'oncall', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' }],
      default_agent: 'kirocrew',
    }),
    agentsInstalled: vi.fn().mockResolvedValue([{ name: 'kirocrew' }]),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [{ name: 'default', dir: 'workspace' }] }),
    kirocrewConfig: vi.fn().mockResolvedValue({ memory_stores: { default: {} } }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: '' }),
    createKirocrewAgent: vi.fn().mockResolvedValue({ ok: true }),
    updateKirocrewAgent: vi.fn().mockResolvedValue({}),
    deleteKirocrewAgent: vi.fn().mockResolvedValue({}),
    setDefaultAgent: vi.fn().mockResolvedValue({}),
    createWorkspace: vi.fn().mockResolvedValue({}),
    crons: vi.fn().mockResolvedValue({ jobs: [] }),
    webhooks: vi.fn().mockResolvedValue({ tokens: [] }),
    models: vi.fn().mockResolvedValue([]),
    createCron: vi.fn().mockResolvedValue({}),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cancelCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    // The Perpetual mode section's two reads and its one write.
    members: vi.fn().mockResolvedValue({
      members: [{ name: 'oncall', slug: 'oncall', slot_key: 'member-oncall', running: false }],
      default_agent: 'kirocrew',
    }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: true, loops: [] }),
    memberPerpetualSet: vi.fn().mockRejectedValue(REFUSED),
  },
}))

async function openSchedules() {
  renderWithProviders(<KiroCrewAgentsPage />)
  fireEvent.click(await screen.findByTestId('crew-card'))
  fireEvent.click(await screen.findByTestId('crew-rail-schedules'))
  await screen.findByTestId('crew-perpetual-reason')
}

const perpetualSwitch = () =>
  within(screen.getByTestId('crew-perpetual-switch')).getByRole('switch')

beforeEach(() => vi.clearAllMocks())

describe('crew editor — Perpetual mode refusal hand-off', () => {
  it('with nothing at stake, a refused press offers "Ask the agent", as the side panel does', async () => {
    await openSchedules()
    fireEvent.click(perpetualSwitch())
    const notice = await screen.findByTestId('crew-perpetual-error')
    expect(notice).toHaveTextContent(/stop the task there first/i)
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeTruthy()
    // The switch stays where the backend is.
    expect(perpetualSwitch()).toHaveAttribute('aria-checked', 'false')
  })

  it('withholds the hand-off while a typed schedule draft is open on the same pane', async () => {
    await openSchedules()
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    await screen.findByTestId('crew-wake-create')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    await waitFor(() => expect(screen.getByTestId('crew-rail-dirty-schedules')).toBeTruthy())
    fireEvent.click(perpetualSwitch())
    const notice = await screen.findByTestId('crew-perpetual-error')
    // The refusal still reads in full; only the navigating action is withheld.
    expect(notice).toHaveTextContent(/stop the task there first/i)
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
    // The draft is intact: nothing navigated.
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('draft')
  })
})
