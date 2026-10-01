/* The crew editor's Perpetual mode refusal carries NO agent hand-off, the same
 * as the Crewmates page side panel. `ErrorNotice`'s generic hand-off opens a
 * NEW /chat, which is not this crewmate's chat, so a button there would lie and
 * would unmount the sheet on the way. The remedy the refusal names (open its
 * chat, stop the task there) is the direct link already in the fact list
 * above: one exact "Open its chat" on the page, addressed to this crewmate,
 * opening in a new tab so nothing on the sheet is lost -- a typed schedule
 * draft included.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'
import { api } from '../api/client'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

// vi.mock factories are hoisted above every import and const in this module, so
// the rejection they reference must be hoisted alongside them.
const REFUSED = vi.hoisted(() =>
  Object.assign(new Error('HTTP 409'), {
    status: 409,
    body: JSON.stringify({ error: 'structured monitor', code: 'structured_monitor_not_convertible' }),
  }),
)

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

const directChatLink = () => {
  // Exact match: the refusal sentence also says "Open its chat", so only the
  // fact list's link -- the one truthful action -- matches the whole string.
  const links = screen.getAllByText('Open its chat')
  expect(links).toHaveLength(1)
  const link = links[0]
  expect(link).toBe(screen.getByTestId('crew-perpetual-review-chat'))
  expect(link.tagName).toBe('A')
  expect(link).toHaveAttribute('href', '/members?member=oncall')
  expect(link).toHaveAttribute('target', '_blank')
}

describe('crew editor — Perpetual mode refusal has no generic hand-off', () => {
  it('a refused press names the remedy and leaves the direct crewmate-chat link as its one action', async () => {
    await openSchedules()
    expect(screen.getByTestId('crew-perpetual-save-split')).toBeTruthy()
    const saveChanges = screen.getByRole('button', { name: 'Save changes' })
    expect(saveChanges).toBeDisabled()
    fireEvent.click(perpetualSwitch())
    const notice = await screen.findByTestId('crew-perpetual-error')
    expect(notice).toHaveTextContent(/stop the task there first/i)
    // No button inside the notice: no `ErrorNotice` hand-off to a new /chat.
    expect(within(notice).queryByRole('button')).toBeNull()
    expect(notice.querySelector('button')).toBeNull()
    directChatLink()
    // The switch stays where the backend is.
    expect(perpetualSwitch()).toHaveAttribute('aria-checked', 'false')
    expect(saveChanges).toBeDisabled()
  })

  it('withholds the switch footer when this install cannot offer the switch', async () => {
    vi.mocked(api.autonudgeList).mockResolvedValueOnce({ enabled: false, loops: [] })
    await openSchedules()
    expect(screen.queryByTestId('crew-perpetual-switch')).toBeNull()
    expect(screen.queryByTestId('crew-perpetual-save-split')).toBeNull()
  })

  it('a refused press while a typed schedule draft is open leaves the draft intact', async () => {
    await openSchedules()
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    await screen.findByTestId('crew-wake-create')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    await waitFor(() => expect(screen.getByTestId('crew-rail-dirty-schedules')).toBeTruthy())
    fireEvent.click(perpetualSwitch())
    const notice = await screen.findByTestId('crew-perpetual-error')
    // The refusal reads in full, and still carries no navigating button.
    expect(notice).toHaveTextContent(/stop the task there first/i)
    expect(within(notice).queryByRole('button')).toBeNull()
    // The direct link remains available: it opens a new tab, so following it
    // would not unmount the sheet or the draft.
    directChatLink()
    // The draft is intact: nothing navigated.
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('draft')
  })
})
