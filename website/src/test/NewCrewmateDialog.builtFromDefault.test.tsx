/**
 * "Built from" is a DEFAULT, not a required pick — the shipped behaviour after
 * the two create doors converged on `NewCrewmateDialog`.
 *
 * Before this, create (through the crew manager's editor sheet) opened with no
 * template selected and REFUSED to submit until one was chosen — the #1684
 * guard, pinned by the now-deleted `KiroCrewAgentsPage.templateRequired.test.tsx`.
 * The accepted `rfc-crewmates-launch.md` rules the New crewmate dialog's
 * **Built from** field is "the default agent or a custom agent" — a default,
 * not a required pick. So converging both doors on this dialog retires that
 * guard: a crew created without touching **Built from** is built from the
 * default `kirocrew` agent, exactly as the Crewmates page already behaved.
 *
 * This pins that shipped behaviour (the positive inverse of the deleted guard):
 * creating with only a name issues the POST — it is NOT blocked — and sends
 * `kiro_agent: 'kirocrew'`. If someone restored a required-template refusal in
 * the dialog, `createKirocrewAgent` would never be called and the first
 * assertion fails; if the `builtFrom || BUILTIN_AGENT` fallback were dropped,
 * the `kiro_agent` assertion fails.
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

describe('NewCrewmateDialog — Built from is a default, not a required pick', () => {
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
