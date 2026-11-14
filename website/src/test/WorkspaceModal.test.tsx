import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
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
  agentCatalog: vi.fn(),
  members: vi.fn(),
  models: vi.fn(),
  agentResolvedModel: vi.fn(),
  setDefaultAgent: vi.fn(),
  createChatSlot: vi.fn(),
}))

/* SimpleSelect is stubbed for the same reason as in CrewEditorSelect.test.tsx:
   reaching this modal means driving a Radix Select from inside a Radix Dialog,
   and Radix commits discrete events with `ReactDOM.flushSync(...)`, which React
   refuses inside Testing Library's `act()` ("Should not already be working").
   The select is only the DOOR to the modal here — the modal's own lifecycle is
   what these tests are about. The real Radix path is driven by
   scripts/verify-crews-dialog-select.mjs, which runs on demand only
   (`npm run verify:crews-dialog-select`); no CI lane runs it. */
vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options, value, onChange, action, clearLabel, 'aria-label': ariaLabel,
  }: {
    options: string[]
    value: string
    onChange: (v: string) => void
    action?: { label: string; onSelect: () => void }
    clearLabel?: string
    'aria-label'?: string
  }) => {
    /* `role="combobox"` owes ARIA an `aria-controls`, so the option rows sit in a real
       listbox the trigger names — this stub exists to keep the accessible surface the
       tests query. The id is derived from the label because the sheet's select and the
       modal's "Copy from" select are mounted at the same time. The action row stays
       OUTSIDE the listbox: it is not an option. */
    const listboxId = `zzq-listbox-${(ariaLabel ?? 'unlabelled').replace(/\W+/g, '-')}`
    return (
      <div>
        <button type="button" role="combobox" aria-label={ariaLabel} aria-expanded={false} aria-controls={listboxId}>{value || clearLabel}</button>
        <div role="listbox" id={listboxId}>
          {clearLabel && (
            <button type="button" role="option" aria-selected={value === ''} onClick={() => onChange('')}>{clearLabel}</button>
          )}
          {options.map(o => (
            <button key={o} type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>{o}</button>
          ))}
        </div>
        {action && <button type="button" onClick={action.onSelect}>{action.label}</button>}
      </div>
    )
  },
}))

vi.mock('../api/client', () => ({ api: mockApi }))

import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'
import { WorkspaceModal } from '../components/crew/crewFields'

function createTestStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
}

function renderPage(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const store = createTestStore()
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
  mockApi.agentCatalog.mockResolvedValue({ agents: [{ name: 'kirocrew', selection_kind: 'template' }] })
  mockApi.members.mockResolvedValue({ members: [] })
  mockApi.models.mockResolvedValue([])
})

/** Open the create dialog. The workspace picker lives inside its Advanced
 *  settings, so every workspace-modal path goes through here first. */
async function openCrewSheet(): Promise<HTMLElement> {
  fireEvent.click(screen.getByTestId('new-crew'))
  const dlg = await screen.findByRole('dialog', { name: 'New bot' })
  // The crew manager's door opens with Advanced settings already unfolded.
  expect(within(dlg).getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'true')
  await within(dlg).findByRole('combobox', { name: 'Workspace' })
  return dlg
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
    await screen.findByTestId('crew-card')
    const sheet = await openCrewSheet()
    fireEvent.click(within(sheet).getByRole('combobox', { name: 'Workspace' }))
    expect(await screen.findByText('+ New workspace…')).toBeInTheDocument()
  })

  it('opens modal when "+ New workspace…" is clicked', async () => {
    renderPage()
    await screen.findByTestId('crew-card')
    await openModalViaWorkspaceDropdown()
    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
  })

  it('closes modal on Escape key', async () => {
    renderPage()
    await screen.findByTestId('crew-card')
    await openModalViaWorkspaceDropdown()
    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('Create Workspace')).not.toBeInTheDocument())
  })

  it('closes modal from its close button', async () => {
    renderPage()
    await screen.findByTestId('crew-card')
    await openModalViaWorkspaceDropdown()
    expect(screen.getByText('Create Workspace')).toBeInTheDocument()
    // Radix supplies the overlay and dismisses on an outside POINTERDOWN, so the
    // old hand-rolled `Clickable` backdrop (a role="button" named "Close dialog")
    // no longer exists. The explicit close is asserted here; outside-click
    // dismissal is a Radix behaviour, exercised in the browser script.
    const modal = screen.getByRole('dialog', { name: 'Create Workspace' })
    fireEvent.click(within(modal).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByText('Create Workspace')).not.toBeInTheDocument())
  })
})

