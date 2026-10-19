import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { crewCapabilitiesApi, type CapabilityDraft, type CapabilityOperation, type CapabilityView } from '../api/crewCapabilities'
import CrewCapabilitiesPane from '../components/crew/CrewCapabilitiesPane'

const endpoint = '/api/agents/crewA/capabilities'
let view: CapabilityView
let previewBodies: CapabilityDraft[]
let saveBodies: (CapabilityDraft & { preview_token: string })[]
const baseView = (): CapabilityView => ({
  schema_version: 1, member: 'crewA', mode: 'inherited', revision: 'r1',
  template: { name: 'atlas', source: 'custom', scope: 'global', available: true },
  rows: [
    { section: 'tools', id: '@search/read', label: 'Search read', state: 'inherited', present: true, value: true },
    { section: 'allowedTools', id: '@search/read', label: 'Search read', state: 'local', present: true, value: true },
    { section: 'autoApprove', id: '@search/list', label: 'Search list', state: 'inherited', present: true, value: true },
    { section: 'mcpServers', id: 'search', label: 'Search', state: 'local', present: true, value: { command: 'search-cli', args: ['--read'], env: {} } },
    { section: 'mcpServers', id: 'protected', label: 'Protected', state: 'inherited', present: true, value: { command: 'protected-cli', args: ['--read', '[REDACTED]'], env: { TOKEN: '[REDACTED]' } } },
    { section: 'mcpServers', id: 'managed', label: 'Managed', state: 'inherited', present: true, value: { command: 'managed-cli' }, managed: true },
    { section: 'skills', id: 'catalog/review', label: 'Review skill', state: 'removed', present: false, value: null },
    { section: 'resources', id: 'file:///manual/*.md', label: 'Manual reference', state: 'local', present: true, value: 'file:///manual/*.md', shared_reference: true },
  ],
  skills: [{ id: 'catalog/review', label: 'Review skill', shared_reference: true }, { id: 'catalog/test', label: 'Test skill', shared_reference: true }],
  connections: [{ id: 'configured', label: 'Configured search', managed: false }],
  parent_changes: [{ section: 'tools', id: '@search/read', kind: 'changed', conflict: true, requires_approval: true, before: true, after: true }],
  runtime: { status: 'unverified', saved_revision: 'r1', sessions: [] },
})

