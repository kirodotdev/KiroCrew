import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { renderWithProviders } from '../test/helpers'
import type { OrganizationSnapshot } from '../api/organization'
import '../api/client'
import OrganizationPage from './OrganizationPage'
import OrganizationMemberWork from './members/OrganizationMemberWork'
import SidePanelLayout from '../components/SidePanelLayout'

let snapshot: OrganizationSnapshot
let writes: Record<string, unknown>[]

afterEach(() => vi.restoreAllMocks())

function renderInCapabilities() {
  return renderWithProviders(
    <SidePanelLayout title="Capabilities" tabs={[
      { key: 'organization', label: 'Organization', icon: null },
      { key: 'other', label: 'Other capability', icon: null },
    ]}>
      {tab => tab === 'organization' ? <OrganizationPage embedded /> : <p>Other capability content</p>}
    </SidePanelLayout>,
    { route: '/capabilities?tab=organization' },
  )
}

beforeEach(() => {
  writes = []
  snapshot = {
    settings: {
      revision: 1, enabled: false, concurrency: 3,
      staffing: { conductor: { manager: 2, researcher: 2 }, manager: { engineer: 3 }, engineer: {}, researcher: {} },
    },
    runtime: { ready: true, backend: '', reason: '' },
    members: [
      { id: 'root', name: 'Conductor', role: 'conductor', manager_id: null, state: 'active', permissions: [] },
      { id: 'manager', name: 'Engineering manager', role: 'manager', manager_id: 'root', state: 'active', permissions: [] },
      { id: 'engineer', name: 'Engineer', role: 'engineer', manager_id: 'manager', state: 'active', permissions: [] },
    ],
    tasks: [], messages: [], runs: [],
  }
  server.use(
    http.get('/api/organization', () => HttpResponse.json(snapshot)),
    http.post('/api/organization', async ({ request }) => {
      writes.push(await request.json() as Record<string, unknown>)
      return HttpResponse.json({ ok: true })
    }),
  )
})

