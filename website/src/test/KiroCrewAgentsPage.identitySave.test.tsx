/* The crew editor's save carries a field only when THIS editor changed it --
 * the identity fields, and the binding, workspace, store, triggers, model,
 * effort and colour alike.
 *
 * The server writes exactly the keys the body carries. A save that echoed the
 * display name and role it loaded would silently revert a rename another
 * surface committed while this sheet was open -- a second tab, or the Crew
 * Members page's in-place rename -- and the losing surface would never know.
 * So an untouched name or role stays out of the body; an edited one rides,
 * including an edit that clears the label back to the id (the server stores
 * '' for that, which is a real rename, not an echo).
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

const mockApi = vi.hoisted(() => ({
  kirocrewAgents: vi.fn(),
  agentsInstalled: vi.fn(),
  workspaces: vi.fn(),
  kirocrewConfig: vi.fn(),
  agentResolvedModel: vi.fn(),
  createKirocrewAgent: vi.fn(),
  updateKirocrewAgent: vi.fn(),
  deleteKirocrewAgent: vi.fn(),
  setDefaultAgent: vi.fn(),
  createWorkspace: vi.fn(),
  crons: vi.fn(),
  webhooks: vi.fn(),
  models: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mockApi }))

const ONCALL = {
  name: 'oncall',
  display_name: 'Pager triage',
  role: 'Oncall triage',
  kiro_agent: 'kirocrew',
  workspace: 'default',
  memory_store: 'default',
  triggers: 'sev1',
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.kirocrewAgents.mockResolvedValue({ agents: [ONCALL], default_agent: 'kirocrew' })
  mockApi.agentsInstalled.mockResolvedValue([{ name: 'kirocrew' }])
  mockApi.workspaces.mockResolvedValue({ workspaces: [{ name: 'default', dir: 'workspace' }] })
  mockApi.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {} } })
  mockApi.agentResolvedModel.mockResolvedValue({ model: '' })
  mockApi.createKirocrewAgent.mockResolvedValue({ ok: true })
  mockApi.updateKirocrewAgent.mockResolvedValue({})
  mockApi.deleteKirocrewAgent.mockResolvedValue({})
  mockApi.setDefaultAgent.mockResolvedValue({})
  mockApi.createWorkspace.mockResolvedValue({})
  mockApi.crons.mockResolvedValue({ jobs: [] })
  mockApi.webhooks.mockResolvedValue({ tokens: [] })
  mockApi.models.mockResolvedValue([])
})

async function openEditor(): Promise<HTMLElement> {
  renderWithProviders(<KiroCrewAgentsPage />)
  fireEvent.click(await screen.findByTestId('crew-card'))
  return await screen.findByRole('dialog', { name: /Edit agent oncall/ })
}

async function savedBody(sheet: HTMLElement): Promise<Record<string, unknown>> {
  fireEvent.click(within(sheet).getByText('Save changes'))
  await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
  const [name, body] = mockApi.updateKirocrewAgent.mock.calls[0]
  expect(name).toBe('oncall')
  return body as Record<string, unknown>
}

describe('crew editor — identity fields ride only when edited', () => {
  it('a save that touched another field does not echo the name or role back', async () => {
    const sheet = await openEditor()
    fireEvent.click(within(sheet).getByTestId('crew-rail-routing'))
    const triggers = within(sheet).getByRole('textbox', { name: 'Triggers' })
    fireEvent.change(triggers, { target: { value: 'sev1, sev2' } })
    const body = await savedBody(sheet)
    expect(body.triggers).toBe('sev1, sev2')
    expect('display_name' in body).toBe(false)
    expect('role' in body).toBe(false)
  })

  it('a rename does not carry the binding fields, so a stale tab cannot revert another tab\'s rebind', async () => {
    const sheet = await openEditor()
    fireEvent.change(within(sheet).getByTestId('crew-display-name-input'), {
      target: { value: 'Pager triage (EU)' },
    })
    const body = await savedBody(sheet)
    expect(body.display_name).toBe('Pager triage (EU)')
    for (const key of ['kiro_agent', 'workspace', 'memory_store', 'triggers', 'model', 'reasoning_effort', 'session_color']) {
      expect(key in body).toBe(false)
    }
    // The face still rides in its "nothing to say" spelling, so the body is
    // never a binding-only payload by accident.
    expect(body.avatar).toEqual({})
  })

  it('an edited display name rides alone; the untouched role stays out', async () => {
    const sheet = await openEditor()
    fireEvent.change(within(sheet).getByTestId('crew-display-name-input'), {
      target: { value: 'Pager triage (EU)' },
    })
    const body = await savedBody(sheet)
    expect(body.display_name).toBe('Pager triage (EU)')
    expect('role' in body).toBe(false)
  })

  it('an edited role rides alone; the untouched name stays out', async () => {
    const sheet = await openEditor()
    fireEvent.change(within(sheet).getByTestId('crew-role-input'), {
      target: { value: 'Head of triage' },
    })
    const body = await savedBody(sheet)
    expect(body.role).toBe('Head of triage')
    expect('display_name' in body).toBe(false)
  })

  it('clearing the label back to the id is a real rename and rides as the id', async () => {
    const sheet = await openEditor()
    fireEvent.change(within(sheet).getByTestId('crew-display-name-input'), {
      target: { value: '   ' },
    })
    const body = await savedBody(sheet)
    expect(body.display_name).toBe('oncall')
  })
})
