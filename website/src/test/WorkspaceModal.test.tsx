import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'

/* ── Mock api client ── */
const mockApi = vi.hoisted(() => ({
  kirocrewAgents: vi.fn(),
  agentsInstalled: vi.fn(),
  workspaces: vi.fn(),
  kirocrewConfig: vi.fn(),
  createWorkspace: vi.fn(),
  createKirocrewAgent: vi.fn(),
  updateKirocrewAgent: vi.fn(),
  deleteKirocrewAgent: vi.fn(),
  agentResolvedModel: vi.fn(),
  setDefaultAgent: vi.fn(),
  createChatSlot: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mockApi }))

import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

function createTestStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
}

function renderPage() {
  const store = createTestStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter>
          <KiroCrewAgentsPage />
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

const AGENTS_RESPONSE = {
  agents: [{ name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' }],
  default_agent: 'kirocrew',
}
const WORKSPACES_RESPONSE = { workspaces: [{ name: 'default', dir: 'workspace' }, { name: 'oncall', dir: 'workspace-oncall' }] }
const INSTALLED_RESPONSE = [{ name: 'kirocrew' }]
const CONFIG_RESPONSE = { memory_stores: { default: {} } }

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.kirocrewAgents.mockResolvedValue(AGENTS_RESPONSE)
  mockApi.agentsInstalled.mockResolvedValue(INSTALLED_RESPONSE)
  mockApi.workspaces.mockResolvedValue(WORKSPACES_RESPONSE)
  mockApi.kirocrewConfig.mockResolvedValue(CONFIG_RESPONSE)
  mockApi.agentResolvedModel.mockResolvedValue({ model: '', pinned: false, kiro_agent: 'kirocrew' })
})

/** Open the crew editor panel. The workspace picker lives inside it now, so
 *  every workspace-modal path goes through here first. */
async function openCrewSheet(): Promise<HTMLElement> {
  fireEvent.click(screen.getByTestId('new-crew'))
  return await screen.findByRole('dialog', { name: 'Create a new crew' })
}

/** Open the workspace select inside the editor panel and click the
 *  "+ New workspace…" action row to open the modal. The trigger is found by
 *  its accessible name — SimpleSelect forwards aria-label onto the combobox —
 *  rather than by DOM structure, so restyling the panel cannot break this. */
async function openModalViaWorkspaceDropdown() {
  const sheet = await openCrewSheet()
  fireEvent.click(within(sheet).getByRole('combobox', { name: 'Workspace' }))
  fireEvent.click(await screen.findByText('+ New workspace…'))
}

describe('WorkspaceModal — StyledSelect trigger and modal lifecycle', () => {
  it('workspace dropdown contains "+ New workspace…" action', async () => {
    renderPage()
    await waitFor(() => expect(mockApi.kirocrewAgents).toHaveBeenCalled())
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    const sheet = await openCrewSheet()
    fireEvent.click(within(sheet).getByRole('combobox', { name: 'Workspace' }))
    expect(await screen.findByText('+ New workspace…')).toBeInTheDocument()
  })

  it('opens modal when "+ New workspace…" is clicked', async () => {
    renderPage()
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    await openModalViaWorkspaceDropdown()
    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
  })

  it('closes modal on Escape key', async () => {
    renderPage()
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    await openModalViaWorkspaceDropdown()
    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('Create Workspace')).not.toBeInTheDocument())
  })

  it('closes modal on backdrop click', async () => {
    renderPage()
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    await openModalViaWorkspaceDropdown()
    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
    // The dismiss backdrop is an accessible role="button" labelled "Close dialog"
    // (a Clickable behind the dialog), not the outer positioning wrapper.
    const backdrop = screen.getByRole('button', { name: 'Close dialog' })
    fireEvent.click(backdrop)
    await waitFor(() => expect(screen.queryByText('Create Workspace')).not.toBeInTheDocument())
  })
})

describe('WorkspaceModal — creation flow', () => {
  it('calls api.createWorkspace() on submit', async () => {
    mockApi.createWorkspace.mockResolvedValue({ ok: true, name: 'staging' })
    renderPage()
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    await openModalViaWorkspaceDropdown()

    const modal = screen.getByText('Create Workspace').closest('.fixed')!
    const nameInput = modal.querySelector('input[placeholder="e.g. oncall"]') as HTMLInputElement
    expect(nameInput).toBeTruthy()
    const user = userEvent.setup()
    await user.type(nameInput, 'staging')

    const buttons = modal.querySelectorAll('button')
    const createBtn = Array.from(buttons).find(b => b.textContent === 'Create')!
    fireEvent.click(createBtn)
    await waitFor(() => {
      expect(mockApi.createWorkspace).toHaveBeenCalledWith({ name: 'staging', dir: 'workspace-staging' })
    })
    await waitFor(() => expect(screen.queryByText('Create Workspace')).not.toBeInTheDocument())
  })

  it('displays error on creation failure', async () => {
    mockApi.createWorkspace.mockRejectedValue(new Error('Workspace already exists'))
    renderPage()
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    await openModalViaWorkspaceDropdown()

    const modal = screen.getByText('Create Workspace').closest('.fixed')!
    const nameInput = modal.querySelector('input[placeholder="e.g. oncall"]') as HTMLInputElement
    fireEvent.change(nameInput, { target: { value: 'default' } })

    const buttons = modal.querySelectorAll('button')
    const createBtn = Array.from(buttons).find(b => b.textContent === 'Create')!
    fireEvent.click(createBtn)

    await waitFor(() => {
      expect(screen.getByText('Workspace already exists')).toBeInTheDocument()
    })
    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
  })

  it('"Copy from" StyledSelect shows existing workspaces', async () => {
    renderPage()
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    await openModalViaWorkspaceDropdown()

    // The "Copy from" select shows "— none —" as its clear row / trigger text
    const modal = screen.getByText('Create Workspace').closest('.fixed')! as HTMLElement
    const copyTrigger = within(modal).getByRole('combobox', { name: 'Copy from workspace' })
    expect(copyTrigger).toHaveTextContent('— none —')
    fireEvent.click(copyTrigger)

    // The portal dropdown should show workspace options
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'default' })).toBeInTheDocument()
      expect(screen.getByRole('option', { name: 'oncall' })).toBeInTheDocument()
    })
  })
})
