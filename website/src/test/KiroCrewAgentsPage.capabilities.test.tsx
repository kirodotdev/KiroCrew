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
    agentDetail: vi.fn(), agentPatch: vi.fn(), skills: vi.fn(), updateKirocrewAgent: vi.fn(),
  },
  capabilities: { get: vi.fn(), preview: vi.fn(), save: vi.fn() },
}))
vi.mock('../api/client', () => ({ api: mocks.api }))
vi.mock('../api/crewCapabilities', async importOriginal => ({
  ...await importOriginal<typeof import('../api/crewCapabilities')>(), crewCapabilitiesApi: mocks.capabilities,
}))
import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

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
async function open() {
  renderWithProviders(<KiroCrewAgentsPage />)
  fireEvent.click(await screen.findByTestId('crew-card'))
  const sheet = await screen.findByRole('dialog', { name: 'Edit agent oncall' })
  fireEvent.click(within(sheet).getByTestId('crew-rail-capabilities'))
  await screen.findByText('Parent template: atlas')
  return sheet
}
async function edit() {
  const sheet = await open()
  fireEvent.click(within(sheet).getByRole('tab', { name: 'Tools', exact: true }))
  fireEvent.click(within(sheet).getByRole('combobox', { name: 'Source for Read tool' }))
  fireEvent.click(await screen.findByRole('option', { name: 'Removed', exact: true }))
  await screen.findByTestId('crew-rail-dirty-capabilities')
  return sheet
}

