/* Private fork copies are not offered as bindable templates.
 *
 * A crew's own copy (blueprint semantics: `private_to` set to that crew) is
 * named after the crew it belongs to, so it means nothing in another crew's
 * Agent Template dropdown. `kiroAgentOptions` filters those rows out of the
 * shared catalog. This pins that the create sheet's dropdown offers the shared
 * templates and hides a private copy — otherwise one crew's fork would leak
 * into every crew's binding list.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

vi.mock('../api/client', () => ({
  api: {
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: 'kirocrew' }),
    // A plain array, one of whose rows is a crew's private fork copy.
    agentsInstalled: vi.fn().mockResolvedValue([
      { name: 'kirocrew' },
      { name: 'reviewer' },
      { name: 'atlas-crewA', private_to: 'crewA', forked_from: 'atlas' },
    ]),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [{ name: 'default', dir: 'workspace' }] }),
    kirocrewConfig: vi.fn().mockResolvedValue({ memory_stores: { default: {} } }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: '' }),
    createKirocrewAgent: vi.fn().mockResolvedValue({ ok: true }),
    updateKirocrewAgent: vi.fn().mockResolvedValue({}),
    deleteKirocrewAgent: vi.fn().mockResolvedValue({}),
    setDefaultAgent: vi.fn().mockResolvedValue({}),
    createWorkspace: vi.fn().mockResolvedValue({}),
  },
}))

async function openTemplateDropdown() {
  renderWithProviders(<KiroCrewAgentsPage />)
  fireEvent.click(await screen.findByTestId('new-crew'))
  const trigger = await screen.findByRole('combobox', { name: 'Agent Template' })
  fireEvent.click(trigger)
}

describe('Agent Template dropdown — private fork copies excluded', () => {
  beforeEach(() => vi.clearAllMocks())

  it('offers the shared templates but not a crew\'s private copy', async () => {
    await openTemplateDropdown()
    // The shared, bindable templates are offered.
    await waitFor(() => expect(screen.getByRole('option', { name: /^kirocrew/ })).toBeInTheDocument())
    expect(screen.getByRole('option', { name: /^reviewer/ })).toBeInTheDocument()
    // The private copy (private_to set) is filtered out of the catalog.
    expect(screen.queryByRole('option', { name: /atlas-crewA/ })).not.toBeInTheDocument()
  })
})
