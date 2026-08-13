/* The crews create sheet must not pre-fill the Agent Template field.
 *
 * It used to open with `kirocrew` already selected, so a crew created without
 * touching that field became an alias for the DEFAULT agent: dispatch flattens a
 * crew alias to its `kiro_agent` pointer, so the crew appeared in the chat
 * picker and then the default answered — reported as "the picker falls back to
 * the default" (#1684). These tests pin that the field starts UNSELECTED and
 * that Create refuses until it is chosen, so the broken row cannot be minted.
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

const mockCreate = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: 'kirocrew' }),
    agentsInstalled: vi.fn().mockResolvedValue([{ name: 'kirocrew' }, { name: 'reviewer' }]),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [{ name: 'default', dir: 'workspace' }] }),
    kirocrewConfig: vi.fn().mockResolvedValue({ memory_stores: { default: {} } }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: '' }),
    createKirocrewAgent: (...args: unknown[]) => mockCreate(...args),
    updateKirocrewAgent: vi.fn().mockResolvedValue({}),
    deleteKirocrewAgent: vi.fn().mockResolvedValue({}),
    setDefaultAgent: vi.fn().mockResolvedValue({}),
    createWorkspace: vi.fn().mockResolvedValue({}),
  },
}))

async function openCreateSheet() {
  renderWithProviders(<KiroCrewAgentsPage />)
  const newCrew = await screen.findByTestId('new-crew')
  fireEvent.click(newCrew)
  return newCrew
}

describe('crews create sheet — Agent Template must be explicit', () => {
  beforeEach(() => {
    mockCreate.mockReset()
    mockCreate.mockResolvedValue({ ok: true })
  })

  it('opens with the template unselected, showing the placeholder', async () => {
    await openCreateSheet()
    const trigger = await screen.findByRole('combobox', { name: 'Agent Template' })
    // The placeholder, NOT a pre-selected 'kirocrew'. Asserting the absence of
    // the old default is the actual regression: a trigger that reads 'kirocrew'
    // is the bug, however the placeholder happens to render.
    expect(trigger).toHaveTextContent('Select an agent template…')
    expect(trigger).not.toHaveTextContent('kirocrew')
  })

  it('refuses Create while no template is chosen, and does not call the API', async () => {
    await openCreateSheet()
    // Queried by placeholder: the Name field is labelled by its surrounding
    // <Field>, which does not wire htmlFor, so getByLabelText does not resolve it.
    fireEvent.change(await screen.findByPlaceholderText('e.g. oncall'), {
      target: { value: 'researcher' },
    })
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() =>
      expect(screen.getByText('Agent Template is required')).toBeInTheDocument(),
    )
    // The guard's whole point: no request is issued, so the server's own
    // refusal is a backstop rather than the only line of defence.
    expect(mockCreate).not.toHaveBeenCalled()
  })
})