describe('capability pane inside the crew dialog', () => {
  it('dismisses the nested footer confirm with Escape without closing the editor', async () => {
    const sheet = await edit()
    const discard = within(sheet).getByRole('button', { name: 'Discard draft', exact: true })
    discard.focus()
    fireEvent.click(discard)
    const confirm = await screen.findByRole('dialog', { name: 'Discard the Capabilities draft?' })
    fireEvent.keyDown(confirm, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Discard the Capabilities draft?' })).toBeNull())
    expect(screen.getByRole('dialog', { name: 'Edit agent oncall' })).toBe(sheet)
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes and close the editor?' })).toBeNull()
    expect(discard).toHaveFocus()
    expect(within(sheet).getByRole('combobox', { name: 'Source for Read tool' })).toHaveTextContent('Removed')
    fireEvent.click(discard)
    fireEvent.click(within(await screen.findByRole('dialog', { name: 'Discard the Capabilities draft?' })).getByRole('button', { name: 'Discard draft' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Discard the Capabilities draft?' })).toBeNull())
    expect(screen.getByRole('dialog', { name: 'Edit agent oncall' })).toBe(sheet)
    expect(within(sheet).getByRole('combobox', { name: 'Source for Read tool' })).toHaveTextContent('Inherited')
    expect(within(sheet).queryByTestId('crew-rail-dirty-capabilities')).toBeNull()
    expect(mocks.capabilities.save).not.toHaveBeenCalled()
    expect(mocks.api.updateKirocrewAgent).not.toHaveBeenCalled()
  })
  it('keeps the same editor and draft when the bare capabilities route becomes mobile', async () => {
    const tree = <SidePanelLayout title="Capabilities" tabs={[{ key: 'crews', label: 'Crews', icon: null }]} rememberKey="capabilities">
      {() => <KiroCrewAgentsPage embedded />}
    </SidePanelLayout>
    const result = renderWithProviders(tree, { route: '/capabilities' })
    fireEvent.click(await screen.findByTestId('crew-card'))
    const sheet = await screen.findByRole('dialog', { name: 'Edit agent oncall' })
    fireEvent.click(within(sheet).getByTestId('crew-rail-capabilities'))
    await screen.findByText('Parent template: atlas')
    fireEvent.click(within(sheet).getByRole('tab', { name: 'Tools', exact: true }))
    fireEvent.click(within(sheet).getByRole('combobox', { name: 'Source for Read tool' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Removed', exact: true }))
    await screen.findByTestId('crew-rail-dirty-capabilities')
    viewport.mobile = true
    result.rerender(cloneElement(tree))
    expect(sheet).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Source for Read tool' })).toHaveTextContent('Removed')
    viewport.mobile = false
    result.rerender(cloneElement(tree))
    expect(screen.getByRole('dialog', { name: 'Edit agent oncall' })).toBe(sheet)
  })

  it('gives member identity its own full-width row before narrow header actions', async () => {
    const sheet = await open()
    expect(within(sheet).getByTestId('crew-editor-identity')).toHaveClass('w-full')
    expect(within(sheet).getByTestId('crew-editor-identity').parentElement).toHaveClass('flex-wrap')
  })

  it('keeps the draft across rail changes and guards Escape', async () => {
    const sheet = await edit()
    fireEvent.click(within(sheet).getByTestId('crew-rail-overview'))
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes and close the editor?' })).not.toBeInTheDocument()
    expect(within(sheet).getByRole('button', { name: 'Save changes' })).toBeDisabled()
    // The other panes' Save is dead while a capability draft is open; the
    // reason is visible text in the footer note, not only a hover title.
    expect(within(sheet).getByTestId('crew-unsaved-note')).toHaveTextContent('Save or discard the Capabilities draft first')
    fireEvent.click(within(sheet).getByTestId('crew-rail-capabilities'))
    expect(within(sheet).getByRole('combobox', { name: 'Source for Read tool' })).toHaveTextContent('Removed')
    fireEvent.keyDown(sheet, { key: 'Escape' })
    const confirm = await screen.findByRole('dialog', { name: 'Discard unsaved changes and close the editor?' })
    fireEvent.click(within(confirm).getByTestId('crew-sched-discard-keep'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes and close the editor?' })).not.toBeInTheDocument())
    expect(within(sheet).getByRole('combobox', { name: 'Source for Read tool' })).toHaveTextContent('Removed')
    expect(mocks.capabilities.save).not.toHaveBeenCalled()
    expect(mocks.api.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('guards Escape immediately after replacing the tool reference', async () => {
    const sheet = await open()
    fireEvent.click(within(sheet).getByRole('tab', { name: 'Tools', exact: true }))
    const input = within(sheet).getByRole('textbox', { name: 'Exact tool reference, such as @server/tool' })
    fireEvent.change(input, { target: { value: '@docs/search' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    const confirm = await screen.findByRole('dialog', { name: 'Discard unsaved changes and close the editor?' })
    fireEvent.click(within(confirm).getByTestId('crew-sched-discard-keep'))
    expect(sheet).toBeInTheDocument()
    expect(input).toHaveValue('@docs/search')
    expect(mocks.capabilities.save).not.toHaveBeenCalled()
  })

  it('discards only on explicit confirmation and leaves server state untouched', async () => {
    const sheet = await edit()
    fireEvent.keyDown(sheet, { key: 'Escape' })
    const confirm = await screen.findByRole('dialog', { name: 'Discard unsaved changes and close the editor?' })
    // The dialog's red button closes the editor; the pane footer's "Discard
    // draft" does not, so the two labels must not read identically.
    expect(within(confirm).getByTestId('crew-sched-discard-confirm')).toHaveTextContent('Discard changes and close')
    // The pane footer sits behind the modal (aria-hidden), so read it by test id.
    expect(within(within(sheet).getByTestId('capability-save-footer')).getByText('Discard draft', { exact: true })).toBeInTheDocument()
    // The title wraps instead of truncating at narrow widths.
    expect(within(confirm).getByText('Discard unsaved changes and close the editor?')).toHaveClass('whitespace-normal')
    fireEvent.click(within(confirm).getByTestId('crew-sched-discard-confirm'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit agent oncall' })).not.toBeInTheDocument())
    expect(mocks.capabilities.save).not.toHaveBeenCalled()
  })

  it('locks instant template edits when capability state cannot be read', async () => {
    mocks.capabilities.get.mockRejectedValue(new Error('connection failed'))
    renderWithProviders(<KiroCrewAgentsPage />)
    fireEvent.click(await screen.findByTestId('crew-card'))
    const sheet = await screen.findByRole('dialog', { name: 'Edit agent oncall' })
    fireEvent.click(within(sheet).getByTestId('crew-rail-template'))
    expect(await within(sheet).findByTestId('crew-template-switch-error')).toHaveTextContent('The capabilities request failed. Your draft is kept. Reload from server and retry.')
    expect(await within(sheet).findByRole('combobox', { name: 'Model', exact: true })).toBeDisabled()
    expect(mocks.api.agentPatch).not.toHaveBeenCalled()
  })

  it('makes the enrolled template definition read-only without changing the member model pane', async () => {
    const sheet = await open()
    fireEvent.click(within(sheet).getByTestId('crew-rail-template'))
    const model = await screen.findByRole('combobox', { name: 'Model', exact: true })
    expect(model).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Agent Template' })).toBeDisabled()
    expect(screen.queryByText('Add skills')).not.toBeInTheDocument()
    fireEvent.click(within(sheet).getByTestId('crew-rail-model'))
    expect(await screen.findByRole('combobox', { name: 'Edit default model' })).not.toBeDisabled()
    expect(mocks.api.agentPatch).not.toHaveBeenCalled()
  })
})