describe('WorkspaceModal — creation flow', () => {
  it('calls api.createWorkspace() on submit', async () => {
    mockApi.createWorkspace.mockResolvedValue({ ok: true, name: 'staging' })
    renderPage()
    await screen.findByTestId('crew-card')
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
    await screen.findByTestId('crew-card')
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

  // A copy whose source folder is gone still creates the (empty) workspace, and the
  // server says so in `warnings`. The dialog must show that line instead of closing
  // as if the copy happened, and whichever way it is left, the new workspace is
  // still the one the form picks up.
  const exits: [string, (modal: HTMLElement) => void][] = [
    ['Done', modal => fireEvent.click(within(modal).getByRole('button', { name: 'Done' }))],
    ['the close button', modal => fireEvent.click(within(modal).getByRole('button', { name: 'Close' }))],
    ['Escape', () => fireEvent.keyDown(document, { key: 'Escape' })],
  ]
  it.each(exits)('shows the copy warning, then %s picks the new workspace', async (_, leave) => {
    const warning = "source workspace 'oncall' has no folder at 'workspace-oncall'; nothing was copied"
    // Answered inside act(): the modal learns of the warned create from the form's
    // effects, and an exit taken before they flush reads as a cancel.
    let finish: (v: unknown) => void = () => {}
    mockApi.createWorkspace.mockReturnValue(new Promise(r => { finish = r }))
    renderPage()
    await screen.findByTestId('crew-card')
    await openModalViaWorkspaceDropdown()

    const modal = screen.getByRole('dialog', { name: 'Create Workspace' })
    fireEvent.change(within(modal).getByPlaceholderText('e.g. oncall'), { target: { value: 'copied' } })
    fireEvent.click(await within(modal).findByRole('option', { name: 'oncall' }))
    fireEvent.click(within(modal).getByRole('button', { name: 'Create' }))
    await act(async () => { finish({ ok: true, name: 'copied', warnings: [warning] }) })

    expect(within(modal).getByText(warning)).toBeInTheDocument()
    expect(mockApi.createWorkspace).toHaveBeenCalledWith({ name: 'copied', dir: 'workspace-copied', copy_from: 'oncall' })
    leave(modal)
    await waitFor(() => expect(screen.queryByText('Create Workspace')).not.toBeInTheDocument())
    const sheet = screen.getByRole('dialog', { name: 'New bot' })
    await waitFor(() => expect(within(sheet).getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('copied'))
  })

  // A page that goes away under the warning (a navigation) still hears about the
  // workspace, and a create reported by Done is never reported again at teardown.
  it.each([false, true])('reports a warned create once when the page unmounts (Done first: %s)', async (doneFirst) => {
    mockApi.createWorkspace.mockResolvedValue({ ok: true, name: 'copied', warnings: ['nothing was copied'] })
    const onCreated = vi.fn()
    const modal = (open: boolean) => <WorkspaceModal open={open} workspaceOptions={[]} onCreated={onCreated} onClose={() => {}} />
    const { rerender, unmount } = render(modal(true))
    fireEvent.change(screen.getByPlaceholderText('e.g. oncall'), { target: { value: 'copied' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await screen.findByText('nothing was copied')
    expect(onCreated).not.toHaveBeenCalled()
    if (doneFirst) {
      fireEvent.click(screen.getByRole('button', { name: 'Done' }))
      rerender(modal(false))
    }
    unmount()
    expect(onCreated).toHaveBeenCalledTimes(1)
    expect(onCreated).toHaveBeenCalledWith('copied')
  })

  // Leaving the page under the warning retires the dialog's generation before the
  // form reports the create. The pick is dropped then, but the workspace exists, so
  // the list is marked stale and re-read on reopen: the app's cache never goes stale
  // on its own.
  it('a warned create left by navigation is in the picker when New bot reopens', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    let finish: (v: unknown) => void = () => {}
    mockApi.createWorkspace.mockReturnValue(new Promise(r => { finish = r }))
    const { unmount } = renderPage(qc)
    await screen.findByTestId('crew-card')
    await openModalViaWorkspaceDropdown()

    const modal = screen.getByRole('dialog', { name: 'Create Workspace' })
    fireEvent.change(within(modal).getByPlaceholderText('e.g. oncall'), { target: { value: 'copied' } })
    fireEvent.click(within(modal).getByRole('button', { name: 'Create' }))
    mockApi.workspaces.mockResolvedValue({ workspaces: [...WORKSPACES_RESPONSE.workspaces, { name: 'copied', dir: 'workspace-copied' }] })
    await act(async () => { finish({ ok: true, name: 'copied', warnings: ['nothing was copied'] }) })
    expect(within(modal).getByText('nothing was copied')).toBeInTheDocument()
    unmount()

    renderPage(qc)
    await screen.findByTestId('crew-card')
    const sheet = await openCrewSheet()
    expect(await within(sheet).findByRole('option', { name: 'copied' })).toBeInTheDocument()
  })

  it('"Copy from" StyledSelect shows existing workspaces', async () => {
    renderPage()
    await screen.findByTestId('crew-card')
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
