import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { crewCapabilitiesApi, type CapabilityDraft, type CapabilityView } from '../api/crewCapabilities'
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
    expect(screen.getByTestId('capability-enroll-to-edit')).toHaveTextContent('Checked: edit overrides below. Unchecked: read-only.')
    expect(screen.queryByText('Tick “Follow the parent template” above to edit.')).toBeNull()
    expect(screen.getByText('Not following parent (independent legacy snapshot)')).toBeInTheDocument()
    const enroll = screen.getByRole('checkbox', { name: 'Follow the parent template' })
    // The guarantee is next to the checkbox, not buried in a long helper.
    expect(enroll.closest('label')).toHaveTextContent('Nothing changes until you save. Following keeps your values and empty fields.')
    expect(enroll).toHaveAccessibleDescription('Nothing changes until you save. Following keeps your values and empty fields. Checked: edit overrides below. Unchecked: read-only.')
    expect(enroll.closest('label')).toHaveAttribute('for', enroll.id)
    expect(enroll.id).toBe('crew-capabilities-enroll')
    fireEvent.click(screen.getByText('Follow the parent template', { selector: 'span' }))
    expect(screen.queryByTestId('capability-enroll-to-edit')).toBeNull()
    // The badge names the SAVED state, so a ticked Follow box beside it reads
    // as a draft, not as a contradiction.
    expect(enroll).toBeChecked()
    expect(screen.getByText('Not following parent (independent legacy snapshot)')).toBeInTheDocument()
    // The pane calls its pending edits a draft everywhere (footer, confirm, stale
    // notice), so the badge uses the same word instead of the editor's generic one.
    expect(screen.getByText('Unsaved draft')).toBeInTheDocument()
    expect(screen.queryByText('Unsaved changes')).toBeNull()
    expect(screen.getByRole('combobox', { name: 'Source for Search' })).not.toBeDisabled()
    await review()
    expect(previewBodies[0].enroll).toBe(true)
    expect(previewBodies[0].operations).toEqual([])
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

  it('encodes the exact member identity in the HTTP path', async () => {
    const observed = vi.fn()
    server.use(http.get('/api/agents/:member/capabilities', ({ params }) => { observed(params.member); return HttpResponse.json(view) }))
    await crewCapabilitiesApi.get('crew space')
    expect(observed).toHaveBeenCalledWith('crew space')
  })
})
