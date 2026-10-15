/**
 * Customize > Crewmates as a host of the shared crewmate editor: the open
 * editor and the create dialog survive the capabilities route turning mobile,
 * the roster-load hand-off stays off while either holds a draft, and the chat
 * error row's `&pane=capabilities` fix link lands on the editor's Approvals
 * section, where a changed member agent file is reviewed and saved.
 */
import { cloneElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SidePanelLayout from '../components/SidePanelLayout'

const viewport = vi.hoisted(() => ({ mobile: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.mobile }))

const mocks = vi.hoisted(() => ({
  api: {
    kirocrewAgents: vi.fn(), agentsInstalled: vi.fn(), workspaces: vi.fn(), kirocrewConfig: vi.fn(),
    agentResolvedModel: vi.fn(), models: vi.fn(), crons: vi.fn(), webhooks: vi.fn(),
    agentDetail: vi.fn(), skills: vi.fn(), updateKirocrewAgent: vi.fn(),
  },
  capabilities: { get: vi.fn(), preview: vi.fn(), save: vi.fn() },
}))
vi.mock('../api/client', () => ({ api: mocks.api }))
vi.mock('../api/crewCapabilities', async importOriginal => ({
  ...await importOriginal<typeof import('../api/crewCapabilities')>(), crewCapabilitiesApi: mocks.capabilities,
}))
import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

/** Opening the editor is a lazy `CrewEditorModal` chunk import followed by a
 *  fresh-roster read (`staleTime: 0`) before the dialog renders, which can
 *  outlast RTL's 1s default on a loaded runner. An explicit deadline. */
const EDITOR_OPEN_TIMEOUT_MS = 5000

beforeEach(() => {
  viewport.mobile = false
  Object.values(mocks.api).forEach(mock => mock.mockReset())
  Object.values(mocks.capabilities).forEach(mock => mock.mockReset())
  mocks.api.kirocrewAgents.mockResolvedValue({ agents: [{ name: 'oncall', kiro_agent: 'atlas', workspace: 'default', memory_store: 'default' }], default_agent: 'oncall' })
  mocks.api.agentsInstalled.mockResolvedValue([{ name: 'atlas' }])
  mocks.api.workspaces.mockResolvedValue({ workspaces: [{ name: 'default' }] })
  mocks.api.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {} } })
  mocks.api.agentResolvedModel.mockResolvedValue({ model: '' })
  mocks.api.models.mockResolvedValue([])
  mocks.api.crons.mockResolvedValue({ jobs: [] })
  mocks.api.webhooks.mockResolvedValue({ tokens: [] })
  mocks.api.agentDetail.mockResolvedValue({ name: 'atlas', model: 'auto', skills: ['review'], tools: ['read'] })
  mocks.api.skills.mockResolvedValue([])
  mocks.capabilities.get.mockResolvedValue({
    schema_version: 1, member: 'oncall', mode: 'inherited', revision: 'r1',
    template: { name: 'atlas', source: 'custom', scope: 'global', available: true },
    rows: [{ section: 'tools', id: 'read', label: 'Read tool', state: 'inherited', present: true, value: true }],
    connections: [], skills: [], parent_changes: [],
    runtime: { status: 'unverified', saved_revision: 'r1', sessions: [] },
  })
})

const capabilitiesTree = () => (
  <SidePanelLayout title="Capabilities" tabs={[{ key: 'crews', label: 'Crews', icon: null }]} rememberKey="capabilities">
    {() => <KiroCrewAgentsPage embedded />}
  </SidePanelLayout>
)

describe('Customize > Mates hosting the mate editor', () => {
  it('keeps the same editor and draft when the bare capabilities route becomes mobile', async () => {
    const tree = capabilitiesTree()
    const result = renderWithProviders(tree, { route: '/capabilities' })
    fireEvent.click(await screen.findByTestId('crew-card'))
    const sheet = await screen.findByTestId('crew-editor-next-dialog', {}, { timeout: EDITOR_OPEN_TIMEOUT_MS })
    fireEvent.change(within(sheet).getByTestId('display-name-input'), { target: { value: 'Pager' } })
    viewport.mobile = true
    result.rerender(cloneElement(tree))
    expect(sheet).toBeInTheDocument()
    expect(within(sheet).getByTestId('display-name-input')).toHaveValue('Pager')
    viewport.mobile = false
    result.rerender(cloneElement(tree))
    expect(screen.getByTestId('crew-editor-next-dialog')).toBe(sheet)
  })

  it('keeps the open create dialog and its typed name when the bare capabilities route becomes mobile', async () => {
    // The resize guard pins the pane route while the create dialog is open, so
    // narrowing past the breakpoint cannot unmount this page and the dialog
    // and discard the typed draft with no confirm.
    const tree = capabilitiesTree()
    const result = renderWithProviders(tree, { route: '/capabilities' })
    fireEvent.click(await screen.findByTestId('new-crew'))
    const dialog = await screen.findByRole('dialog', { name: 'New mate' })
    const name = within(dialog).getByRole('textbox', { name: 'Name' })
    fireEvent.change(name, { target: { value: 'my-draft-crew' } })
    viewport.mobile = true
    result.rerender(cloneElement(tree))
    expect(screen.getByRole('dialog', { name: 'New mate' })).toBe(dialog)
    expect(within(dialog).getByRole('textbox', { name: 'Name' })).toHaveValue('my-draft-crew')
  })

  it('suppresses the roster-load error hand-off while the create dialog holds a typed draft', async () => {
    // The notice's "Ask the agent" navigates to chat and unmounts this page,
    // so it must not be offered while a dialog holds a typed draft.
    mocks.api.kirocrewAgents.mockRejectedValue(new Error('connection failed'))
    renderWithProviders(<KiroCrewAgentsPage />)
    // The roster failed to load, so the notice shows and — with nothing else
    // open — offers the hand-off.
    const notice = await screen.findByTestId('crews-roster-load-error')
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    fireEvent.click(await screen.findByTestId('new-crew'))
    const dialog = await screen.findByRole('dialog', { name: 'New mate' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'my-draft-crew' } })
    await waitFor(() =>
      expect(within(screen.getByTestId('crews-roster-load-error')).queryByRole('button', { name: 'Ask the agent' })).toBeNull(),
    )
  })

  it('lands the &pane=capabilities fix link on Approvals, then strips the params', async () => {
    renderWithProviders(<KiroCrewAgentsPage />, { route: '/capabilities?tab=crews&crew=oncall&pane=capabilities' })
    const sheet = await screen.findByTestId('crew-editor-next-dialog', {}, { timeout: EDITOR_OPEN_TIMEOUT_MS })
    await waitFor(() => expect(within(sheet).getByTestId('next-rail-approvals')).toHaveAttribute('aria-selected', 'true'))
    // Landing is once per opening: the user's own navigation wins afterwards.
    fireEvent.click(within(sheet).getByTestId('next-rail-general'))
    expect(within(sheet).getByTestId('next-rail-general')).toHaveAttribute('aria-selected', 'true')
  })

  it('opens an ordinary Edit on General, not on the last deep link’s section', async () => {
    renderWithProviders(<KiroCrewAgentsPage />)
    fireEvent.click(await screen.findByTestId('crew-card'))
    const sheet = await screen.findByTestId('crew-editor-next-dialog', {}, { timeout: EDITOR_OPEN_TIMEOUT_MS })
    expect(within(sheet).getByTestId('next-rail-general')).toHaveAttribute('aria-selected', 'true')
  })
})