describe('Org Chart and Guardrails', () => {
  it('assigns owner work to the selected member with acceptance conditions', async () => {
    renderWithProviders(<OrganizationPage />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Work and messages' }))
    fireEvent.change(screen.getByLabelText('Task'), { target: { value: 'Build a greeting' } })
    fireEvent.change(screen.getByLabelText('Acceptance conditions'), { target: { value: 'A tested file' } })
    fireEvent.click(screen.getByRole('button', { name: 'Assign a task' }))
    await waitFor(() => expect(writes).toEqual([{
      action: 'assign', recipient: 'root', title: 'Build a greeting', acceptance: 'A tested file',
    }]))
  })

  it('keeps team execution unavailable when the role boundary is unsupported', async () => {
    snapshot.runtime = { ready: false, backend: 'unsupported', reason: 'Choose a supported runtime.' }
    renderWithProviders(<OrganizationPage />)
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('Choose a supported runtime.')
    expect(within(notice).queryByRole('button', { name: /Ask the agent/i })).toBeNull()
    fireEvent.change(screen.getByLabelText('Name (optional)'), { target: { value: 'Draft member' } })
    expect(screen.getByLabelText('Name (optional)')).toHaveValue('Draft member')
    expect(screen.getByRole('button', { name: 'Run team' }).hasAttribute('disabled')).toBe(true)
    expect(writes).toEqual([])
  })

  it('shows an enabled team as unavailable and still pauses without submitting staffing drafts', async () => {
    snapshot.settings.enabled = true
    snapshot.runtime = { ready: false, backend: 'unsupported', reason: 'Choose a supported runtime.' }
    const saved = structuredClone(snapshot.settings)
    server.use(http.post('/api/organization', async ({ request }) => {
      const values = await request.json() as Record<string, unknown>
      writes.push(values)
      snapshot.settings.enabled = values.enabled as boolean
      return HttpResponse.json({ ok: true })
    }))
    renderWithProviders(<OrganizationPage />)
    expect(await screen.findByText('Unavailable', { exact: true })).toBeVisible()
    expect(screen.queryByText('Team running')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a supported runtime.')
    await userEvent.click(screen.getByRole('tab', { name: 'Staffing' }))
    fireEvent.change(screen.getByLabelText('Members working at once'), { target: { value: '2' } })
    const pause = screen.getByRole('button', { name: 'Pause new work' })
    expect(pause).toBeEnabled()
    await userEvent.click(pause)
    await waitFor(() => expect(writes).toEqual([{ action: 'configure', ...saved, enabled: false }]))
    expect(await screen.findByText('New work paused')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Run team' })).toBeDisabled()
    expect(screen.getByLabelText('Members working at once')).toHaveValue(2)
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a supported runtime.')
  })

  it('labels the reporting relationship separately from the Manager role', async () => {
    renderWithProviders(<OrganizationPage memberName="Engineering manager" />)
    expect(await screen.findByText('Manager · Reports to: Conductor')).toBeVisible()
    expect(screen.getByText('Reports to', { selector: 'label > span' })).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Reports to' })).toHaveTextContent('Conductor')
  })

  it.each(['failed', 'interrupted'])('shows %s activity without an empty work card and keeps retry available', async state => {
    snapshot.runs = [{ id: 'run', member_id: 'engineer', state, error: 'Review the saved work before retrying.' }]
    renderWithProviders(<OrganizationPage />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Work and messages' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Review the saved work before retrying.')
    expect(screen.queryByText('No assignments yet.')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Retry work' }))
    await waitFor(() => expect(writes).toEqual([{ action: 'retry', member_id: 'engineer' }]))
  })

  it.each([
    { label: 'no runs', runs: [] },
    { label: 'completed runs only', runs: [{ id: 'run', member_id: 'engineer', state: 'completed', error: '' }] },
  ])(
    'shows the empty work card when there are no assignments or displayed runs ($label)',
    async ({ runs }) => {
      snapshot.runs = runs
      renderWithProviders(<OrganizationPage />)
      await userEvent.click(await screen.findByRole('tab', { name: 'Work and messages' }))
      expect(screen.getByText('No assignments yet.')).toBeVisible()
      expect(screen.queryByRole('button', { name: 'Retry work' })).toBeNull()
    },
  )

  it('names team controls and the separate save scopes in a member editor', async () => {
    renderWithProviders(<OrganizationPage memberName="Engineer" />)
    expect(await screen.findByText('Run, pause and staffing controls here affect the whole team.')).toBeVisible()
    expect(screen.getByText('Save changes saves this member’s settings. Finish or discard drafts here first.')).toBeVisible()
    await userEvent.click(screen.getByRole('tab', { name: 'Staffing' }))
    expect(screen.getByText('Save staffing applies team limits immediately. No restart is needed.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save staffing' })).toBeEnabled()
  })

  it('describes the latest member work as team activity', () => {
    snapshot.runs = [{ id: 'run', member_id: 'engineer', state: 'failed', error: 'Failed to start' }]
    renderWithProviders(<OrganizationMemberWork member={snapshot.members[2]} data={snapshot} />)
    expect(screen.getByText('Latest team activity: Failed')).toBeVisible()
  })

  it.each(['Cancel', 'Escape'])('keeps the member and work draft when retirement is dismissed with %s', async dismissal => {
    renderWithProviders(<OrganizationPage memberName="Engineer" />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Work and messages' }))
    fireEvent.change(screen.getByLabelText('Task'), { target: { value: 'Keep this draft' } })
    await userEvent.click(screen.getByRole('tab', { name: 'Org chart', exact: true }))
    await userEvent.click(screen.getByRole('button', { name: 'More actions' }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Retire member' }))
    const dialog = await screen.findByRole('dialog', { name: 'Retire member “Engineer”?' })
    expect(within(dialog).getByText('This member will stop receiving team work. Memory and conversation history are kept. Retirement cannot be undone.')).toBeVisible()
    expect(writes).toEqual([])
    if (dismissal === 'Escape') await userEvent.keyboard('{Escape}')
    else await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel', exact: true }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(writes).toEqual([])
    await userEvent.click(screen.getByRole('tab', { name: 'Work and messages' }))
    expect(screen.getByLabelText('Task')).toHaveValue('Keep this draft')
  })

  it('retires only the named member after explicit confirmation', async () => {
    renderWithProviders(<OrganizationPage memberName="Engineer" />)
    await userEvent.click(await screen.findByRole('button', { name: 'More actions' }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Retire member' }))
    const dialog = await screen.findByRole('dialog', { name: 'Retire member “Engineer”?' })
    expect(writes).toEqual([])
    await userEvent.click(within(dialog).getByRole('button', { name: 'Retire member', exact: true }))
    await waitFor(() => expect(writes).toEqual([{ action: 'retire', member_id: 'engineer' }]))
  })

  it('does not substitute the conductor for an unrelated member editor', async () => {
    renderWithProviders(<OrganizationPage memberName="Unmanaged writer" />)
    expect(await screen.findByText(/This member is outside the organization/)).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Open conversation' })).toBeNull()
  })

  it('submits per-manager staffing independently of concurrency', async () => {
    renderWithProviders(<OrganizationPage />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Staffing' }))
    fireEvent.change(screen.getByLabelText('Members working at once'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save staffing', exact: true }))
    await waitFor(() => expect(writes).toEqual([{
      action: 'configure', ...snapshot.settings, concurrency: 1,
    }]))
  })

  it('connects keyboard navigation to the named organization panels', async () => {
    renderWithProviders(<OrganizationPage />)
    const chart = await screen.findByRole('tab', { name: 'Org chart', exact: true })
    chart.focus()
    await userEvent.keyboard('{ArrowRight}')
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Guardrails' }).getAttribute('aria-selected')).toBe('true'))
    expect(screen.getByRole('tabpanel', { name: 'Guardrails' })).toBeTruthy()
    expect(screen.getByText('Plans, hires, delegates and reviews. Cannot edit project files or run a shell.')).toBeTruthy()
  })

  it.each(['Task', 'Acceptance conditions', 'Message', 'Review decision (required)'])('keeps a %s draft when leaving is declined', async field => {
    snapshot.tasks = [{
      id: 'root-task', parent_id: null, sender: 'owner', recipient: 'root',
      title: 'Review work', acceptance: 'Tests', state: 'review', report: 'Passed', decision: '',
    }]
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderInCapabilities()
    await userEvent.click(await screen.findByRole('tab', { name: 'Work and messages' }))
    fireEvent.change(screen.getByLabelText(field), { target: { value: 'Do not lose this draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Other capability' }))
    expect(confirm).toHaveBeenCalledWith('Discard unsaved changes?')
    expect(screen.getByLabelText(field)).toHaveValue('Do not lose this draft')
    expect(screen.queryByText('Other capability content')).toBeNull()
    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Other capability' }))
    expect(screen.getByText('Other capability content')).toBeTruthy()
  })

  it.each([true, false])('toggles execution using saved settings while retaining an invalid staffing draft (enabled=%s)', async enabled => {
    snapshot.settings.enabled = enabled
    const saved = structuredClone(snapshot.settings)
    server.use(http.post('/api/organization', async ({ request }) => {
      const values = await request.json() as Record<string, unknown>
      writes.push(values)
      snapshot.settings.enabled = values.enabled as boolean
      return HttpResponse.json({ ok: true })
    }))
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderInCapabilities()
    await userEvent.click(await screen.findByRole('tab', { name: 'Staffing' }))
    const engineerLimit = screen.getAllByLabelText('Engineer')[1]
    fireEvent.change(engineerLimit, { target: { value: '0' } }) // an engineer already exists
    fireEvent.click(screen.getByRole('button', { name: enabled ? 'Pause new work' : 'Run team' }))
    await waitFor(() => expect(writes).toEqual([{ action: 'configure', ...saved, enabled: !enabled }]))
    expect(engineerLimit).toHaveValue(0)
    fireEvent.click(screen.getByRole('button', { name: 'Other capability' }))
    expect(confirm).toHaveBeenCalled()
    expect(engineerLimit).toHaveValue(0)
  })

  it('clears a saved staffing draft so it no longer blocks navigation', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderInCapabilities()
    await userEvent.click(await screen.findByRole('tab', { name: 'Staffing' }))
    fireEvent.change(screen.getByLabelText('Members working at once'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save staffing', exact: true }))
    await waitFor(() => expect(screen.getByLabelText('Members working at once')).toHaveValue(3))
    fireEvent.click(screen.getByRole('button', { name: 'Other capability' }))
    expect(confirm).not.toHaveBeenCalled()
    expect(screen.getByText('Other capability content')).toBeTruthy()
  })

  it.each([
    ['Name (optional)', 'Create member', 'Org chart'],
    ['Task', 'Assign a task', 'Work and messages'],
    ['Message', 'Send message', 'Work and messages'],
    ['Review decision (required)', 'Accept', 'Work and messages'],
  ])('preserves new %s text typed while the previous submission is pending', async (field, action, tab) => {
    snapshot.tasks = [{
      id: 'root-task', parent_id: null, sender: 'owner', recipient: 'root',
      title: 'Review work', acceptance: 'Tests', state: 'review', report: 'Passed', decision: '',
    }]
    let release: () => void = () => {}
    const pending = new Promise<void>(resolve => { release = resolve })
    server.use(http.post('/api/organization', async ({ request }) => {
      writes.push(await request.json() as Record<string, unknown>)
      await pending
      return HttpResponse.json({ ok: true })
    }))
    renderInCapabilities()
    await userEvent.click(await screen.findByRole('tab', { name: tab, exact: true }))
    if (field === 'Task') fireEvent.change(screen.getByLabelText('Acceptance conditions'), { target: { value: 'Evidence' } })
    fireEvent.change(screen.getByLabelText(field), { target: { value: 'First draft' } })
    try {
      fireEvent.click(screen.getByRole('button', { name: action, exact: true }))
      await waitFor(() => expect(writes).toHaveLength(1))
      fireEvent.change(screen.getByLabelText(field), { target: { value: 'Next draft' } })
    } finally {
      release()
    }
    await waitFor(() => expect(screen.getByRole('button', { name: action, exact: true })).toBeEnabled())
    expect(screen.getByLabelText(field)).toHaveValue('Next draft')
  })

  it.each(['Request changes', 'Cancel task'])('puts %s in the review overflow and clears only the submitted decision', async action => {
    snapshot.tasks = [{
      id: 'root-task', parent_id: null, sender: 'owner', recipient: 'root',
      title: 'Review work', acceptance: 'Tests', state: 'review', report: 'Passed', decision: '',
    }]
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderInCapabilities()
    await userEvent.click(await screen.findByRole('tab', { name: 'Work and messages' }))
    const decision = screen.getByRole('textbox', { name: 'Review decision (required)' })
    const decisionLabel = screen.getByText('Review decision (required)', { selector: 'label' })
    expect(decisionLabel).toBeVisible()
    expect(decisionLabel).toHaveAttribute('for', decision.id)
    expect(decision).toBeRequired()
    expect(screen.getByRole('button', { name: 'Accept', exact: true })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Review decision (required)'), { target: { value: 'Review evidence' } })
    const row = screen.getByRole('button', { name: 'Accept', exact: true }).parentElement!
    expect(within(row).getAllByRole('button')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: action, exact: true })).toBeNull()
    await userEvent.click(within(row).getByRole('button', { name: 'More actions' }))
    await userEvent.click(screen.getByRole('menuitem', { name: action }))
    await waitFor(() => expect(writes).toEqual([{
      action: 'review', task_id: 'root-task', verdict: action === 'Cancel task' ? 'cancel' : 'revise', text: 'Review evidence',
    }]))
    await waitFor(() => expect(screen.getByLabelText('Review decision (required)')).toHaveValue(''))
    fireEvent.click(screen.getByRole('button', { name: 'Other capability' }))
    expect(confirm).not.toHaveBeenCalled()
  })
})
