import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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
})

/** Open the workspace StyledSelect dropdown in the create form and click
 *  the "+ New workspace…" action to open the modal. */
/** Locate the "Workspace" field group's StyledSelect trigger button.
 *  The field label is a <span> (it labels a StyledSelect, which has no native
 *  form control to associate a <label> with), so we scope by the enclosing
 *  ``.flex.flex-col`` group that actually contains a button trigger rather
 *  than by the label's tag name. */
function findWorkspaceTrigger(): HTMLElement {
  const wsLabels = screen.getAllByText('Workspace')
  for (const label of wsLabels) {
    const group = label.closest('.flex.flex-col') as HTMLElement | null
    const trigger = group?.querySelector('button')
    if (trigger) return trigger as HTMLElement
  }
  throw new Error('Workspace StyledSelect trigger not found')
}

async function openModalViaWorkspaceDropdown() {
  const wsTrigger = findWorkspaceTrigger()
  fireEvent.click(wsTrigger)
  // Now click the "+ New workspace…" action in the portal dropdown
  const newWsBtn = await screen.findByText('+ New workspace…')
  fireEvent.click(newWsBtn)
}

describe('WorkspaceModal — StyledSelect trigger and modal lifecycle', () => {
  it('workspace dropdown contains "+ New workspace…" action', async () => {
    renderPage()
    await waitFor(() => expect(mockApi.kirocrewAgents).toHaveBeenCalled())
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    // Open the workspace StyledSelect
    const wsTrigger = findWorkspaceTrigger()
    expect(wsTrigger).toBeTruthy()
    fireEvent.click(wsTrigger)
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

    // The "Copy from" StyledSelect shows "— none —" as placeholder
    const modal = screen.getByText('Create Workspace').closest('.fixed')!
    const copyTrigger = Array.from(modal.querySelectorAll('button[aria-haspopup="listbox"]'))
      .find(b => b.textContent?.includes('— none —'))
    expect(copyTrigger).toBeTruthy()
    fireEvent.click(copyTrigger!)

    // The portal dropdown should show workspace options
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'default' })).toBeInTheDocument()
      expect(screen.getByRole('option', { name: 'oncall' })).toBeInTheDocument()
    })
  })
})