beforeEach(() => {
  view = baseView(); previewBodies = []; saveBodies = []
  server.use(
    http.get(endpoint, () => HttpResponse.json(view)),
    http.post(`${endpoint}/preview`, async ({ request }) => {
      const body = await request.json() as CapabilityDraft
      previewBodies.push(body)
      return HttpResponse.json({ ...view, preview_token: 'signed-preview', impact: body.operations.map(op => ({ ...op, member: 'crewA', change: 'changed', approval_expanded: op.section === 'allowedTools' })) })
    }),
    http.put(endpoint, async ({ request }) => {
      saveBodies.push(await request.json() as CapabilityDraft & { preview_token: string })
      view = { ...view, revision: 'r2', runtime: { ...view.runtime, status: 'pending', saved_revision: 'r2' } }
      return HttpResponse.json(view)
    }),
  )
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const dirty = vi.fn(), busy = vi.fn(), saved = vi.fn()
  const tree = (hidden: boolean) => <QueryClientProvider client={client}><CrewCapabilitiesPane member="crewA" members={['crewA', 'crewB', 'crewC']} hidden={hidden} onDirtyChange={dirty} onBusyChange={busy} onSaved={saved} /></QueryClientProvider>
  const result = render(tree(false))
  return { ...result, client, dirty, busy, saved, hide: (hidden: boolean) => result.rerender(tree(hidden)) }
}
async function ready() { await screen.findByText('Parent template: atlas') }
async function pickState(name: string, state: string) {
  fireEvent.click(screen.getByRole('combobox', { name: `Source for ${name}` }))
  fireEvent.click(await screen.findByRole('option', { name: state, exact: true }))
}
async function review() {
  fireEvent.click(screen.getByRole('button', { name: 'Review changes', exact: true }))
  await screen.findByRole('button', { name: 'Save reviewed changes' })
}

describe('crew capability draft editor with mocked HTTP', () => {
  it('keeps category navigation non-shrinking beside an expanded transport', async () => {
    mount(); await ready()
    const row = within(screen.getByTestId('capability-mcpServers-search'))
    fireEvent.click(row.getByText('Connection settings'))
    fireEvent.change(row.getByLabelText('Command'), { target: { value: 'draft-command' } })
    const tabs = screen.getByRole('tablist', { name: 'Capabilities' })
    expect(tabs.parentElement).toHaveClass('shrink-0', 'overflow-x-auto')
    fireEvent.click(within(tabs).getByRole('tab', { name: 'Tools', exact: true }))
    expect(screen.getByText('Search read')).toBeInTheDocument()
    fireEvent.click(within(tabs).getByRole('tab', { name: 'MCP Servers', exact: true }))
    expect(within(screen.getByTestId('capability-mcpServers-search')).getByLabelText('Command')).toHaveValue('draft-command')
  })

  it('keeps the save controls outside the focused input scroll region', async () => {
    mount(); await ready()
    const scroller = screen.getByTestId('capability-scroll-region')
    fireEvent.click(within(screen.getByTestId('capability-mcpServers-search')).getByText('Connection settings'))
    const command = within(screen.getByTestId('capability-mcpServers-search')).getByLabelText('Command')
    command.focus()
    expect(scroller.contains(command)).toBe(true)
    expect(scroller.contains(screen.getByRole('button', { name: 'Review changes' }))).toBe(false)
  })

  it('renders proposed values only from the sanitized preview response', async () => {
    server.use(http.post(`${endpoint}/preview`, () => HttpResponse.json({ ...view,
      rows: [{ ...view.rows[3], value: { command: 'server-approved-command', env: { TOKEN: '[REDACTED]' } } }],
      preview_token: 'signed-preview', impact: [{ member: 'crewA', section: 'mcpServers', id: 'search', change: 'changed', approval_expanded: false }],
    })))
    mount(); await ready()
    const row = within(screen.getByTestId('capability-mcpServers-search'))
    fireEvent.click(row.getByText('Connection settings'))
    fireEvent.change(row.getByLabelText('Command'), { target: { value: 'unsanitized-draft-command' } })
    await review()
    const preview = within(screen.getByRole('region', { name: 'Changes to be saved' }))
    expect(preview.getByText(/server-approved-command/)).toBeInTheDocument()
    expect(preview.queryByText(/unsanitized-draft-command/)).not.toBeInTheDocument()
  })

  it('reports an unavailable parent as a source error, not a provider load failure', async () => {
    view.template.available = false
    view.runtime = { ...view.runtime, status: 'failed', error_code: 'parent_missing' }
    mount(); await ready()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.queryByText('parent_missing')).not.toBeInTheDocument()
    expect(screen.queryByText('Server notices')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('The parent template is missing or unverified, so saving is blocked.')
    expect(screen.queryByText('Runtime loading failed. The saved version remains.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload from server (keeps your draft)' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled()
  })

  it.each([
    { code: 'source_changed', message: 'Could not validate the saved configuration. Reload from server to retry.', other: 'Runtime loading failed. The saved version remains.' },
    { code: undefined, message: 'Runtime loading failed. The saved version remains.', other: 'Could not validate the saved configuration. Reload from server to retry.' },
  ])('keeps validation and provider failures distinct ($code)', async ({ code, message, other }) => {
    view.runtime = { ...view.runtime, status: 'failed', error_code: code, sessions: code ? [] : [{ session_key: 'test-session', status: 'failed', error_code: 'capability_mcp_failed' }] }
    mount(); await ready()
    expect(screen.getByRole('alert')).toHaveTextContent(message)
    expect(screen.queryByText(other)).not.toBeInTheDocument()
  })

  it('lets a changed agent file be reviewed and saved with no edit first', async () => {
    view.runtime = { ...view.runtime, status: 'failed', error_code: 'materialization_changed' }
    const result = mount(); await ready()
    // Fact first, then the button that is actually on screen; Save is named
    // only as what comes after the review, never as a control to press now.
    const notice = screen.getByRole('alert').textContent ?? ''
    expect(notice.startsWith("crewA's agent file was edited outside this page.")).toBe(true)
    expect(notice).toContain('Press Review changes to see that outside edit')
    expect(notice).toContain('After reviewing, Save writes the fix or tells you why it cannot.')
    expect(result.dirty).toHaveBeenLastCalledWith(false)
    await review()
    expect(previewBodies).toEqual([{ revision: 'r1', enroll: false, operations: [], accept_parent: [], accept_members: [] }])
    fireEvent.click(screen.getByRole('button', { name: 'Save reviewed changes' }))
    await waitFor(() => expect(result.saved).toHaveBeenCalledOnce())
    expect(saveBodies).toEqual([{ ...previewBodies[0], preview_token: 'signed-preview' }])
  })

  it('explains a refused save beside the disabled Review changes button, and Reload clears it', async () => {
    view.runtime = { ...view.runtime, status: 'failed', error_code: 'materialization_changed' }
    server.use(http.put(endpoint, () => HttpResponse.json({ error: 'unreviewable_drift', code: 'unreviewable_drift', file: 'crew-abc.json' }, { status: 409 })))
    const result = mount(); await ready()
    await review()
    fireEvent.click(screen.getByRole('button', { name: 'Save reviewed changes' }))
    // The reason renders in the footer next to the button it disables, names
    // that button's state by its visible label (the footer reads Review changes
    // again once the preview is gone, so "Save" is nowhere on screen), and names
    // the file without claiming a Reset control the pane does not have.
    const footer = screen.getByTestId('capability-save-footer')
    const refusal = await within(footer).findByText(/crew-abc\.json was edited outside this page in a setting this page cannot show/)
    expect(refusal).toHaveTextContent('Review changes stays turned off')
    expect(refusal.textContent).not.toMatch(/\bSave\b/)
    expect(within(footer).getByText(/then press Reload from server/)).toBeInTheDocument()
    expect(within(screen.getByTestId('capability-scroll-region')).queryByText(/cannot show/)).not.toBeInTheDocument()
    expect(within(footer).getByRole('button', { name: 'Review changes' })).toBeDisabled()
    expect(screen.queryByText(/The saved version changed/)).not.toBeInTheDocument()
    expect(result.saved).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /Reload from server/ }))
    await waitFor(() => expect(within(footer).queryByText(/cannot show/)).not.toBeInTheDocument())
  })

  it('keeps Review disabled with no edit when the saved file is intact', async () => {
    mount(); await ready()
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled()
  })

  it('previews before saving and sends the identical draft with its signed token', async () => {
    const result = mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed')
    expect(saveBodies).toEqual([])
    await review()
    expect(previewBodies).toEqual([{ revision: 'r1', enroll: false, operations: [{ section: 'tools', id: '@search/read', action: 'remove' }], accept_parent: [], accept_members: [] }])
    fireEvent.click(screen.getByRole('button', { name: 'Save reviewed changes' }))
    await waitFor(() => expect(result.saved).toHaveBeenCalledOnce())
    expect(saveBodies).toEqual([{ ...previewBodies[0], preview_token: 'signed-preview' }])
    expect(screen.getByText('Saved. Waiting for a new runtime.')).toBeInTheDocument()
    expect(screen.queryByText('The server reports this version loaded.')).not.toBeInTheDocument()
    expect(result.dirty).toHaveBeenLastCalledWith(false)
  })

  it('warns on review and after save when the member backend cannot apply the spec, and still saves', async () => {
    view = { ...view, backend_warning: null }
    server.use(http.post(`${endpoint}/preview`, async ({ request }) => {
      const body = await request.json() as CapabilityDraft
      previewBodies.push(body)
      return HttpResponse.json({ ...view, backend_warning: { code: 'tool_limits_unsupported', backend: 'kas', setting: 'agent.member_acp_backend', servers: ['search'] }, preview_token: 'signed-preview', impact: [] })
    }), http.put(endpoint, async ({ request }) => {
      saveBodies.push(await request.json() as CapabilityDraft & { preview_token: string })
      view = { ...view, revision: 'r2', backend_warning: { code: 'harness_unsupported', backend: 'codex', setting: 'agent.member_acp_backend' } }
      return HttpResponse.json(view)
    }))
    const result = mount(); await ready()
    expect(screen.queryByTestId('capability-backend-warning')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed')
    await review()
    expect(screen.getByTestId('capability-backend-warning')).toHaveTextContent("Saving works, but new chats with crewA won't start. Their backend (kas) can't turn off individual tools inside the search connection. To fix it, go to MCP Servers and turn that whole connection off, or turn those tools back on.")
    expect(screen.queryByTestId('capability-backend-command')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save reviewed changes' }))
    await waitFor(() => expect(result.saved).toHaveBeenCalledOnce())
    expect(saveBodies).toHaveLength(1)
    const notes = screen.getAllByTestId('capability-backend-warning')
    expect(notes).toHaveLength(1)
    expect(notes[0]).toHaveAttribute('role', 'note')
    expect(notes[0]).toHaveTextContent("Saving works, but new chats with crewA won't start. They use a backend (codex) that ignores saved capabilities. To fix it, run the command below in a terminal, then start a new chat.")
    // The command sits on its own line as a copy button, never inside the sentence.
    expect(within(screen.getByTestId('capability-backend-command')).getByRole('button')).toHaveTextContent('kirocrew config set agent.member_acp_backend kas')
  })

  it('names the second fix when the harness fix would hit the tool limits next', async () => {
    view = { ...view, backend_warning: { code: 'harness_unsupported', backend: 'codex', setting: 'agent.member_acp_backend', servers: ['search'] } }
    mount(); await ready()
    const note = screen.getByTestId('capability-backend-warning')
    expect(within(screen.getByTestId('capability-backend-command')).getByRole('button')).toHaveTextContent('kirocrew config set agent.member_acp_backend kas')
    expect(note).toHaveTextContent('Then go to MCP Servers and turn the search connection off, or turn its tools back on')
    expect(screen.getByTestId('capability-backend-open-tab')).toHaveTextContent('MCP Servers')
  })

  it('scopes a default-backend refusal to Slack, scheduled and spawned work and offers no one-line switch', async () => {
    view = { ...view, backend_warning: { code: 'harness_unsupported', backend: 'claude', setting: 'agent.acp_backend', servers: ['search'] } }
    mount(); await ready()
    const note = screen.getByTestId('capability-backend-warning')
    expect(note).toHaveTextContent("Saving works, but crewA's Slack threads, scheduled jobs and spawned tasks won't start. They use the default backend (claude), which ignores saved capabilities. Chats with crewA are not affected.")
    expect(screen.queryByTestId('capability-backend-command')).not.toBeInTheDocument()
    expect(note).not.toHaveTextContent('Then go to')
    expect(screen.queryByTestId('capability-backend-open-tab')).not.toBeInTheDocument()
  })

  it('scopes a default-backend tool-limit refusal to the same entry points and opens MCP Servers', async () => {
    view = { ...view, backend_warning: { code: 'tool_limits_unsupported', backend: 'kas', setting: 'agent.acp_backend', servers: ['search'] } }
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    const note = screen.getByTestId('capability-backend-warning')
    expect(note).toHaveTextContent("Saving works, but crewA's Slack threads, scheduled jobs and spawned tasks won't start. Their default backend (kas) can't turn off individual tools inside the search connection. Chats with crewA are not affected.")
    expect(note).not.toHaveTextContent('new chats with crewA')
    fireEvent.click(screen.getByTestId('capability-backend-open-tab'))
    expect(screen.getByRole('tab', { name: 'MCP Servers', exact: true })).toHaveAttribute('aria-selected', 'true')
  })

  it('keeps drafts and previews through pane hiding and invalidates preview after an edit', async () => {
    const result = mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed'); await review()
    result.hide(true); result.hide(false)
    expect(screen.getByRole('button', { name: 'Save reviewed changes' })).toBeInTheDocument()
    await pickState('Search read', 'Inherited')
    expect(screen.queryByRole('button', { name: 'Save reviewed changes' })).not.toBeInTheDocument()
    await review()
    expect(previewBodies[1].operations).toEqual([{ section: 'tools', id: '@search/read', action: 'inherit' }])
  })

  it('requires explicit enrollment for an independent snapshot', async () => {
    view.mode = 'legacy_snapshot'
    mount(); await ready()
    expect(screen.getByRole('combobox', { name: 'Source for Search' })).toBeDisabled()
    expect(screen.getByTestId('capability-enroll-to-edit')).toHaveTextContent("Check this box to follow the parent and add this member's own overrides on top. While it is unchecked, the fields below are read-only.")
    expect(screen.queryByText('Tick “Follow the parent template” above to edit.')).toBeNull()
    expect(screen.getByText('Not following parent (own copy)')).toBeInTheDocument()
    const enroll = screen.getByRole('checkbox', { name: 'Follow the parent template' })
    // The guarantee is next to the checkbox, not buried in a long helper.
    expect(enroll.closest('label')).toHaveTextContent('Nothing changes until you save. Following keeps your values and empty fields.')
    expect(enroll).toHaveAccessibleDescription("Nothing changes until you save. Following keeps your values and empty fields. Check this box to follow the parent and add this member's own overrides on top. While it is unchecked, the fields below are read-only.")
    expect(enroll.closest('label')).toHaveAttribute('for', enroll.id)
    expect(enroll.id).toBe('crew-capabilities-enroll')
    fireEvent.click(screen.getByText('Follow the parent template', { selector: 'span' }))
    expect(screen.queryByTestId('capability-enroll-to-edit')).toBeNull()
    // The badge names the SAVED state, so a ticked Follow box beside it reads
    // as a draft, not as a contradiction.
    expect(enroll).toBeChecked()
    expect(screen.getByText('Not following parent (own copy)')).toBeInTheDocument()
    // The pane calls its pending edits a draft everywhere (footer, confirm, stale
    // notice), so the badge uses the same word instead of the editor's generic one.
    expect(screen.getByText('Unsaved draft')).toBeInTheDocument()
    expect(screen.queryByText('Unsaved changes')).toBeNull()
    expect(screen.getByRole('combobox', { name: 'Source for Search' })).not.toBeDisabled()
    await review()
    expect(previewBodies[0].enroll).toBe(true)
    expect(previewBodies[0].operations).toEqual([])
  })

  it('says a shared member runs the parent template as-is and names checking the box as the way to add overrides', async () => {
    view.mode = 'shared'
    mount(); await ready()
    // A shared member runs the parent template's own file with no overrides of
    // its own, so the badge says that rather than "Not following parent", and
    // it must not read as the following badge beside the unchecked Follow box.
    expect(screen.getByText('Parent template as-is (no overrides)')).toBeInTheDocument()
    expect(screen.queryByText('Not following parent')).toBeNull()
    expect(screen.queryByText('Following parent')).toBeNull()
    const enroll = screen.getByRole('checkbox', { name: 'Follow the parent template' })
    expect(enroll).not.toBeChecked()
    expect(screen.getByTestId('capability-enroll-to-edit')).toHaveTextContent("Check this box to follow the parent and add this member's own overrides on top. While it is unchecked, the fields below are read-only.")
    expect(screen.getByRole('combobox', { name: 'Source for Search' })).toBeDisabled()
    fireEvent.click(enroll)
    expect(screen.getByRole('combobox', { name: 'Source for Search' })).not.toBeDisabled()
  })

  it('lets a following member stop following the parent after a confirm', async () => {
    const detachBodies: unknown[] = []
    server.use(http.post(`${endpoint}/detach`, async ({ request }) => {
      detachBodies.push(await request.json())
      view = { ...view, mode: 'legacy_snapshot', revision: 'r2', parent_changes: [] }
      return HttpResponse.json(view)
    }))
    const { saved } = mount(); await ready()
    const stop = screen.getByRole('button', { name: 'Stop following the parent' })
    // The help names the way back, so the button does not read as a one-way door.
    expect(stop).toHaveAccessibleDescription("Keeps the current values as this member's own. Parent changes stop arriving. You can follow the parent again any time.")
    // Keep following closes the confirm and sends nothing.
    fireEvent.click(stop)
    expect(await screen.findByText('Stop following the parent for crewA?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Keep following' }))
    await waitFor(() => expect(screen.queryByText('Stop following the parent for crewA?')).toBeNull())
    expect(detachBodies).toEqual([])
    fireEvent.click(stop)
    // The confirm names where the kept copy is edited afterwards: the crew editor's template pane.
    // The dialog names the way back and where the values are edited afterwards.
    expect(await screen.findByText(/crewA keeps every value it uses now, as its own copy\. Changes to atlas will no longer reach it\. To edit those values later, open the “Built from” tab of this same editor\. Afterwards a “Follow the parent template” checkbox appears on this page, so you can follow atlas again any time\./)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('capability-detach-confirm'))
    await waitFor(() => expect(detachBodies).toEqual([{ revision: 'r1' }]))
    await waitFor(() => expect(saved).toHaveBeenCalled())
    // Back to an independent copy: the way back in is the enroll checkbox again.
    expect(await screen.findByRole('checkbox', { name: 'Follow the parent template' })).not.toBeChecked()
    expect(screen.queryByRole('button', { name: 'Stop following the parent' })).toBeNull()
    expect(saveBodies).toEqual([])
  })

  it('offers stop following only on a clean pane, and never to a member that does not follow', async () => {
    mount(); await ready()
    await pickState('Search', 'Inherited')
    expect(screen.getByRole('button', { name: 'Stop following the parent' })).toBeDisabled()
    expect(screen.getByText('Save or discard your draft first.')).toBeInTheDocument()
  })

  it.each([
    { code: 'detach_tool_limits_unkept' as const, text: 'crewA launches a server with single-tool limits, which only a saved setup can enforce. Remove the limit (set the whole server to allowed), save, then stop following.' },
  ])('disables stop following up front and says why when the view reports $code', async ({ code, text }) => {
    view.detach_blocked = code
    mount(); await ready()
    const stop = screen.getByRole('button', { name: 'Stop following the parent' })
    expect(stop).toBeDisabled()
    expect(stop).toHaveAccessibleDescription(text)
  })

  it('asks the owner to confirm turning the global mcp.json off, and sends the confirmation', async () => {
    view.detach_mcp_json_loss = true
    const detachBodies: unknown[] = []
    server.use(http.post(`${endpoint}/detach`, async ({ request }) => {
      detachBodies.push(await request.json())
      view = { ...view, mode: 'legacy_snapshot', revision: 'r2', detach_mcp_json_loss: false }
      return HttpResponse.json(view)
    }))
    mount(); await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Stop following the parent' }))
    expect(await screen.findByTestId('capability-detach-mcp-json')).toHaveTextContent('crewA also loads servers from the global mcp.json. Stopping turns that off for its own copy, so it stops loading them.')
    fireEvent.click(screen.getByTestId('capability-detach-confirm'))
    await waitFor(() => expect(detachBodies).toEqual([{ revision: 'r1', accept_mcp_json: true }]))
  })

  it('sends no confirmation, and shows no warning, for a member that does not read the global mcp.json', async () => {
    const detachBodies: unknown[] = []
    server.use(http.post(`${endpoint}/detach`, async ({ request }) => {
      detachBodies.push(await request.json())
      view = { ...view, mode: 'legacy_snapshot', revision: 'r2' }
      return HttpResponse.json(view)
    }))
    mount(); await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Stop following the parent' }))
    await screen.findByTestId('capability-detach-confirm')
    expect(screen.queryByTestId('capability-detach-mcp-json')).toBeNull()
    fireEvent.click(screen.getByTestId('capability-detach-confirm'))
    await waitFor(() => expect(detachBodies).toEqual([{ revision: 'r1' }]))
  })

  it('disables stop following after a refusal that names a save, with the reason as its help', async () => {
    server.use(http.post(`${endpoint}/detach`, () => HttpResponse.json({ error: 'detach_removal_unkept', code: 'detach_removal_unkept' }, { status: 409 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Stop following the parent' }))
    fireEvent.click(await screen.findByTestId('capability-detach-confirm'))
    const text = 'crewA removed something from a template Crew owns, and the copy would get it back. Set each removed row back to Inherited, save, then stop following.'
    await waitFor(() => expect(screen.getByRole('button', { name: 'Stop following the parent' })).toBeDisabled())
    expect(await screen.findByText(text)).toBeInTheDocument()
    // The help line ("follow again any time") would contradict the notice, so it steps aside.
    expect(screen.queryByText(/follow the parent again any time/)).toBeNull()
  })

  it('shows no stop following control on an independent member', async () => {
    view.mode = 'shared'
    mount(); await ready()
    expect(screen.queryByRole('button', { name: 'Stop following the parent' })).toBeNull()
  })

  it('says a refused stop following changed nothing, without a raw code, and keeps the member following', async () => {
    server.use(http.post(`${endpoint}/detach`, () => HttpResponse.json({ error: 'materialization_pending', code: 'materialization_pending' }, { status: 409 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Stop following the parent' }))
    fireEvent.click(await screen.findByTestId('capability-detach-confirm'))
    expect(await screen.findByText('Stopping was refused. Nothing changed and the member still follows the parent. Reload and try again.')).toBeInTheDocument()
    expect(screen.queryByText(/materialization_pending/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop following the parent' })).toBeInTheDocument()
  })

  it.each(['materialization_changed', 'foreign_private_copy', 'stale_revision'])('shows no draft advice for a refused stop following with %s', async (code) => {
    server.use(http.post(`${endpoint}/detach`, () => HttpResponse.json({ error: code, code }, { status: 409 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Stop following the parent' }))
    fireEvent.click(await screen.findByTestId('capability-detach-confirm'))
    expect(await screen.findByText('Stopping was refused. Nothing changed and the member still follows the parent. Reload and try again.')).toBeInTheDocument()
    expect(screen.queryByText(new RegExp(code))).toBeNull()
    expect(screen.queryByText(/Your draft is kept/)).toBeNull()
  })

  it.each([
    { code: 'detach_tool_limits_unkept', text: 'crewA launches a server with single-tool limits, which only a saved setup can enforce. Remove the limit (set the whole server to allowed), save, then stop following.' },
    { code: 'detach_removal_unkept', text: 'crewA removed something from a template Crew owns, and the copy would get it back. Set each removed row back to Inherited, save, then stop following.' },
  ])('names the save that clears a $code refusal instead of draft advice', async ({ code, text }) => {
    server.use(http.post(`${endpoint}/detach`, () => HttpResponse.json({ error: code, code }, { status: 409 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Stop following the parent' }))
    fireEvent.click(await screen.findByTestId('capability-detach-confirm'))
    // The reason is shown once, in the error notice; error text stays out of the help line.
    expect(await screen.findByText(text)).toBeInTheDocument()
    // No draft exists when stopping is refused, so the draft-refusal copy must not show.
    expect(screen.queryByText(/Your draft is kept/)).toBeNull()
    expect(screen.queryByText(new RegExp(code))).toBeNull()
    // The reason names a save, so the button waits for it instead of inviting a second refusal.
    expect(screen.getByRole('button', { name: 'Stop following the parent' })).toBeDisabled()
  })

  it('keeps tool exposure, agent approvals and MCP approvals separate', async () => {
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Auto-Approved' }))
    await pickState('Search read', 'Removed')
    await pickState('Search list', 'Override')
    await review()
    expect(previewBodies[0].operations).toEqual([
      { section: 'allowedTools', id: '@search/read', action: 'remove' },
      { section: 'autoApprove', id: '@search/list', action: 'set', value: true },
    ])
    expect(screen.getByText('This change grants more automatic approval.')).toBeInTheDocument()
  })

  it('names each approval the save will withhold, and why, before the save', async () => {
    server.use(http.post(`${endpoint}/preview`, () => HttpResponse.json({ ...view, preview_token: 'signed-preview', impact: [], withheld_approvals: [
      { section: 'autoApprove', id: '@search/list', reason: 'governed' },
      { section: 'allowedTools', id: '@absent/tool', reason: 'not_connected' },
      { section: 'autoApprove', id: '@kirocrew-mate/ask', reason: 'managed' },
      { section: 'autoApprove', id: '@other/ask', reason: 'unexplained' },
    ] })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Auto-Approved' }))
    await pickState('Search list', 'Override')
    fireEvent.change(screen.getByLabelText('Exact tool reference, such as @server/tool'), { target: { value: '@absent/tool' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to draft' }))
    await review()
    const note = screen.getByTestId('capability-preview-withheld')
    expect(note).toHaveTextContent('Saving will not auto-approve these tools for crewA, so calls to them will still ask for approval.')
    expect(note).toHaveTextContent('@search/list: A security profile governs this server, so Kiro Crew withholds the approval.')
    expect(note).toHaveTextContent('@absent/tool: crewA has no enabled connection to this server. Add or enable it in the MCP Servers tab.')
    expect(note).toHaveTextContent('@kirocrew-mate/ask: Kiro Crew rebuilds this connection itself, and the rebuilt connection keeps no approvals.')
    // The note leads the review, so the per-member summary follows from it.
    const members = screen.getByTestId('capability-preview-members')
    expect(note.compareDocumentPosition(members) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The not-connected fix opens the tab it names.
    expect(note).toHaveTextContent('@other/ask: Kiro Crew will not save this approval, and no known setting explains why. Report this as a bug.')
    // The member name renders as code, and a fully withheld draft's summary says why nothing changes.
    expect(within(note).getAllByText('crewA', { selector: 'code' }).length).toBeGreaterThan(0)
    expect(members).toHaveTextContent('crewA: saving changes nothing, because none of these approvals can be kept.')
    expect(members).not.toHaveTextContent('no effective value changes')
    fireEvent.click(within(note).getByRole('button', { name: 'Open the MCP Servers tab (keeps your draft)' }))
    expect(screen.getByRole('tab', { name: 'MCP Servers', exact: true })).toHaveAttribute('aria-selected', 'true')
  })

  it('names a refused approval reference instead of the generic retry copy', async () => {
    server.use(http.post(`${endpoint}/preview`, () => HttpResponse.json({ error: 'invalid_approval_ref', code: 'invalid_approval_ref', ref: '@search' }, { status: 400 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Auto-Approved' }))
    fireEvent.click(screen.getByRole('combobox', { name: 'Approval list' }))
    fireEvent.click(await screen.findByRole('option', { name: 'MCP server approval list' }))
    fireEvent.change(screen.getByLabelText('Exact tool reference, such as @server/tool'), { target: { value: '@search' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to draft' }))
    fireEvent.click(screen.getByRole('button', { name: 'Review changes', exact: true }))
    expect(await screen.findByText(/^@search is not a valid approval entry\. An MCP server approval needs the @server\/tool form/)).toBeInTheDocument()
    expect(screen.queryByText('The capabilities request failed. Your draft is kept. Reload from server and retry.')).toBeNull()
  })

  it('names the code of any other refused draft shape', async () => {
    server.use(http.post(`${endpoint}/preview`, () => HttpResponse.json({ error: 'invalid_set_value', code: 'invalid_set_value' }, { status: 400 })))
    mount(); await ready()
    await pickState('Search', 'Removed')
    fireEvent.click(screen.getByRole('button', { name: 'Review changes', exact: true }))
    expect(await screen.findByText('The server refused this draft (invalid_set_value). Your draft is kept. Reloading will not change this: edit or discard the draft.')).toBeInTheDocument()
  })

  it('hides an empty approval heading without removing the list choice or draft rows', async () => {
    view.rows = view.rows.filter(row => row.section !== 'autoApprove')
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Auto-Approved' }))
    expect(screen.queryByText('MCP server approval list')).toBeNull()
    const hint = screen.getByText('Stored on this agent. Matches tool names, @server, @server/tool, or patterns.')
    expect(hint).toHaveAttribute('aria-live', 'polite')
    fireEvent.click(screen.getByRole('combobox', { name: 'Approval list' }))
    fireEvent.click(await screen.findByRole('option', { name: 'MCP server approval list' }))
    expect(hint).toHaveTextContent('Stored on one MCP connection. Matches @server/tool.')
    expect(screen.queryByText('Stored on this agent. Matches tool names, @server, @server/tool, or patterns.')).toBeNull()
    fireEvent.change(screen.getByLabelText('Exact tool reference, such as @server/tool'), { target: { value: '@search/new' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to draft' }))
    expect(screen.getByTestId('capability-autoApprove-@search/new')).toBeInTheDocument()
    // The selected choice and the now-populated section both name the list.
    expect(screen.getAllByText('MCP server approval list')).toHaveLength(2)
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'autoApprove', id: '@search/new', action: 'set', value: true }])
  })

  it('selects configured connections without copying transport secrets', async () => {
    mount(); await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Add configured MCP connection' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Configured search' }))
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'configured', action: 'set', connection_id: 'configured' }])
  })

  it('offers searchable skills and preserves manual resource references', async () => {
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Skills', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Add a skill from the catalog' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Test skill' }))
    await pickState('Review skill', 'Override')
    fireEvent.click(screen.getByText('Advanced references and template fields'))
    expect(screen.getByText('file:///manual/*.md')).toBeInTheDocument()
    await review()
    expect(previewBodies[0].operations).toEqual([
      { section: 'skills', id: 'catalog/test', action: 'set', value: true },
      { section: 'skills', id: 'catalog/review', action: 'set', value: true },
    ])
  })

  it('edits whole transports and never serializes a redaction mask', async () => {
    mount(); await ready()
    const protectedRow = within(screen.getByTestId('capability-mcpServers-protected'))
    fireEvent.click(protectedRow.getByText('Connection settings'))
    expect(protectedRow.getByLabelText('Command')).not.toBeDisabled()
    fireEvent.click(protectedRow.getByText('Replace connection settings', { selector: 'summary' }))
    fireEvent.click(protectedRow.getByRole('button', { name: 'Replace connection settings' }))
    fireEvent.change(protectedRow.getByLabelText('Command'), { target: { value: 'new-cli' } })
    fireEvent.click(protectedRow.getByRole('button', { name: 'Add argument' }))
    fireEvent.change(protectedRow.getByLabelText('Argument 1'), { target: { value: '--quiet' } })
    fireEvent.click(protectedRow.getByRole('button', { name: 'Add argument' }))
    fireEvent.change(protectedRow.getByLabelText('Argument 2'), { target: { value: '--read' } })
    fireEvent.click(protectedRow.getByRole('button', { name: 'Add variable' }))
    fireEvent.change(protectedRow.getByLabelText('Variable name'), { target: { value: 'REGION' } })
    fireEvent.change(protectedRow.getByLabelText('Variable value'), { target: { value: 'test-region' } })
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'protected', action: 'set', value: { command: 'new-cli', args: ['--quiet', '--read'], env: { REGION: 'test-region' } } }])
    expect(JSON.stringify(previewBodies)).not.toContain('REDACTED')
    expect(screen.getByRole('combobox', { name: 'Source for Managed' })).toBeEnabled()
    expect(within(screen.getByTestId('capability-mcpServers-managed')).getByRole('checkbox', { name: 'Enabled' })).toBeEnabled()
  })

  it('sends selected parent changes and explicit member scope together', async () => {
    mount(); await ready()
    fireEvent.click(screen.getByText('Incoming parent updates'))
    // The hint must not promise that accepting replaces a local value: the
    // resolver keeps explicit rows until Inherited or Restore from parent.
    expect(screen.getByText('Ticked updates leave the pending list on save. Each label says what this member keeps or takes.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /tools: @search\/read/ }))
    // Members come from the declared roster; the current member is never offered.
    const scope = within(screen.getByRole('group', { name: /Also apply the ticked parent updates/ }))
    expect(scope.queryByRole('checkbox', { name: 'crewA' })).toBeNull()
    // The hint says why a name is listed (declared) and leaves eligibility to
    // the server; it never claims a listed member is eligible.
    expect(scope.getByText('Declared crew members. Eligibility for this same parent is verified when you review.')).toBeInTheDocument()
    fireEvent.click(scope.getByRole('checkbox', { name: 'crewB' }))
    fireEvent.click(scope.getByRole('checkbox', { name: 'crewC' }))
    fireEvent.click(scope.getByRole('checkbox', { name: 'crewC' }))
    fireEvent.click(scope.getByRole('checkbox', { name: 'crewC' }))
    await review()
    expect(previewBodies[0].accept_parent).toEqual([{ section: 'tools', id: '@search/read' }])
    expect(previewBodies[0].accept_members).toEqual(['crewB', 'crewC'])
    // The preview names every covered member, including one with no effective row.
    const summary = within(screen.getByTestId('capability-preview-members'))
    expect(summary.getByText('crewA: no effective value changes.')).toBeInTheDocument()
    expect(summary.getByText(/^crewB: no effective value changes/)).toBeInTheDocument()
    expect(summary.getByText(/^crewC: no effective value changes/)).toBeInTheDocument()
    // Changing the member selection invalidates the signed preview.
    fireEvent.click(scope.getByRole('checkbox', { name: 'crewC' }))
    expect(screen.queryByRole('button', { name: 'Save reviewed changes' })).not.toBeInTheDocument()
  })

  it('names the edited member as unchanged when only another member gets a row', async () => {
    server.use(http.post(`${endpoint}/preview`, async ({ request }) => {
      const body = await request.json() as CapabilityDraft
      previewBodies.push(body)
      return HttpResponse.json({ ...view, preview_token: 'signed-preview', impact: [{ section: 'tools', id: '@search/read', action: 'set', member: 'crewB', change: 'changed', approval_expanded: false }] })
    }))
    mount(); await ready()
    fireEvent.click(screen.getByText('Incoming parent updates'))
    fireEvent.click(screen.getByRole('checkbox', { name: /tools: @search\/read/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'crewB' }))
    await review()
    const summary = within(screen.getByTestId('capability-preview-members'))
    expect(summary.getByText(/^crewA: no effective value changes/)).toBeInTheDocument()
    expect(summary.getByText('crewB: 1 change listed below.')).toBeInTheDocument()
  })

  it('states the outcome on a ticked conflict row and names the kept row in the receipt', async () => {
    // A conflict means this member overrides the row locally.
    view.rows[0] = { ...view.rows[0], state: 'local' }
    mount(); await ready()
    fireEvent.click(screen.getByText('Incoming parent updates'))
    const conflict = screen.getByRole('checkbox', { name: /Keep your override: dismiss this parent update on save/ })
    expect(conflict).toHaveAccessibleName('Keep your override: dismiss this parent update on save tools: @search/read')
    expect(conflict).not.toBeChecked()
    fireEvent.click(conflict)
    expect(conflict).toBeChecked()
    // The checkbox names the operation rather than implying it replaces an override.
    expect(conflict).toHaveAccessibleName(/Keep your override/)
    const label = screen.getByText('Keep your override: dismiss this parent update on save')
    expect(label).toHaveAttribute('aria-live', 'polite')
    expect(label.parentElement).toHaveClass('break-words')
    expect(label.parentElement).not.toHaveClass('break-all')
    expect(screen.queryByText(/Accept this parent change|Source Override:|Selected: save accepts/)).toBeNull()
    fireEvent.click(conflict)
    expect(conflict).not.toBeChecked()
    expect(conflict).toHaveAccessibleName(/Keep your override/)
    fireEvent.click(conflict)
    fireEvent.click(screen.getByRole('checkbox', { name: 'crewB' }))
    await review()
    // The receipt derives the kept row from the reviewed selection, the
    // server's conflict flag and the sanitized preview row (still local, no
    // impact). It names the reference, never a value.
    const summary = within(screen.getByTestId('capability-preview-members'))
    expect(summary.getByText('crewA: no effective value changes.')).toBeInTheDocument()
    expect(summary.getByText('crewA keeps these overrides.')).toBeInTheDocument()
    const kept = within(summary.getByTestId('capability-preview-kept-rows'))
    expect(kept.getAllByRole('listitem')).toHaveLength(1)
    expect(kept.getByText('tools: @search/read')).toBeInTheDocument()
    expect(kept.getByText('Override')).toBeInTheDocument()
    // Peer members get no invented kept-row details: the response carries no
    // projection for them.
    expect(summary.getByText(/^crewB: no effective value changes/)).toBeInTheDocument()
    expect(summary.getAllByTestId('capability-preview-kept-rows')).toHaveLength(1)
    expect(summary.queryByText(/^crewB keeps/)).toBeNull()
    // Accept semantics are untouched: the same request body as before.
    expect(previewBodies[0].accept_parent).toEqual([{ section: 'tools', id: '@search/read' }])
    expect(previewBodies[0].operations).toEqual([])
  })

  it('states that a saved removal stays on a ticked conflict row and lists it as a kept Removed row', async () => {
    // A saved local removal is an override too: accepting advances the
    // followed Parent version and the row stays removed.
    view.rows[0] = { ...view.rows[0], state: 'removed', present: false, value: null }
    mount(); await ready()
    fireEvent.click(screen.getByText('Incoming parent updates'))
    const box = screen.getByRole('checkbox', { name: /Keep it removed: dismiss this parent update on save/ })
    expect(box).not.toBeChecked()
    fireEvent.click(box)
    expect(box).toBeChecked()
    expect(box).toHaveAccessibleName(/Keep it removed/)
    expect(box).not.toHaveAccessibleName(/take the parent value/)
    fireEvent.click(screen.getByRole('checkbox', { name: 'crewB' }))
    await review()
    // The kept row comes from the sanitized preview projection (still
    // removed, absent, no impact entry) and names its state, never a value.
    const summary = within(screen.getByTestId('capability-preview-members'))
    expect(summary.getByText('crewA: no effective value changes.')).toBeInTheDocument()
    expect(summary.getByText('crewA keeps these overrides.')).toBeInTheDocument()
    const kept = within(summary.getByTestId('capability-preview-kept-rows'))
    expect(kept.getAllByRole('listitem')).toHaveLength(1)
    expect(kept.getByText('tools: @search/read')).toBeInTheDocument()
    expect(kept.getByText('Stays removed')).toBeInTheDocument()
    expect(kept.queryByText('Override')).toBeNull()
    expect(summary.getAllByTestId('capability-preview-kept-rows')).toHaveLength(1)
    expect(summary.queryByText(/^crewB keeps/)).toBeNull()
    // Accept semantics are untouched: no operation is invented for the row.
    expect(previewBodies[0].accept_parent).toEqual([{ section: 'tools', id: '@search/read' }])
    expect(previewBodies[0].operations).toEqual([])
  })

  it('states that a drafted removal stays when the same draft accepts the conflicting parent change', async () => {
    view.rows[0] = { ...view.rows[0], state: 'local' }
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed')
    fireEvent.click(screen.getByText('Incoming parent updates'))
    // The draft's own remove decides the action label, not the saved state.
    const box = screen.getByRole('checkbox', { name: /Keep it removed: dismiss this parent update on save/ })
    fireEvent.click(box)
    expect(box).toHaveAccessibleName(/Keep it removed/)
    await review()
    // The request carries the removal and the acceptance side by side.
    expect(previewBodies[0].operations).toEqual([{ section: 'tools', id: '@search/read', action: 'remove' }])
    expect(previewBodies[0].accept_parent).toEqual([{ section: 'tools', id: '@search/read' }])
  })

  it('says accepting follows a parent removal when the draft sets the row to Inherited, and lists no kept row', async () => {
    view.rows[0] = { ...view.rows[0], state: 'local' }
    view.parent_changes = [{ section: 'tools', id: '@search/read', kind: 'removed', conflict: true, requires_approval: true, before: true, after: null }]
    server.use(http.post(`${endpoint}/preview`, async ({ request }) => {
      const body = await request.json() as CapabilityDraft
      previewBodies.push(body)
      const rows = view.rows.map(row => row.section === 'tools' && row.id === '@search/read' ? { ...row, state: 'inherited' as const, present: false, value: null } : row)
      return HttpResponse.json({ ...view, rows, preview_token: 'signed-preview', impact: [{ section: 'tools', id: '@search/read', member: 'crewA', change: 'removed', approval_expanded: false }] })
    }))
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Inherited')
    fireEvent.click(screen.getByText('Incoming parent updates'))
    fireEvent.click(screen.getByRole('checkbox', { name: /tools: @search\/read/ }))
    const box = screen.getByRole('checkbox', { name: /Include this update for the selected members/ })
    // The hint covers removals as well as values; inheritance does not depend on the tick.
    expect(box).toHaveAccessibleDescription(/On save, crewA uses the parent's current value or removal.*This checkbox only selects the update for the other members below/)
    expect(box).toBeChecked()
    await review()
    const summary = within(screen.getByTestId('capability-preview-members'))
    expect(summary.getByText('crewA: 1 change listed below.')).toBeInTheDocument()
    expect(summary.queryByTestId('capability-preview-kept-rows')).toBeNull()
    expect(summary.queryByText(/keeps these overrides/)).toBeNull()
    // The impact list names the removal from the sanitized response.
    const region = within(screen.getByRole('region', { name: 'Changes to be saved' }))
    expect(region.getByText('crewA: tools: @search/read')).toBeInTheDocument()
    expect(region.getByText('Removed', { selector: 'p' })).toBeInTheDocument()
    expect(previewBodies[0].operations).toEqual([{ section: 'tools', id: '@search/read', action: 'inherit' }])
    expect(previewBodies[0].accept_parent).toEqual([{ section: 'tools', id: '@search/read' }])
  })

  it('switches the conflict outcome when the draft sets the row to Inherited, and lists no kept row', async () => {
    view.rows[0] = { ...view.rows[0], state: 'local' }
    server.use(http.post(`${endpoint}/preview`, async ({ request }) => {
      const body = await request.json() as CapabilityDraft
      previewBodies.push(body)
      const rows = view.rows.map(row => row.section === 'tools' && row.id === '@search/read' ? { ...row, state: 'inherited' as const } : row)
      return HttpResponse.json({ ...view, rows, preview_token: 'signed-preview', impact: [{ section: 'tools', id: '@search/read', member: 'crewA', change: 'changed', approval_expanded: false }] })
    }))
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Inherited')
    fireEvent.click(screen.getByText('Incoming parent updates'))
    fireEvent.click(screen.getByRole('checkbox', { name: /tools: @search\/read/ }))
    expect(screen.getByRole('checkbox', { name: /Include this update for the selected members/ })).toHaveAccessibleDescription(/On save, crewA uses the parent's current value or removal.*This checkbox only selects the update for the other members below/)
    await review()
    const summary = within(screen.getByTestId('capability-preview-members'))
    expect(summary.getByText('crewA: 1 change listed below.')).toBeInTheDocument()
    expect(summary.queryByTestId('capability-preview-kept-rows')).toBeNull()
  })

  it('never labels an accepted non-conflict row as a kept local value', async () => {
    view.parent_changes = [{ section: 'autoApprove', id: '@search/list', kind: 'changed', conflict: false, requires_approval: true, before: true, after: true }]
    mount(); await ready()
    fireEvent.click(screen.getByText('Incoming parent updates'))
    const box = screen.getByRole('checkbox', { name: /autoApprove: @search\/list/ })
    expect(box).toHaveAccessibleName(/Take the parent value: accept this update on save/)
    expect(box).not.toBeChecked()
    fireEvent.click(box)
    expect(box).toBeChecked()
    expect(box).toHaveAccessibleName(/Take the parent value: accept this update on save/)
    expect(box).not.toHaveAccessibleName(/override stays/)
    await review()
    const summary = within(screen.getByTestId('capability-preview-members'))
    expect(summary.getByText(/^crewA: no effective value changes/)).toBeInTheDocument()
    expect(summary.queryByTestId('capability-preview-kept-rows')).toBeNull()
    expect(summary.queryByText(/keeps these overrides/)).toBeNull()
  })

  it.each([
    { source: 'Override', action: 'set', label: 'Keep your override: dismiss this parent update on save' },
    { source: 'Removed', action: 'remove', label: 'Keep it removed: dismiss this parent update on save' },
    { source: 'Inherited', action: 'inherit', label: 'Include this update for the selected members' },
  ] as const)('uses draft Source $source even when the server reported no conflict and acceptance is unchecked', async ({ source, action, label }) => {
    view.rows[0] = { ...view.rows[0], state: source === 'Inherited' ? 'local' : 'inherited' }
    view.parent_changes[0] = { ...view.parent_changes[0], conflict: false }
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', source)
    fireEvent.click(screen.getByText('Incoming parent updates'))
    const box = screen.getByRole('checkbox', { name: /tools: @search\/read/ })
    expect(box).not.toBeChecked()
    expect(box).toHaveAccessibleName(`${label} tools: @search/read`)
    if (action === 'inherit') expect(box).toHaveAccessibleDescription(/This checkbox only selects the update for the other members below/)
    else expect(box).not.toHaveAttribute('aria-describedby')
    fireEvent.click(box)
    expect(box).toBeChecked()
    expect(box).toHaveAccessibleName(`${label} tools: @search/read`)
    fireEvent.click(box)
    expect(box).not.toBeChecked()
    expect(box).toHaveAccessibleName(`${label} tools: @search/read`)
    await review()
    expect(previewBodies[0].accept_parent).toEqual([])
    expect(previewBodies[0].operations).toEqual([
      { section: 'tools', id: '@search/read', action, ...(action === 'set' ? { value: true } : {}) },
    ])
  })

  it('keeps an explicit draft inheritance of Parent removal effective without selecting acceptance', async () => {
    view.rows[0] = { ...view.rows[0], state: 'local' }
    view.parent_changes[0] = { ...view.parent_changes[0], kind: 'removed', after: null }
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Inherited')
    fireEvent.click(screen.getByText('Incoming parent updates'))
    expect(screen.getByRole('checkbox', { name: /tools: @search\/read/ })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: /Include this update for the selected members/ })).toHaveAccessibleDescription(/On save, crewA uses the parent's current value or removal.*This checkbox only selects the update for the other members below/)
    await review()
    expect(previewBodies[0].accept_parent).toEqual([])
    expect(previewBodies[0].operations).toEqual([{ section: 'tools', id: '@search/read', action: 'inherit' }])
  })

  it.each(['added', 'changed', 'removed'] as const)('distinguishes Parent %s from Source and impact labels', async kind => {
    view.parent_changes[0] = { ...view.parent_changes[0], kind, conflict: false, after: kind === 'removed' ? null : true }
    mount(); await ready()
    fireEvent.click(screen.getByText('Incoming parent updates'))
    const box = screen.getByRole('checkbox', { name: /tools: @search\/read/ })
    expect(screen.getByText(`Parent ${kind} this`)).toBeInTheDocument()
    const action = kind === 'removed' ? 'Follow the parent removal: this row is removed on save' : 'Take the parent value: accept this update on save'
    expect(box).toHaveAccessibleName(`${action} tools: @search/read`)
    expect(box).not.toBeChecked()
    fireEvent.click(box)
    expect(box).toBeChecked()
    expect(box).toHaveAccessibleName(`${action} tools: @search/read`)
    await review()
    expect(previewBodies[0].operations).toEqual([])
    expect(previewBodies[0].accept_parent).toEqual([{ section: 'tools', id: '@search/read' }])
  })

  it('keeps hidden values in the draft after cancel and restores them after confirmed discard', async () => {
    mount(); await ready()
    const row = within(screen.getByTestId('capability-mcpServers-protected'))
    fireEvent.click(row.getByText('Connection settings'))
    const input = row.getByLabelText('Variable value')
    expect(input).toHaveAttribute('placeholder', 'Hidden value kept. Type to replace.')
    expect(row.getByText(/Your saved hidden value stays unchanged until you save a replacement/)).toBeInTheDocument()
    fireEvent.change(input, { target: { value: 'draft-replacement' } })
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(input).toHaveValue('draft-replacement')
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard draft' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', 'Hidden value kept. Type to replace.')
    expect(saveBodies).toEqual([])
  })

  it('uses the plural receipt for multiple effective changes', async () => {
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed')
    fireEvent.click(screen.getByRole('tab', { name: 'Auto-Approved', exact: true }))
    await pickState('Search read', 'Removed')
    await review()
    expect(within(screen.getByTestId('capability-preview-members')).getByText('crewA: 2 changes listed below.')).toBeInTheDocument()
  })

  it('shows each row state once, in the source select, never as a duplicate badge', async () => {
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    const row = within(screen.getByTestId('capability-tools-@search/read'))
    expect(row.getByRole('combobox', { name: 'Source for Search read' })).toHaveTextContent('Inherited')
    expect(row.getAllByText('Inherited')).toHaveLength(1)
  })

  it('explains the three source states once above the rows, never per row', async () => {
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    const legend = screen.getAllByTestId('capability-state-legend')
    expect(legend).toHaveLength(1)
    expect(legend[0]).toHaveTextContent("Inherited follows the parent's accepted value. Override sets this member's own value. Removed drops it for this member.")
    // The legend is a plain paragraph before the row list, not a row control.
    expect(legend[0].tagName).toBe('P')
    expect(within(screen.getByTestId('capability-tools-@search/read')).queryByText(/accepted value/)).toBeNull()
  })

  it('keeps the draft after stale save, reloads and requires a fresh preview', async () => {
    server.use(http.put(endpoint, () => HttpResponse.json({ code: 'stale_revision' }, { status: 409 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed'); await review()
    fireEvent.click(screen.getByRole('button', { name: 'Save reviewed changes' }))
    await screen.findByText(/The saved version changed/)
    expect(screen.queryByRole('button', { name: 'Save reviewed changes' })).not.toBeInTheDocument()
    view = { ...view, revision: 'r3' }
    fireEvent.click(screen.getByRole('button', { name: 'Reload from server (keeps your draft)' }))
    await screen.findByText('Review version: r3')
    fireEvent.click(screen.getByRole('button', { name: 'Use the new version for this draft' }))
    await review()
    expect(previewBodies[1].revision).toBe('r3')
    expect(previewBodies[1].operations).toEqual(previewBodies[0].operations)
  })

  it('names a permissions refusal on review instead of sending the user round a reload loop', async () => {
    server.use(http.post(`${endpoint}/preview`, () => HttpResponse.json({ error: 'alternate_permissions_require_review', code: 'alternate_permissions_require_review' }, { status: 409 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed')
    fireEvent.click(screen.getByRole('button', { name: 'Review changes' }))
    await screen.findByText(/This page cannot change approvals for crewA: its agent file in ~\/\.kiro\/agents has hand-written permission settings/)
    expect(screen.queryByText(/The saved version changed/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use the new version for this draft' })).not.toBeInTheDocument()
  })

  it('reports any other non-stale 409 by its code, not as a changed version', async () => {
    server.use(http.post(`${endpoint}/preview`, () => HttpResponse.json({ error: 'approval_exclusion_overlaps', code: 'approval_exclusion_overlaps' }, { status: 409 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed')
    fireEvent.click(screen.getByRole('button', { name: 'Review changes' }))
    await screen.findByText('The server refused this draft (approval_exclusion_overlaps). Your draft is kept. Reloading will not change this: edit or discard the draft.')
    expect(screen.queryByText(/The saved version changed/)).not.toBeInTheDocument()
  })

  it('keeps the load copy for a 409 on the load itself, where there may be no draft to refuse', async () => {
    server.use(http.get(endpoint, () => HttpResponse.json({ error: 'parent_missing', code: 'parent_missing' }, { status: 409 })))
    mount()
    await screen.findByText(/The saved version changed/)
    expect(screen.queryByText(/refused this draft/)).not.toBeInTheDocument()
  })

  it.each(['stale_revision', 'stale_preview', 'stale_binding', 'governance_changed', 'source_changed', 'parent_identity_changed', 'project_identity_changed', 'source_identity_changed', 'materialization_changed'])('keeps the reload copy for the stale code %s', async code => {
    server.use(http.post(`${endpoint}/preview`, () => HttpResponse.json({ error: code, code }, { status: 409 })))
    mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed')
    fireEvent.click(screen.getByRole('button', { name: 'Review changes' }))
    await screen.findByText(/The saved version changed/)
  })

  it('confirms before discarding every draft change and leaves the server untouched', async () => {
    const result = mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed')
    fireEvent.click(screen.getByText('Incoming parent updates'))
    fireEvent.click(screen.getByRole('checkbox', { name: /tools: @search\/read/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'crewB' }))
    await review()
    const discard = screen.getByRole('button', { name: 'Discard draft' })
    discard.focus()
    fireEvent.click(discard)
    const confirm = await screen.findByRole('dialog', { name: 'Discard the Capabilities draft?' })
    expect(confirm).toHaveTextContent('Saved values and hidden credentials are unchanged. No other member is affected.')
    expect(result.dirty).toHaveBeenLastCalledWith(true)
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Save reviewed changes' })).toBeEnabled()
    expect(screen.getByRole('checkbox', { name: 'crewB' })).toBeChecked()
    expect(discard).toHaveFocus()
    fireEvent.click(discard)
    fireEvent.click(within(await screen.findByRole('dialog', { name: 'Discard the Capabilities draft?' })).getByRole('button', { name: 'Discard draft' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(result.dirty).toHaveBeenLastCalledWith(false)
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Source for Search read' })).toHaveTextContent('Inherited')
    expect(screen.getByRole('checkbox', { name: 'crewB' })).not.toBeChecked()
    expect(previewBodies).toHaveLength(1); expect(saveBodies).toEqual([])
  })

  it('blocks a missing parent and reports an unsupported backend honestly', async () => {
    view.template.available = false
    const result = mount(); await ready()
    expect(screen.getByText(/The parent template is missing/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled()
    result.unmount()
    server.use(http.get(endpoint, () => new HttpResponse(null, { status: 501 })))
    mount()
    await screen.findByText('This server does not support the capabilities editor.')
  })

  it('locks managed transport fields while allowing the supported enable switch', async () => {
    mount(); await ready()
    const managed = within(screen.getByTestId('capability-mcpServers-managed'))
    fireEvent.click(managed.getByText('Connection settings'))
    expect(managed.getByLabelText('Command')).toBeDisabled()
    expect(managed.getByRole('combobox', { name: 'Connection settings' })).toBeDisabled()
    expect(managed.queryByRole('button', { name: 'Replace connection settings' })).not.toBeInTheDocument()
    fireEvent.click(managed.getByRole('checkbox', { name: 'Enabled' }))
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'managed', action: 'set', value: { disabled: true } }])
  })

  it.each([
    { url: '[REDACTED]', args: '[REDACTED]', env: '[REDACTED]', headers: '[REDACTED]' },
    { url: 'https://example.test/mcp', args: ['--read'], env: { TOKEN: '[REDACTED]' }, headers: { Authorization: '[REDACTED]' } },
  ])('keeps masked remote transports untouched when selecting their configured connection (%#)', async transport => {
    view.rows.push({ section: 'mcpServers', id: 'remote', label: 'Remote', state: 'local', present: true, value: transport })
    view.connections.push({ id: 'remote', label: 'Configured remote', managed: false })
    mount(); await ready()
    const remote = within(screen.getByTestId('capability-mcpServers-remote'))
    fireEvent.click(remote.getByText('Connection settings'))
    expect(remote.getByLabelText('Server URL')).not.toBeDisabled()
    expect(remote.getByLabelText('Server URL')).toHaveValue(transport.url === '[REDACTED]' ? '' : transport.url)
    const headerValues = remote.queryAllByLabelText('Header value')
    expect(headerValues).toHaveLength(typeof transport.headers === 'string' ? 0 : 1)
    for (const input of headerValues) { expect(input).not.toBeDisabled(); expect(input).toHaveValue(''); expect(input).toHaveAttribute('placeholder', 'Hidden value kept. Type to replace.') }
    expect(previewBodies).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: 'Add configured MCP connection' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Configured remote' }))
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'remote', action: 'set', connection_id: 'remote' }])
    expect(JSON.stringify(previewBodies)).not.toContain('REDACTED')
  })

  it('blocks a reviewed save when a background read sees a newer version', async () => {
    const result = mount(); await ready()
    fireEvent.click(screen.getByRole('tab', { name: 'Tools', exact: true }))
    await pickState('Search read', 'Removed'); await review()
    view = { ...view, revision: 'r4' }
    await act(async () => { await result.client.invalidateQueries({ queryKey: ['crew-capabilities', 'crewA'] }) })
    await screen.findByText('Review version: r4')
    expect(screen.getByRole('button', { name: 'Save reviewed changes' })).toBeDisabled()
    expect(saveBodies).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: 'Use the new version for this draft' }))
    expect(screen.queryByRole('button', { name: 'Save reviewed changes' })).not.toBeInTheDocument()
    await review()
    expect(previewBodies[1].revision).toBe('r4')
    expect(previewBodies[1].operations).toEqual(previewBodies[0].operations)
  })

  it('retains unchanged masked leaves on a command-only edit through preview and save', async () => {
    view.rows = view.rows.map(row => row.id === 'protected' ? { ...row, value: { command: 'old-cli', args: ['--token', '[REDACTED]'], env: { 'TOKEN/~key': '[REDACTED]' }, timeout: 30 } } : row)
    mount(); await ready()
    const row = within(screen.getByTestId('capability-mcpServers-protected'))
    fireEvent.click(row.getByText('Connection settings'))
    expect(row.getByLabelText('Variable value')).toHaveValue('')
    expect(row.getByLabelText('Argument 2')).toHaveAttribute('placeholder', 'Hidden value kept. Type to replace.')
    fireEvent.change(row.getByLabelText('Command'), { target: { value: 'new-cli' } })
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'protected', action: 'set', value: { command: 'new-cli', args: ['--token', '[REDACTED]'], env: { 'TOKEN/~key': '[REDACTED]' }, timeout: 30 }, retain_paths: ['/args/1', '/env/TOKEN~1~0key'] }])
    fireEvent.click(screen.getByRole('button', { name: 'Save reviewed changes' }))
    await waitFor(() => expect(saveBodies).toHaveLength(1))
    expect(saveBodies[0]).toEqual({ ...previewBodies[0], preview_token: 'signed-preview' })
  })

  it('never reuses retention when an argument moves or a secret key is renamed', async () => {
    mount(); await ready()
    const row = within(screen.getByTestId('capability-mcpServers-protected'))
    fireEvent.click(row.getByText('Connection settings'))
    expect(row.getAllByRole('button', { name: 'Remove argument' })[0]).toBeDisabled()
    expect(row.getByLabelText('Variable name')).toBeDisabled()
    fireEvent.change(row.getByLabelText('Argument 2'), { target: { value: 'replacement' } })
    fireEvent.click(row.getAllByRole('button', { name: 'Remove argument' })[0])
    fireEvent.change(row.getByLabelText('Variable value'), { target: { value: 'replacement-value' } })
    fireEvent.change(row.getByLabelText('Variable name'), { target: { value: 'RENAMED' } })
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'protected', action: 'set', value: { command: 'protected-cli', args: ['replacement'], env: { RENAMED: 'replacement-value' } } }])
    expect(screen.queryByText('replacement-value')).not.toBeInTheDocument()
  })

  it('does not treat a newly typed mask as permission to retain the original value', async () => {
    mount(); await ready()
    const row = within(screen.getByTestId('capability-mcpServers-protected'))
    fireEvent.click(row.getByText('Connection settings'))
    fireEvent.change(row.getByLabelText('Variable value'), { target: { value: 'replacement' } })
    fireEvent.change(row.getByLabelText('Variable value'), { target: { value: '[REDACTED]' } })
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled()
    expect(screen.getByText('Fill in the connection fields and replace hidden values not marked to keep.')).toBeInTheDocument()
    expect(previewBodies).toEqual([])
  })

  it('edits HTTP headers while keeping an unchanged authorization header', async () => {
    view.rows.push({ section: 'mcpServers', id: 'http', label: 'HTTP server', state: 'local', present: true, value: { url: 'https://example.test/mcp', headers: { Authorization: '[REDACTED]' } } })
    mount(); await ready()
    const row = within(screen.getByTestId('capability-mcpServers-http'))
    fireEvent.click(row.getByText('Connection settings'))
    fireEvent.change(row.getByLabelText('Server URL'), { target: { value: 'https://example.test/new' } })
    fireEvent.click(row.getByRole('button', { name: 'Add header' }))
    fireEvent.change(row.getAllByLabelText('Header name')[1], { target: { value: 'X-Region' } })
    fireEvent.change(row.getAllByLabelText('Header value')[1], { target: { value: 'test-region' } })
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'http', action: 'set', value: { url: 'https://example.test/new', headers: { Authorization: '[REDACTED]', 'X-Region': 'test-region' } }, retain_paths: ['/headers/Authorization'] }])
  })

  it('creates an unlisted member connection as a complete transport', async () => {
    mount(); await ready()
    fireEvent.change(screen.getByLabelText('Custom connection name'), { target: { value: 'member-only' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add custom MCP connection' }))
    expect(screen.getByRole('button', { name: 'Review changes' })).toBeDisabled()
    const row = within(screen.getByTestId('capability-mcpServers-member-only'))
    fireEvent.click(row.getByText('Connection settings'))
    fireEvent.change(row.getByLabelText('Command'), { target: { value: 'member-cli' } })
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'member-only', action: 'set', value: { command: 'member-cli', args: [], env: {} } }])
  })

  it('sets absent prompt and model using the advertised model catalog', async () => {
    const modelsRead = vi.fn()
    server.use(http.get('/api/models', () => { modelsRead(); return HttpResponse.json([{ model_name: 'catalog-model' }]) }))
    view.rows.push(...(['prompt', 'model'] as const).map(section => ({ section, id: section, label: section, state: 'inherited' as const, present: false, value: null })))
    mount(); await ready()
    fireEvent.click(screen.getByText('Advanced references and template fields'))
    expect(screen.getAllByText('System Prompt')).toHaveLength(1)
    fireEvent.change(screen.getByLabelText('System Prompt'), { target: { value: 'First line\nSecond line' } })
    await waitFor(() => expect(modelsRead).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('combobox', { name: 'Model', exact: true }))
    fireEvent.click(await screen.findByRole('option', { name: 'catalog-model' }))
    await review()
    expect(previewBodies[0].operations).toEqual([{ section: 'prompt', id: 'prompt', action: 'set', value: 'First line\nSecond line' }, { section: 'model', id: 'model', action: 'set', value: 'catalog-model' }])
    expect(screen.queryByRole('textbox', { name: 'Model', exact: true })).not.toBeInTheDocument()
  })

  describe('a platform server the member lacks', () => {
    const offerOps: CapabilityOperation[] = [
      { section: 'mcpServers', id: 'kirocrew-mate', action: 'set', value: { disabled: false } },
      { section: 'tools', id: '@kirocrew-mate', action: 'set', value: true },
      { section: 'allowedTools', id: '@kirocrew-mate/set_goal', action: 'set', value: true },
      { section: 'allowedTools', id: '@kirocrew-mate/set_check_ins', action: 'set', value: true },
    ]
    const offerName = 'Add the goal tools (kirocrew-mate)'
    const approval = 'Saving adds these tools to crewA only (not other crewmates on this template) and auto-approves them.'
    const help = 'Lets crewA propose its goal, milestones and check-ins and track its progress. It can also change its own name, description and face when you ask, and request a different model, approval mode or tools on a card you confirm.'
    const rowNote = 'Added for crewA only — not part of the template'
    const clickRowEnabled = () => fireEvent.click(within(screen.getByTestId('capability-mcpServers-kirocrew-mate')).getByRole('checkbox', { name: 'Enabled' }))
    beforeEach(() => {
      view.mode = 'shared'
      view.platform_offer = { value: { command: 'kirocrew', args: ['mcp-mate'] }, operations: offerOps }
    })

    it.each(['shared', 'legacy_snapshot'] as const)('is offered behind the enroll box and adds every creation row in one draft (%s)', async mode => {
      view.mode = mode
      server.use(http.put(endpoint, async ({ request }) => {
        saveBodies.push(await request.json() as CapabilityDraft & { preview_token: string })
        view = { ...view, mode: 'inherited', revision: 'r2', platform_offer: null, rows: [...view.rows, { section: 'mcpServers', id: 'kirocrew-mate', label: 'kirocrew-mate', state: 'local', present: true, value: { command: 'kirocrew', args: ['mcp-mate'] }, managed: true }] }
        return HttpResponse.json(view)
      }))
      const { saved } = mount(); await ready()
      const offer = screen.getByRole('checkbox', { name: offerName })
      // Read-only like every other row until the member follows its parent, and
      // the box itself names the box that lifts that.
      expect(offer).toBeDisabled()
      expect(offer).toHaveAccessibleDescription(`Check “Follow the parent template” above to add these tools. ${approval} ${help}`)
      expect(screen.queryByTestId('capability-mcpServers-kirocrew-mate')).toBeNull()
      fireEvent.click(screen.getByRole('checkbox', { name: 'Follow the parent template' }))
      // The approval comes before the help, which names every tool it covers,
      // and neither is muted.
      expect(offer).toHaveAccessibleDescription(`${approval} ${help}`)
      expect(screen.queryByTestId('capability-offer-enroll-first')).toBeNull()
      expect(screen.getByText(approval)).not.toHaveClass('text-muted')
      expect(screen.getByText(help)).not.toHaveClass('text-muted')
      fireEvent.click(offer)
      expect(offer).toBeChecked()
      // The server shows as the managed row it becomes: enabled, transport
      // read-only, with a note that it is this member's, not the template's.
      const row = within(screen.getByTestId('capability-mcpServers-kirocrew-mate'))
      expect(row.getByRole('checkbox', { name: 'Enabled' })).toBeChecked()
      expect(row.getByText(rowNote)).toBeInTheDocument()
      expect(row.queryByText('Comes with the template — read-only here')).toBeNull()
      fireEvent.click(row.getByText('Connection settings'))
      expect(row.getByLabelText('Command')).toHaveValue('kirocrew')
      expect(row.getByLabelText('Command')).toBeDisabled()
      await review()
      expect(previewBodies).toEqual([{ revision: 'r1', enroll: true, operations: offerOps, accept_parent: [], accept_members: [] }])
      fireEvent.click(screen.getByRole('button', { name: 'Save reviewed changes' }))
      await waitFor(() => expect(saved).toHaveBeenCalled())
      expect(saveBodies).toEqual([{ revision: 'r1', enroll: true, operations: offerOps, accept_parent: [], accept_members: [], preview_token: 'signed-preview' }])
      // Saved: the offer is gone and the row is the member's own.
      await waitFor(() => expect(screen.queryByRole('checkbox', { name: offerName })).toBeNull())
      expect(within(screen.getByTestId('capability-mcpServers-kirocrew-mate')).getByRole('checkbox', { name: 'Enabled' })).toBeChecked()
    })

    it('leaves the draft whole when unticked, keeping rows the owner set before', async () => {
      mount(); await ready()
      fireEvent.click(screen.getByRole('checkbox', { name: 'Follow the parent template' }))
      await pickState('Search', 'Removed')
      const offer = screen.getByRole('checkbox', { name: offerName })
      fireEvent.click(offer)
      fireEvent.click(offer)
      expect(offer).not.toBeChecked()
      expect(screen.queryByTestId('capability-mcpServers-kirocrew-mate')).toBeNull()
      await review()
      expect(previewBodies[0].operations).toEqual([{ section: 'mcpServers', id: 'search', action: 'remove' }])
    })

    it.each([
      { change: 'removed', act: () => pickState('kirocrew-mate', 'Removed') },
      { change: 'muted', act: async () => { clickRowEnabled() } },
    ])('unticks once the added server is $change, and ticking again restores every row', async ({ act }) => {
      mount(); await ready()
      fireEvent.click(screen.getByRole('checkbox', { name: 'Follow the parent template' }))
      const offer = screen.getByRole('checkbox', { name: offerName })
      fireEvent.click(offer)
      await act()
      // The draft no longer adds the server as offered, so the box does not claim it does.
      expect(offer).not.toBeChecked()
      fireEvent.click(offer)
      expect(offer).toBeChecked()
      await review()
      expect(previewBodies[0].operations).toEqual(offerOps)
    })

    it.each([
      { way: 'the box', act: async () => { fireEvent.click(screen.getByRole('checkbox', { name: offerName })) } },
      { way: 'its Enabled box', act: async () => { clickRowEnabled() } },
      { way: 'Override', act: () => pickState('kirocrew-mate', 'Override') },
    ])('gives the added row nothing to inherit or restore, and adds it back through $way', async ({ act }) => {
      mount(); await ready()
      fireEvent.click(screen.getByRole('checkbox', { name: 'Follow the parent template' }))
      const offer = screen.getByRole('checkbox', { name: offerName })
      fireEvent.click(offer)
      // Its parent has no entry for this server, so Inherited is not a choice.
      fireEvent.click(screen.getByRole('combobox', { name: 'Source for kirocrew-mate' }))
      expect(await screen.findByRole('option', { name: 'Removed', exact: true })).toBeInTheDocument()
      expect(screen.queryByRole('option', { name: 'Inherited', exact: true })).toBeNull()
      fireEvent.click(screen.getByRole('option', { name: 'Removed', exact: true }))
      // Removed, it is not added for the member, so the row carries no note, and
      // with nothing in its parent it has no restore button of any label.
      const row = within(screen.getByTestId('capability-mcpServers-kirocrew-mate'))
      expect(row.queryByText(rowNote)).toBeNull()
      expect(row.queryByText('Comes with the template — read-only here')).toBeNull()
      expect(row.queryByRole('button')).toBeNull()
      expect(offer).not.toBeChecked()
      // The box adds it back, and so do the row's own controls, as on any
      // managed row; either way the draft holds the whole offer again.
      await act()
      expect(offer).toBeChecked()
      expect(within(screen.getByTestId('capability-mcpServers-kirocrew-mate')).getByText(rowNote)).toBeInTheDocument()
      await review()
      expect(previewBodies[0].operations).toHaveLength(offerOps.length)
      expect(previewBodies[0].operations).toEqual(expect.arrayContaining(offerOps))
    })

    it('is not shown when the view offers nothing', async () => {
      view.platform_offer = null
      mount(); await ready()
      expect(screen.queryByTestId('capability-offer')).toBeNull()
    })
  })

  it('encodes the exact member identity in the HTTP path', async () => {
    const observed = vi.fn()
    server.use(http.get('/api/agents/:member/capabilities', ({ params }) => { observed(params.member); return HttpResponse.json(view) }))
    await crewCapabilitiesApi.get('crew space')
    expect(observed).toHaveBeenCalledWith('crew space')
  })
})
