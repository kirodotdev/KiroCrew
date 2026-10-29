/**
 * Create never asks for a template: `NewCrewmateDialog` offers none, and every
 * new mate starts on the built-in `kirocrew` agent as its starting binding (the
 * server gives it its own, source-free file at create).
 *
 * Creating with only a name issues the POST -- it is NOT blocked -- and sends
 * `kiro_agent: 'kirocrew'`. A restored required-template refusal (#1684) would
 * leave `createKirocrewAgent` uncalled and fail the first assertion; a dropped
 * `DEFAULT_CREWMATE_TEMPLATE` would fail the `kiro_agent` one.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { I18nextProvider } from 'react-i18next'
import i18n from '../i18n'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

// The dialog reads the execution catalog for its "Built from" list, the
// workspace list for its Advanced fold, and the roster for its post-failure
// reconcile. The create path here touches only the name, so a bare stub keeps
// the dialog mountable without a network; `createKirocrewAgent` resolves with
// the created name, as the server answers a 2xx.
const mockApi = vi.hoisted(() => ({
  agentCatalog: vi.fn(async () => ({ agents: [] })),
  workspaces: vi.fn(async () => ({ workspaces: [] })),
  members: vi.fn(async () => ({ members: [] })),
  createKirocrewAgent: vi.fn(async () => ({ name: 'researcher' })),
  createWorkspace: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../hooks/useAvailableModels', () => ({
  useAvailableModelsQuery: () => ({ data: [], isLoading: false }),
}))

import NewCrewmateDialog from '../pages/members/NewCrewmateDialog'

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={['/members']}>
      <QueryClientProvider client={qc}>
        <I18nextProvider i18n={i18n}>
          <NewCrewmateDialog open onClose={() => {}} onCreated={() => {}} existingNames={[]} />
        </I18nextProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const nameInput = () => screen.getByLabelText('Name') as HTMLInputElement

describe('NewCrewmateDialog — no template to pick; every mate starts on the built-in agent', () => {
  beforeEach(() => { mockApi.createKirocrewAgent.mockClear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('creates without touching Built from, defaulting kiro_agent to the built-in kirocrew agent', async () => {
    renderDialog()
    // Only a name is given; "Built from" is never opened.
    fireEvent.change(nameInput(), { target: { value: 'researcher' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))

    // The guard is retired: the request IS issued (not refused for a missing
    // template), and it carries the default agent as `kiro_agent`.
    await waitFor(() => expect(mockApi.createKirocrewAgent).toHaveBeenCalledTimes(1))
    expect(mockApi.createKirocrewAgent).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'researcher', kiro_agent: 'kirocrew' }),
    )
  })

  it('asks for a workspace of the mate\'s own unless Advanced names one to share', async () => {
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'researcher' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))

    // `''` is the create API's "allocate one of its own" value; a stored
    // workspace name would be the explicit opt-in to share one.
    await waitFor(() => expect(mockApi.createKirocrewAgent).toHaveBeenCalledTimes(1))
    expect(mockApi.createKirocrewAgent).toHaveBeenCalledWith(
      expect.objectContaining({ workspace: '' }),
    )
  })

  it('the card never names a template, folded or unfolded', async () => {
    renderDialog()
    const form = screen.getByTestId('crewmate-create-form')
    expect(form).not.toHaveTextContent(/kirocrew/i)
    fireEvent.click(screen.getByTestId('crewmate-create-advanced-toggle'))
    expect(await screen.findByRole('textbox', { name: 'Goal' })).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Built from' })).toBeNull()
    expect(form).not.toHaveTextContent(/kirocrew/i)
  })

  it('still refuses a BLANK name — client-side name validation is independent of the retired template guard', async () => {
    renderDialog()
    // No name typed: this is the one thing create still refuses, and it refuses
    // WITHOUT a request. Distinct from the old template guard, which this test's
    // sibling proves is gone.
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(mockApi.createKirocrewAgent).not.toHaveBeenCalled()
    expect(screen.getByTestId('crewmate-create-name-hint')).toBeInTheDocument()
  })
})
