/**
 * CrewEditorNextDialog + CrewApprovalsEditor + CrewEditorModal — the crewmate
 * editor (the single, "New" editing experience).
 *
 * CrewEditorModal is the lazy-split wrapper that mounts CrewEditorNextDialog
 * against the CrewEditorController. These tests drive the REAL hook through a
 * tiny Host and exercise the dialog's own surfaces:
 *   - the section rail (general / builtFrom / behavior / memory / danger) and
 *     each pane's body;
 *   - the read-only Built From inspectors (details stats, Tools / MCP / Skills),
 *     including that an MCP row expands to env/header NAMES only (never a value)
 *     and the capability loading / error states;
 *   - the Approvals editor's toggle → review → save path and the
 *     ceiling-expansion warning;
 *   - the instant template switch and its refusal (templateSwitchError) surface;
 *   - the Save/dirty footer, the discard guard, and the two-step danger delete;
 *   - that the modal mounts the New dialog directly with no classic dialog or
 *     experience toggle;
 *   - that the Schedules pane receives hideKindBadge (redundant-chip removal) and
 *     the crew slug as member_id.
 *
 * i18nT resolves against the real English catalog (generated ⊕ en.manual, which
 * is runtime-merged), so assertions use the real English strings or testids.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { CapabilityRow, CapabilityView, CapabilityPreview } from '../api/crewCapabilities'
import { ApiError } from '../api/apiError'

/* Render framer-motion elements as plain DOM (same rationale as the sibling
   editor test: the dialog is an AnimatePresence child). */
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, { get: (_t, tag: string) => { if (!cache.has(tag)) cache.set(tag, make(tag)); return cache.get(tag) } }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

const mockApi = vi.hoisted(() => ({
  kirocrewAgents: vi.fn(),
  agentsInstalled: vi.fn(),
  workspaces: vi.fn(),
  kirocrewConfig: vi.fn(),
  createWorkspace: vi.fn(),
  updateKirocrewAgent: vi.fn(),
  deleteKirocrewAgent: vi.fn(),
  uploadCrewAvatar: vi.fn(),
  agentResolvedModel: vi.fn(),
  crons: vi.fn(),
  webhooks: vi.fn(),
  models: vi.fn(),
  // Memory doc reads the Memory → Profile pane fires once memoryStore is set.
  memoryPreferences: vi.fn(),
  memoryProjects: vi.fn(),
  saveMemoryPreferences: vi.fn(),
  saveMemoryProjects: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

/* The capabilities API drives both the Built From inspectors and the Approvals
   editor. Mock the module so we hand back a realistic view. */
const mockCaps = vi.hoisted(() => ({
  get: vi.fn(),
  preview: vi.fn(),
  save: vi.fn(),
}))
const mockKit = vi.hoisted(() => ({
  get: vi.fn(),
  preview: vi.fn(),
  apply: vi.fn(),
}))
vi.mock('../api/crewCapabilities', async (orig) => {
  const real = await orig<typeof import('../api/crewCapabilities')>()
  return { ...real, crewCapabilitiesApi: mockCaps, crewWorkerKitApi: mockKit }
})

/* Plain-DOM stand-in for SimpleSelect (same as the sibling test). */
vi.mock('../components/SimpleSelect', () => ({
  default: ({ options, value, onChange, 'aria-label': ariaLabel }: {
    options: string[]; value: string; onChange: (v: string) => void; 'aria-label'?: string
  }) => {
    const listboxId = `zzq-listbox-${(ariaLabel ?? 'unlabelled').replace(/\W+/g, '-')}`
    return (
      <div>
        <button type="button" role="combobox" aria-label={ariaLabel} aria-expanded={false} aria-controls={listboxId}>{value}</button>
        <div role="listbox" id={listboxId}>
          {options.map((o) => (
            <button key={o} type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>{o}</button>
          ))}
        </div>
      </div>
    )
  },
}))

/* Service-backed sub-panels reach live endpoints and are not this test's
   subject; stub them to plain markers so the dialog mounts. CrewWakeSection
   records its props so we can assert hideKindBadge + member_id threading. */
const wakeProps = vi.hoisted(() => ({ last: null as null | { crew?: string; memberId?: string; hideKindBadge?: boolean } }))
vi.mock('../components/CrewWakeSection', () => ({
  default: (props: { crew?: string; memberId?: string; hideKindBadge?: boolean }) => {
    wakeProps.last = { crew: props.crew, memberId: props.memberId, hideKindBadge: props.hideKindBadge }
    return <div data-testid="stub-wake" data-member-id={props.memberId} data-hide-kind-badge={String(props.hideKindBadge)} />
  },
}))
vi.mock('../components/CrewWebhookSection', () => ({ default: () => <div data-testid="stub-webhook" /> }))
// Holds a local draft and reports it dirty like the real card, so an unmount
// visibly loses the text.
vi.mock('../pages/overview/MemoryDocCard', async () => {
  const { useState } = await import('react')
  function StubMemoryDocCard({ title, onDirtyChange }: { title: string; onDirtyChange?: (dirty: boolean) => void }) {
    const [draft, setDraft] = useState('')
    return <textarea data-testid="stub-memory-doc" aria-label={title} value={draft} onChange={e => { setDraft(e.target.value); onDirtyChange?.(e.target.value !== '') }} />
  }
  return { default: StubMemoryDocCard }
})
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div data-testid="stub-memory-records" /> }))
vi.mock('../pages/overview/MemoryBackupsCard', () => ({ default: () => <div data-testid="stub-memory-backups" /> }))
vi.mock('../pages/overview/MemoryRetiredCard', () => ({ default: () => <div data-testid="stub-memory-retired" /> }))
vi.mock('../pages/overview/MemoryCarveCard', () => ({ default: () => <div data-testid="stub-memory-carve" /> }))
vi.mock('../components/CrewAvatarBuilder', () => ({
  default: ({ open }: { open?: boolean }) => (open ? <div data-testid="stub-avatar-builder" /> : null),
}))

import CrewEditorModal from '../components/crew/CrewEditorModal'
import { useCrewEditor } from '../components/crew/useCrewEditor'
import type { KiroCrewAgent } from '../api/types'

const DEFAULT_CREW = { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'core-ws', memory_store: 'core-mem' }
const OTHER_CREW = { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-mem' }
const AGENTS_RESPONSE = { agents: [DEFAULT_CREW, OTHER_CREW], default_agent: 'kirocrew' }

/* A capability view the inspectors + approvals editor render against:
   two built-in tools (one read-only, one not), two MCP servers (HTTP with a
   header NAME + stdio with an env NAME — values must never surface), one skill,
   and two auto-approve grants (a tool + an MCP verb). */
const CAPS_VIEW: CapabilityView = {
  schema_version: 1,
  member: 'oncall',
  mode: 'inherited',
  revision: 'rev-1',
  template: { name: 'oncall-agent', source: 'builtin', scope: 'global', available: true },
  rows: [
    { section: 'tools', id: 'fs_read', label: 'fs_read', state: 'inherited', present: true, value: null },
    { section: 'tools', id: 'fs_write', label: 'fs_write', state: 'inherited', present: true, value: null },
    {
      section: 'mcpServers', id: 'github', label: 'github', state: 'inherited', present: true, managed: true,
      value: { url: 'https://api.githubcopilot.com/mcp', headers: { Authorization: 'SECRET-TOKEN' } },
    },
    {
      section: 'mcpServers', id: 'local-fs', label: 'local-fs', state: 'inherited', present: true, managed: true,
      value: { command: '/usr/bin/node', args: ['server.js'], env: { API_KEY: 'SECRET-VALUE' } },
    },
    { section: 'skills', id: 'kirocrew-core/writing-tests', label: '', state: 'inherited', present: true, value: null, shared_reference: true },
    { section: 'allowedTools', id: 'fs_read', label: 'fs_read', state: 'inherited', present: true, value: null },
    // An MCP server-allow parent (`@github`) with auto-approved verb children.
    // The nesting + cascade regression test below drives these.
    { section: 'allowedTools', id: '@github', label: '@github', state: 'inherited', present: true, value: null },
    { section: 'autoApprove', id: '@github/create_issue', label: 'create_issue', state: 'inherited', present: true, value: null },
    { section: 'autoApprove', id: '@github/list_issues', label: 'list_issues', state: 'inherited', present: true, value: null },
    // An allowed server with NO auto-approved verbs → renders a non-expandable
    // "No tools auto-approved" row, never "0 of 0".
    { section: 'allowedTools', id: '@builder-mcp', label: '@builder-mcp', state: 'inherited', present: true, value: null },
  ],
  connections: [],
  skills: [],
  parent_changes: [],
  runtime: { status: 'applied', saved_revision: 'rev-1', sessions: [] },
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.kirocrewAgents.mockResolvedValue(AGENTS_RESPONSE)
  mockApi.agentsInstalled.mockResolvedValue([{ name: 'kirocrew' }, { name: 'oncall-agent' }])
  mockApi.workspaces.mockResolvedValue({ workspaces: [{ name: 'default' }, { name: 'core-ws' }, { name: 'oncall' }] })
  mockApi.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {}, 'core-mem': {}, 'oncall-mem': { memory_version: 2, owner_member: 'oncall' } } })
  mockApi.agentResolvedModel.mockResolvedValue({ model: '', pinned: false, kiro_agent: 'oncall-agent' })
  mockApi.crons.mockResolvedValue({ jobs: [] })
  mockApi.webhooks.mockResolvedValue({ tokens: [], switch_on: true })
  mockApi.models.mockResolvedValue([])
  mockApi.updateKirocrewAgent.mockResolvedValue({})
  mockApi.deleteKirocrewAgent.mockResolvedValue({})
  mockApi.uploadCrewAvatar.mockResolvedValue({ ok: true, token: 'staged-token' })
  mockApi.createWorkspace.mockResolvedValue({ name: 'staging' })
  mockApi.memoryPreferences.mockResolvedValue({ content: '' })
  mockApi.memoryProjects.mockResolvedValue({ content: '' })
  mockCaps.get.mockResolvedValue(CAPS_VIEW)
  mockCaps.preview.mockResolvedValue({ ...CAPS_VIEW, preview_token: 'tok-1', impact: [] } as CapabilityPreview)
  mockCaps.save.mockResolvedValue(CAPS_VIEW)
})

const leaveSpy = vi.fn((perform: () => void | Promise<void>) => { void perform() })

function Host({ memberId, openCrew }: { memberId?: string; openCrew?: string } = {}) {
  const [editingCrew, setEditingCrew] = useState('')
  const crewAgentsQuery = useQuery({
    queryKey: ['kirocrew-agents'],
    queryFn: () => mockApi.kirocrewAgents(),
    enabled: !!editingCrew,
  })
  const crewAgents = (crewAgentsQuery.data?.agents || []) as KiroCrewAgent[]
  const ctl = useCrewEditor({
    editingName: editingCrew,
    memberId,
    agents: crewAgents,
    agentsLoaded: crewAgentsQuery.data !== undefined || crewAgentsQuery.isError,
    defaultAgent: crewAgentsQuery.data?.default_agent || '',
    refetchAgents: () => {},
    onClose: () => setEditingCrew(''),
    onDeleted: () => {},
    leave: leaveSpy,
  })
  return (
    <>
      <button data-testid="open-editor" onClick={() => setEditingCrew(openCrew ?? 'oncall')}>edit</button>
      {/* Direct controller drives for handlers the modal UI does not surface
          (save flow) and a readout of a computed value, so the hook's
          save/collision paths are exercised. */}
      <button data-testid="ctl-save" onClick={() => ctl.save()}>save</button>
      <div data-testid="ctl-colliding">{ctl.collidingCrews.join(',')}</div>
      <CrewEditorModal ctl={ctl} />
    </>
  )
}

let lastQc: QueryClient | undefined
function renderHost(opts?: { memberId?: string; openCrew?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  lastQc = qc
  const store = configureStore({ reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter><Host memberId={opts?.memberId} openCrew={opts?.openCrew} /></MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

/** Render the host and open the editor. The New experience is the default, so
 *  the New dialog mounts directly; wait for the options read then return it. */
async function openNew(opts?: { memberId?: string; openCrew?: string }) {
  renderHost(opts)
  fireEvent.click(screen.getByTestId('open-editor'))
  // New dialog is the default experience — it mounts directly, no toggle flip.
  const dialog = await screen.findByTestId('crew-editor-next-dialog')
  await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
  return dialog
}

describe('CrewEditorModal — single editing experience', () => {
  it('mounts the New dialog directly, with no Classic dialog or experience toggle', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    // The New dialog is the only experience — it mounts straight from the modal.
    const next = await screen.findByTestId('crew-editor-next-dialog')
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    // No classic dialog, and the retired Classic/New toggle is gone. The one
    // dialog is the New editor, named for the crewmate it edits.
    expect(screen.getAllByRole('dialog', { name: /Edit mate/ })).toEqual([next])
    expect(within(next).queryByTestId('crew-editor-experience-toggle')).toBeNull()
    expect(within(next).queryByTestId('crew-editor-exp-classic')).toBeNull()
    expect(within(next).queryByTestId('crew-editor-exp-next')).toBeNull()
  })
})

describe('CrewEditorNextDialog — section rail', () => {
  it('shows the identity header and walks every section rail entry', async () => {
    const dialog = await openNew()
    // Header carries the mate's identity (display name falls back to the slug).
    // Scope to the heading so it does not collide with the Workspace select,
    // which also offers an "oncall" option.
    expect(within(dialog).getByRole('heading', { name: 'oncall' })).toBeInTheDocument()

    for (const [key, pane] of [
      ['general', null],
      ['builtFrom', 'next-pane-builtFrom'],
      ['approvals', 'next-pane-approvals'],
      ['schedules', 'next-pane-schedules'],
      ['webhooks', 'next-pane-webhooks'],
      ['memory', 'next-pane-memory'],
      ['danger', 'next-pane-danger'],
    ] as const) {
      fireEvent.click(within(dialog).getByTestId(`next-rail-${key}`))
      await waitFor(() => expect(within(dialog).getByTestId(`next-rail-${key}`)).toHaveAttribute('aria-selected', 'true'))
      if (pane) expect(await within(dialog).findByTestId(pane)).toBeInTheDocument()
    }
  })

  it('offers no Add-abilities pane to a crewmate that does not own its config', async () => {
    const dialog = await openNew()
    await waitFor(() => expect(mockCaps.get).toHaveBeenCalled())
    await waitFor(() => expect(within(dialog).getByTestId('next-rail-builtFrom')).toBeInTheDocument())
    // There is no separate Advanced rail entry; Add abilities lives on the
    // Abilities page, and only for a crewmate that owns its file.
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    // The capabilities query determines whether Add abilities is available.
    await within(dialog).findByTestId('next-builtfrom-details', {}, { timeout: 2000 })
    await waitFor(() => expect(within(dialog).queryByTestId('next-pane-advanced')).toBeNull(), { timeout: 2000 })
  })

  it('shows the Add-abilities flow on the Abilities page for a crewmate that owns its config', async () => {
    mockCaps.get.mockResolvedValue({ ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, scope: 'mate' } })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    // Add abilities is on the same page as the bands, not a separate tab.
    expect(await within(dialog).findByTestId('next-pane-advanced')).toBeInTheDocument()
    // The picker waits for WorkerKitPane's separate worker-kit query.
    expect(await within(dialog).findByRole('combobox', { name: /copy abilities from/i }, { timeout: 2000 })).toBeInTheDocument()
  })

  it('gives a crewmate that owns its config no Built from switcher, only the way to Abilities', async () => {
    mockCaps.get.mockResolvedValue({ ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' } })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    const owned = await within(dialog).findByTestId('next-built-from-owned')
    // The switcher would repoint the binding away from the crewmate's own file.
    expect(within(dialog).queryByRole('combobox', { name: /built from/i })).toBeNull()
    // A generic mate label, never the file's id and never a template provenance.
    expect(owned).toHaveTextContent('Mate')
    expect(owned).not.toHaveTextContent('c0ffee12')
    fireEvent.click(within(dialog).getByTestId('next-open-advanced'))
    expect(await within(dialog).findByTestId('next-pane-builtFrom')).toBeInTheDocument()
  })

  it("names the capabilities view and the Added band by the crewmate's display name, not the file id", async () => {
    mockApi.kirocrewAgents.mockResolvedValue({
      ...AGENTS_RESPONSE, agents: [DEFAULT_CREW, { ...OTHER_CREW, display_name: 'On-Call Pager' }],
    })
    mockCaps.get.mockResolvedValue({ ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' } })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    const name = await within(dialog).findByTestId('next-builtfrom-name')
    expect(name).toHaveTextContent('What On-Call Pager can do')
    expect(name).not.toHaveTextContent('c0ffee12')
    const added = within(dialog).getByTestId('next-band-added')
    expect(added).toHaveTextContent('Added to On-Call Pager')
    expect(added).not.toHaveTextContent('Added to Mate')
  })

  it("splits what the owned config's worker carries into Comes-with and Added bands", async () => {
    const row = (section: CapabilityRow['section'], id: string, state: CapabilityRow['state']): CapabilityRow => ({
      section, id, label: section === 'skills' ? '' : id, state, present: true, value: true, managed: false, shared_reference: section === 'skills',
      remove: state === 'local' ? { section, id } : null,
    })
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' },
      worker: {
        rows: [
          // Comes with (source base): inherited.
          row('tools', 'fs_read', 'inherited'),
          row('mcpServers', 'platform', 'inherited'),
          // Added (the file's own additions): local.
          row('mcpServers', 'github', 'local'),
          row('skills', 'release-notes', 'local'),
        ],
      },
    })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))

    const comesWith = await within(dialog).findByTestId('next-band-comes-with')
    // Tools starts collapsed (it's the long group); expand to read a tool row.
    fireEvent.click(within(comesWith).getByTestId('next-band-comes-with-group-tools'))
    expect(within(comesWith).getByText('fs_read')).toBeInTheDocument()
    // MCP servers are open by default; a comes-with row is never removable.
    expect(within(comesWith).getByText('platform')).toBeInTheDocument()
    expect(within(comesWith).queryByTestId('next-band-remove-mcpServers-platform')).toBeNull()

    const added = within(dialog).getByTestId('next-band-added')
    expect(within(added).getByText('github')).toBeInTheDocument()
    expect(within(added).getByText('Release Notes')).toBeInTheDocument()
    // An added item is removable; a comes-with item never is.
    expect(within(added).getByTestId('next-band-remove-mcpServers-github')).toBeInTheDocument()
  })

  it('hides the Comes-with band entirely when the file carries everything (no inherited rows)', async () => {
    const row = (section: CapabilityRow['section'], id: string, state: CapabilityRow['state']): CapabilityRow => ({
      section, id, label: section === 'skills' ? '' : id, state, present: true, value: true, managed: false, shared_reference: section === 'skills',
    })
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' },
      // A full-copy mate: every row is in its own file -> local, nothing inherited.
      worker: { rows: [row('mcpServers', 'github', 'local'), row('tools', '@github', 'local')] },
    })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    // Added renders; Comes-with is absent (a dead "None" box reads as broken).
    expect(await within(dialog).findByTestId('next-band-added')).toBeInTheDocument()
    expect(within(dialog).queryByTestId('next-band-comes-with')).toBeNull()
  })

  it.each([
    { section: 'skills' as const, id: 'kirocrew-worktree-dev', secondary: '' },
    { section: 'mcpServers' as const, id: 'long-named-local-development-server', secondary: 'long-development-server.example.com' },
  ])('truncates the long local $section band label while keeping Remove rendered', async ({ section, id, secondary }) => {
    const row: CapabilityRow = {
      section, id, label: id, state: 'local', present: true,
      value: secondary ? { url: `https://${secondary}/mcp` } : null,
      managed: false, shared_reference: section === 'skills', remove: { section, id },
    }
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' },
      worker: { rows: [row] },
    })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    const added = await within(dialog).findByTestId('next-band-added')
    const bandRow = within(added).getByTestId('next-band-added-row')
    const label = within(bandRow).getByTitle(id)
    const remove = within(bandRow).getByRole('button', { name: 'Remove' })
    expect(remove).toBeInTheDocument()
    expect(remove).toHaveClass('shrink-0')
    expect(label).toHaveTextContent(id)
    expect(label).toHaveClass('truncate', 'min-w-0', 'flex-1')
    expect(label).not.toHaveClass('shrink-0')
    expect(label.parentElement).toHaveClass('flex', 'min-w-0', 'flex-1')
    expect(bandRow).toHaveClass('flex', 'min-w-0')
    if (secondary) {
      expect(within(bandRow).getByText(secondary)).toHaveClass('truncate', 'min-w-0', 'flex-1')
    }
  })

  it('removes an added capability through the capabilities remove op', async () => {
    const row = (section: CapabilityRow['section'], id: string, state: CapabilityRow['state']): CapabilityRow => ({
      section, id, label: id, state, present: true, value: true, managed: false, shared_reference: false,
      remove: state === 'local' ? { section, id } : null,
    })
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' },
      worker: { rows: [row('mcpServers', 'github', 'local')] },
    })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    const added = await within(dialog).findByTestId('next-band-added')
    fireEvent.click(within(added).getByTestId('next-band-remove-mcpServers-github'))
    const ask = await screen.findByRole('dialog', { name: 'Remove “github”?' })
    fireEvent.click(within(ask).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(mockCaps.preview).toHaveBeenCalled())
    await waitFor(() => expect(mockCaps.save).toHaveBeenCalled())
    const draft = mockCaps.preview.mock.calls[0][1]
    expect(draft.operations).toEqual([{ section: 'mcpServers', id: 'github', action: 'remove' }])
  })

  it('removes against the revision shown when Remove was pressed, not a fresh read', async () => {
    const view: CapabilityView = {
      ...CAPS_VIEW, mode: 'mate', revision: 'r1',
      worker: { rows: [{
        section: 'mcpServers', id: 'github', label: 'github', state: 'local', present: true, value: true,
        remove: { section: 'mcpServers', id: 'github' },
      }] },
    }
    mockCaps.get.mockResolvedValue(view)
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    fireEvent.click(await within(dialog).findByTestId('next-band-remove-mcpServers-github'))
    const ask = await screen.findByRole('dialog', { name: 'Remove “github”?' })
    mockCaps.get.mockResolvedValue({ ...view, revision: 'r2' })
    fireEvent.click(within(ask).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(mockCaps.save).toHaveBeenCalled())
    expect(mockCaps.preview.mock.calls[0][1].revision).toBe('r1')
  })

  it('refuses a stale remove, refreshes the view and asks to confirm again', async () => {
    const view: CapabilityView = {
      ...CAPS_VIEW, mode: 'mate', revision: 'r1',
      worker: { rows: [{
        section: 'mcpServers', id: 'github', label: 'github', state: 'local', present: true, value: true,
        remove: { section: 'mcpServers', id: 'github' },
      }] },
    }
    mockCaps.get.mockResolvedValue(view)
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    mockCaps.preview.mockRejectedValueOnce(new ApiError(409, 'stale_revision', '{"code":"stale_revision"}'))
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    fireEvent.click(await within(dialog).findByTestId('next-band-remove-mcpServers-github'))
    const ask = await screen.findByRole('dialog', { name: 'Remove “github”?' })
    const getsBefore = mockCaps.get.mock.calls.length
    mockCaps.get.mockResolvedValue({ ...view, revision: 'r2' })
    fireEvent.click(within(ask).getByRole('button', { name: 'Remove' }))
    expect(await within(dialog).findByTestId('next-builtfrom-remove-error')).toHaveTextContent(
      "This mate's abilities changed since you opened this. Nothing was removed; check the list and press Remove again.",
    )
    expect(mockCaps.save).not.toHaveBeenCalled()
    expect(mockCaps.get.mock.calls.length).toBeGreaterThan(getsBefore)
    expect(mockCaps.preview).toHaveBeenCalledTimes(1)
    fireEvent.click(within(dialog).getByTestId('next-band-remove-mcpServers-github'))
    const retryAsk = await screen.findByRole('dialog', { name: 'Remove “github”?' })
    expect(mockCaps.preview).toHaveBeenCalledTimes(1)
    fireEvent.click(within(retryAsk).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(mockCaps.save).toHaveBeenCalledTimes(1))
    expect(mockCaps.preview.mock.calls[1][1].revision).toBe('r2')
  })

  it('asks before removing an added capability, naming it, and sends nothing on cancel', async () => {
    const row = (section: CapabilityRow['section'], id: string, state: CapabilityRow['state']): CapabilityRow => ({
      section, id, label: id, state, present: true, value: true, managed: false, shared_reference: false,
      remove: state === 'local' ? { section, id } : null,
    })
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' },
      worker: { rows: [row('mcpServers', 'github', 'local')] },
    })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    const added = await within(dialog).findByTestId('next-band-added')
    const getsBefore = mockCaps.get.mock.calls.length
    fireEvent.click(within(added).getByTestId('next-band-remove-mcpServers-github'))
    const ask = await screen.findByRole('dialog', { name: 'Remove “github”?' })
    expect(ask).toHaveTextContent('It stops being available to this mate. You can add it back with Add abilities.')
    fireEvent.click(within(ask).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Remove “github”?' })).toBeNull())
    expect(mockCaps.preview).not.toHaveBeenCalled()
    expect(mockCaps.save).not.toHaveBeenCalled()
    // Cancel does not even read a fresh revision for the remove draft.
    expect(mockCaps.get.mock.calls.length).toBe(getsBefore)
    expect(within(added).getByText('github')).toBeInTheDocument()
  })

  it('keeps the editor open through the Remove confirm: real pointer, focus and Escape', async () => {
    const user = userEvent.setup()
    const row = (section: CapabilityRow['section'], id: string): CapabilityRow => ({
      section, id, label: id, state: 'local', present: true, value: true, managed: false, shared_reference: false,
      remove: { section, id },
    })
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' },
      worker: { rows: [row('mcpServers', 'github')] },
    })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    await user.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    const added = await within(dialog).findByTestId('next-band-added')
    // Escape closes only the confirm.
    await user.click(within(added).getByTestId('next-band-remove-mcpServers-github'))
    await screen.findByRole('dialog', { name: 'Remove “github”?' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Remove “github”?' })).toBeNull())
    expect(screen.getByTestId('crew-editor-next-dialog')).toBeInTheDocument()
    // Confirming removes, and the editor stays.
    await user.click(within(added).getByTestId('next-band-remove-mcpServers-github'))
    const ask = await screen.findByRole('dialog', { name: 'Remove “github”?' })
    await user.click(within(ask).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(mockCaps.save).toHaveBeenCalled())
    expect(screen.getByTestId('crew-editor-next-dialog')).toBeInTheDocument()
  })

  it('removes a skill under the stored row the server names, not the displayed section', async () => {
    const uri = 'skill:///home/x/skills/release-notes/SKILL.md'
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' },
      worker: {
        rows: [{
          section: 'skills', id: uri, label: '', state: 'local', present: true, value: uri,
          managed: false, shared_reference: true, remove: { section: 'resources', id: uri },
        }],
      },
    })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    const added = await within(dialog).findByTestId('next-band-added')
    fireEvent.click(within(added).getByTestId(`next-band-remove-skills-${uri}`))
    const ask = await screen.findByRole('dialog', { name: 'Remove “release-notes”?' })
    fireEvent.click(within(ask).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(mockCaps.preview).toHaveBeenCalled())
    const draft = mockCaps.preview.mock.calls[0][1]
    expect(draft.operations).toEqual([{ section: 'resources', id: uri, action: 'remove' }])
  })

  it('offers no Remove for an added row that names no stored row', async () => {
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW, mode: 'mate', template: { ...CAPS_VIEW.template, name: 'c0ffee12', scope: 'mate' },
      worker: {
        rows: [{
          section: 'mcpServers', id: 'jira', label: 'jira', state: 'local', present: true, value: true,
          managed: false, shared_reference: false, remove: null,
        }],
      },
    })
    mockKit.get.mockResolvedValue({ member: 'oncall', revision: 'r1' })
    const dialog = await openNew()
    fireEvent.click(await within(dialog).findByTestId('next-rail-builtFrom'))
    const added = await within(dialog).findByTestId('next-band-added')
    expect(within(added).getByText('jira')).toBeInTheDocument()
    expect(within(added).queryByTestId('next-band-remove-mcpServers-jira')).toBeNull()
  })

  it('renders the General pane fields (name, built-from, workspace, color)', async () => {
    const dialog = await openNew()
    // General is the default section.
    expect(within(dialog).getByTestId('display-name-input')).toBeInTheDocument()
    expect(within(dialog).getByTestId('next-avatar-button')).toBeInTheDocument()
    // The built-from SimpleSelect (stub) shows the current template.
    expect(within(dialog).getByRole('combobox', { name: /built from/i })).toHaveTextContent('oncall-agent')
  })
})

describe('CrewEditorNextDialog — Capabilities view (read-only two bands)', () => {
  it('shows loading then the bands once capabilities resolve', async () => {
    let resolveCaps: (v: CapabilityView) => void = () => {}
    mockCaps.get.mockReturnValue(new Promise((res) => { resolveCaps = res }))
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    expect(await within(dialog).findByText(/loading/i)).toBeInTheDocument()
    resolveCaps(CAPS_VIEW)
    // The non-mate CAPS_VIEW rows are all inherited → the Comes-with band.
    const comesWith = await within(dialog).findByTestId('next-band-comes-with')
    fireEvent.click(within(comesWith).getByTestId('next-band-comes-with-group-tools'))
    expect(within(comesWith).getByText('fs_read')).toBeInTheDocument()
  })

  it('surfaces an error when the capabilities read fails', async () => {
    mockCaps.get.mockRejectedValue(new Error('caps boom'))
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    expect(await within(dialog).findByTestId('next-builtfrom-error')).toBeInTheDocument()
  })

  it('lists comes-with tools, MCP servers and skills in collapsible kind groups, none removable', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    const comesWith = await within(dialog).findByTestId('next-band-comes-with')
    // MCP servers and skills are open by default.
    expect(within(comesWith).getByText('github')).toBeInTheDocument()
    expect(within(comesWith).getByText('Writing Tests')).toBeInTheDocument()
    // Tools starts collapsed; expand to see them.
    fireEvent.click(within(comesWith).getByTestId('next-band-comes-with-group-tools'))
    expect(within(comesWith).getByText('fs_read')).toBeInTheDocument()
    expect(within(comesWith).getByText('fs_write')).toBeInTheDocument()
    // No remove control anywhere in the comes-with band.
    expect(comesWith.querySelector('[data-testid^="next-band-remove-"]')).toBeNull()
  })

  it('shows the Added band empty-state when the file adds nothing', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    const added = await within(dialog).findByTestId('next-band-added')
    expect(within(added).getByTestId('next-band-added-empty')).toBeInTheDocument()
  })
})

describe('CrewEditorNextDialog — Behavior / Approvals', () => {
  it('renders the approvals editor and runs toggle → review → save', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    // Approvals is now its own top-level rail section.
    const approvals = await within(dialog).findByTestId('next-approvals')

    // Toggle the fs_read grant on-mate: this dirties the editor and reveals Save.
    fireEvent.click(within(approvals).getByTestId('next-approval-toggle-fs_read'))
    const save = await within(approvals).findByTestId('next-approvals-save')
    // First click reviews (preview), second click saves.
    fireEvent.click(save)
    await waitFor(() => expect(mockCaps.preview).toHaveBeenCalled())
    fireEvent.click(within(approvals).getByTestId('next-approvals-save'))
    await waitFor(() => expect(mockCaps.save).toHaveBeenCalled())
  })

  it('a dirty approval draft does not enable footer Save, and survives section nav', async () => {
    // Footer Save runs the roster-only save and closes, so a child draft
    // (approvals) it cannot persist must NOT enable it — otherwise saving
    // silently drops the draft. And navigation is now ALLOWED while an approvals
    // draft is live, because the draft state is lifted to the dialog and
    // survives the pane unmounting (matching the classic always-mounted pane).
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals = await within(dialog).findByTestId('next-approvals')
    fireEvent.click(within(approvals).getByTestId('next-approval-toggle-fs_read'))
    expect(await within(dialog).findByTestId('next-unsaved-note')).toBeInTheDocument()
    // Footer Save is NOT enabled by the child draft alone.
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeDisabled()
    // Because the footer Save is disabled when only a child pane is dirty, the
    // note must point the user at the pane's own Save, not show the generic
    // "Unsaved changes" next to a dead button.
    expect(within(dialog).getByTestId('next-unsaved-note')).toHaveTextContent('Not saved — use Save in this section')
    // Navigation is NO LONGER blocked: switching to General works (the approvals
    // pane unmounts), and the dirty note persists because the draft lives in the
    // dialog, not the pane.
    fireEvent.click(within(dialog).getByTestId('next-rail-general'))
    expect(within(dialog).queryByTestId('next-approvals')).not.toBeInTheDocument()
    expect(within(dialog).getByTestId('next-unsaved-note')).toBeInTheDocument()
    // Returning to Approvals shows the draft preserved: fs_read is still toggled
    // on-mate (its row carries the "changed" marker).
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals2 = await within(dialog).findByTestId('next-approvals')
    expect(within(approvals2).getByTestId('next-approval-toggle-fs_read')).toHaveAttribute('aria-checked', 'false')
  })

  it('keeps footer Save disabled when a roster field AND a child draft are both dirty', async () => {
    // GPT F2: a `.every(p => capabilities||schedules)` guard re-enabled Save
    // once ANY roster pane was dirty alongside a live capability draft, so the
    // roster-only save would close and silently drop the child draft. Save must
    // stay disabled while a capability draft is live, regardless of roster dirt.
    const dialog = await openNew()
    // Make a roster field dirty (display name) — on its own this enables Save.
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'Renamed' } })
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeEnabled()
    // Now also dirty a child pane (an approval toggle).
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals = await within(dialog).findByTestId('next-approvals')
    fireEvent.click(within(approvals).getByTestId('next-approval-toggle-fs_read'))
    await within(dialog).findByTestId('next-unsaved-note')
    // Footer Save must be disabled again — the child draft cannot be persisted
    // by the roster-only save, so closing would lose it.
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('surfaces a runtime-failed saved spec through an inline notice', async () => {
    // GPT guard-removal: a saved spec with runtime.status==='failed' returns 200
    // with an available template, so without this the approvals editor shows
    // normal toggles over a mate that cannot start. The classic pane surfaced it.
    mockCaps.get.mockResolvedValue({
      ...CAPS_VIEW,
      runtime: { status: 'failed', saved_revision: 'rev-1', sessions: [], error_code: 'materialization_changed' },
    } as CapabilityView)
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals = await within(dialog).findByTestId('next-approvals')
    expect(await within(approvals).findByTestId('next-approvals-runtime-failed')).toBeInTheDocument()
  })

  it('cascades: turning an MCP server off removes the server AND its auto-approved verbs', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals = await within(dialog).findByTestId('next-approvals')

    // The @github server renders as a collapsible parent (not a flat verb row).
    const serverToggle = within(approvals).getByTestId('next-approvals-server-toggle-github')
    expect(serverToggle).toHaveAttribute('aria-checked', 'true')

    // Turn the server OFF — this must cascade to its verbs so the draft is
    // coherent (backend 409s approval_exclusion_overlaps otherwise).
    fireEvent.click(serverToggle)

    // Review → the draft handed to preview carries the server remove plus a
    // remove for every present verb, all in one operations list.
    const save = await within(approvals).findByTestId('next-approvals-save')
    fireEvent.click(save)
    await waitFor(() => expect(mockCaps.preview).toHaveBeenCalled())
    const draft = mockCaps.preview.mock.calls.at(-1)![1] as { operations: { section: string; id: string; action: string }[] }
    const removes = draft.operations.filter(o => o.action === 'remove')
    expect(removes).toEqual(expect.arrayContaining([
      { section: 'allowedTools', id: '@github', action: 'remove' },
      { section: 'autoApprove', id: '@github/create_issue', action: 'remove' },
      { section: 'autoApprove', id: '@github/list_issues', action: 'remove' },
    ]))
    // No stray set/keep of a verb whose server is being removed.
    expect(draft.operations.some(o => o.id.startsWith('@github') && o.action !== 'remove')).toBe(false)
  })

  it('server toggle off→on is a clean round-trip (no stranded verb removes)', async () => {
    // Regression: the on-path cascaded only the glob, leaving the off-step's
    // verb removes stranded in pending → the next Review sent `set @github`
    // alongside orphan verb removes → approval_exclusion_overlaps 409. Off then
    // on must net to no change (dirty clears), so no illegal draft is built.
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals = await within(dialog).findByTestId('next-approvals')
    const serverToggle = within(approvals).getByTestId('next-approvals-server-toggle-github')

    fireEvent.click(serverToggle)   // off → remove glob + all verbs
    await within(dialog).findByTestId('next-unsaved-note')
    fireEvent.click(within(approvals).getByTestId('next-approvals-server-toggle-github'))  // on → restore all

    // Net no-op: the dirty indicator is gone, so there is nothing to Review and
    // no draft (illegal or otherwise) can be sent.
    await waitFor(() =>
      expect(within(dialog).queryByTestId('next-unsaved-note')).not.toBeInTheDocument(),
    )
    expect(within(approvals).queryByTestId('next-approvals-save')).not.toBeInTheDocument()
    expect(serverToggle).toHaveAttribute('aria-checked', 'true')
  })

  it('renders glob-covered server verbs read-only (no per-verb toggle, cannot 409)', async () => {
    // @github has an `@github` allowedTools glob AND per-verb autoApprove rows.
    // The backend treats glob + per-verb as mutually overlapping, so carving one
    // verb out 409s (approval_exclusion_overlaps). The editor must not offer a
    // per-verb toggle for this shape — only the whole-server toggle is legal.
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals = await within(dialog).findByTestId('next-approvals')

    // Expand the glob-covered server.
    fireEvent.click(within(approvals).getByTestId('next-approvals-server-expand-github'))

    // The verbs appear read-only: a static row, NOT an interactive toggle.
    expect(await within(approvals).findByTestId('next-approval-readonly-@github/create_issue')).toBeInTheDocument()
    expect(within(approvals).getByTestId('next-approval-readonly-@github/list_issues')).toBeInTheDocument()
    // No per-verb toggle is rendered for a glob-covered verb.
    expect(within(approvals).queryByTestId('next-approval-toggle-@github/create_issue')).not.toBeInTheDocument()
    // The whole-server toggle is still the one legal control.
    expect(within(approvals).getByTestId('next-approvals-server-toggle-github')).toBeEnabled()
  })

  it('renders a verb-less allowed MCP server as a non-expandable "whole server" row', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals = await within(dialog).findByTestId('next-approvals')

    // builder-mcp has an `@builder-mcp` parent grant (allowedTools) and no listed
    // verbs: the whole server is auto-approved. Its row exists and its allow-
    // toggle works, but there is no expand control and no "0 of 0". The subtitle
    // must say the whole server is auto-approved — NOT "No tools auto-approved",
    // which would contradict the on switch and understate the grant.
    const builderRow = within(approvals).getByTestId('next-approvals-server-builder-mcp')
    expect(builderRow).toBeInTheDocument()
    expect(within(approvals).getByTestId('next-approvals-server-toggle-builder-mcp')).toBeInTheDocument()
    expect(within(approvals).queryByTestId('next-approvals-server-expand-builder-mcp')).not.toBeInTheDocument()
    expect(within(builderRow).getByText('Auto-approved as a whole — turn off the server to choose tools one by one')).toBeInTheDocument()
    expect(within(builderRow).queryByText('No tools auto-approved')).not.toBeInTheDocument()
  })

  it('shows the ceiling-expansion warning when the preview flags it', async () => {
    mockCaps.preview.mockResolvedValue({
      ...CAPS_VIEW, preview_token: 'tok-1',
      impact: [{ section: 'allowedTools', id: 'fs_read', member: 'oncall', change: 'added', approval_expanded: true }],
    } as CapabilityPreview)
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-approvals'))
    const approvals = await within(dialog).findByTestId('next-approvals')
    fireEvent.click(within(approvals).getByTestId('next-approval-toggle-fs_read'))
    fireEvent.click(await within(approvals).findByTestId('next-approvals-save'))
    expect(await within(dialog).findByTestId('next-approvals-expanded')).toBeInTheDocument()
  })

  it('threads hideKindBadge and the slug member_id to the Schedules pane', async () => {
    wakeProps.last = null
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-schedules'))
    await within(dialog).findByTestId('stub-wake')
    // The redundant "schedule" chip is suppressed inside the schedules-only tab.
    expect(wakeProps.last?.hideKindBadge).toBe(true)
    // member_id defaults to the editing name when no slug is supplied.
    expect(wakeProps.last?.memberId).toBe('oncall')
  })

  it('renders the model readout on the General page', async () => {
    mockApi.agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5', pinned: true, kiro_agent: 'oncall-agent', reasoning_effort: '', effort_pinned: false })
    const dialog = await openNew()
    // Model moved from the Behavior/Model tab onto the General page.
    expect(await within(dialog).findByTestId('next-general-model')).toBeInTheDocument()
    await waitFor(() => expect(mockApi.agentResolvedModel).toHaveBeenCalled())
  })

  it('names the resolved fallback when a foreign pin is unserved', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({
      agents: [DEFAULT_CREW, { ...OTHER_CREW, model: 'claude-fable-5' }], default_agent: 'kirocrew',
    })
    mockApi.agentResolvedModel.mockResolvedValue({
      model: 'openai.gpt-5.6-terra', pinned: true, kiro_agent: 'oncall-agent', reasoning_effort: '', effort_pinned: false,
      models_backend: 'codex', pin_served_as: '',
    })
    const dialog = await openNew()
    const readout = await within(dialog).findByText(/Using openai\.gpt-5\.6-terra/)
    expect(within(dialog).queryByTestId('next-model-pin-unserved')).toBeNull()
    expect(readout.parentElement).toHaveTextContent('taken from the custom agent it is built from.')
    expect(readout.parentElement).not.toHaveTextContent('chosen for this mate.')
  })

  it('does not claim a stored pin is in use when its harness serves it under no id', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({
      agents: [DEFAULT_CREW, { ...OTHER_CREW, model: 'claude-opus-5.5' }], default_agent: 'kirocrew',
    })
    mockApi.agentResolvedModel.mockResolvedValue({
      model: 'claude-opus-5.5', pinned: true, kiro_agent: 'oncall-agent', reasoning_effort: '', effort_pinned: false,
      models_backend: 'codex', pin_served_as: '',
    })
    const dialog = await openNew()
    const readout = await within(dialog).findByTestId('next-model-pin-unserved')
    // The session leaves the pin unsent and stays on the harness's default model.
    expect(readout.textContent).toMatch(/doesn't offer claude-opus-5\.5, so this mate's sessions run on .+'s default model instead/)
    expect(within(dialog).queryByText(/Using claude-opus-5\.5/)).toBeNull()
  })
})

describe('CrewEditorNextDialog — General edits, footer, danger', () => {
  it('edits the display name and saves through the update mutation', async () => {
    const dialog = await openNew()
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'On-Call Bot' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
  })

  it('shows the unsaved-changes note when a pane is dirty', async () => {
    const dialog = await openNew()
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'Renamed' } })
    expect(await within(dialog).findByTestId('next-unsaved-note')).toBeInTheDocument()
  })

  it('persists a template switch immediately when a different template is picked', async () => {
    const dialog = await openNew()
    const kirocrewOption = within(dialog).getAllByRole('option').find(o => o.textContent === 'kirocrew')
    expect(kirocrewOption).toBeTruthy()
    fireEvent.click(kirocrewOption!)
    await waitFor(() =>
      expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', expect.objectContaining({ kiro_agent: 'kirocrew' })),
    )
  })

  it('surfaces templateSwitchError inline when the repoint is refused', async () => {
    mockApi.updateKirocrewAgent.mockRejectedValue(new Error("Template 'kirocrew' is crew 'other's private copy; it cannot be bound to another crew."))
    const dialog = await openNew()
    const kirocrewOption = within(dialog).getAllByRole('option').find(o => o.textContent === 'kirocrew')
    fireEvent.click(kirocrewOption!)
    const err = await within(dialog).findByTestId('next-template-switch-error')
    expect(err).toHaveTextContent(/private copy/i)
  })

  it('runs the two-step danger delete', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-danger'))
    fireEvent.click(await within(dialog).findByTestId('next-delete-crew'))
    fireEvent.click(await within(dialog).findByTestId('next-confirm-delete-crew'))
    await waitFor(() => expect(mockApi.deleteKirocrewAgent).toHaveBeenCalled())
  })

  it('cancels the armed danger delete', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-danger'))
    fireEvent.click(await within(dialog).findByTestId('next-delete-crew'))
    fireEvent.click(await within(dialog).findByTestId('next-cancel-delete-crew'))
    expect(within(dialog).queryByTestId('next-confirm-delete-crew')).toBeNull()
  })

  it('hides the Danger Zone for the default mate (the backend 409s its delete)', async () => {
    // GPT guard-removal: the classic editor excluded the Danger Zone for the
    // default crew because DELETE /api/agents/{default} always 409s. Opening the
    // default crew must show no Danger Zone rail entry or panel.
    const dialog = await openNew({ openCrew: 'kirocrew' })
    expect(within(dialog).queryByTestId('next-rail-danger')).toBeNull()
    expect(within(dialog).queryByTestId('next-pane-danger')).toBeNull()
  })

  it('raises the discard prompt when closing with unsaved edits', async () => {
    const dialog = await openNew()
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'Renamed' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(await screen.findByTestId('crew-sched-discard-confirm')).toBeInTheDocument()
  })
})

describe('CrewEditorNextDialog — Memory', () => {
  it('renders the private-store empty state for a crew with no V2 store', async () => {
    // kirocrew's memory_store is core-mem (not a V2 private store) — memoryStore
    // resolves empty, so the Memory pane shows its empty state.
    mockApi.kirocrewAgents.mockResolvedValue({ agents: [DEFAULT_CREW, { ...OTHER_CREW, memory_store: '' }], default_agent: 'kirocrew' })
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-memory'))
    expect(await within(dialog).findByTestId('next-pane-memory')).toBeInTheDocument()
  })

  it('renders the Profile memory tab for a crew with a private V2 store', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-memory'))
    // oncall-mem is a private V2 store owned by oncall, so Profile renders.
    expect(await within(dialog).findByTestId('next-memory-profile')).toBeInTheDocument()
  })

  it('names a private V2 store in the status line', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-memory'))
    expect(await within(dialog).findByTestId('next-memory-status')).toHaveTextContent('This mate has its own memory (V2).')
  })

  it('shows only the diagnostic for a store another member owns', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {}, 'oncall-mem': { memory_version: 2, owner_member: 'someone-else' } } })
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-memory'))
    expect(await within(dialog).findByTestId('next-memory-status')).toHaveTextContent(
      'This member’s configured memory store belongs to another member. It cannot be used here. Inspect the cause on the gateway: kirocrew doctor',
    )
    expect(within(dialog).queryByTestId('next-memory-tab-memories')).toBeNull()
    expect(within(dialog).queryByTestId('next-memory-profile')).toBeNull()
  })

  it('keeps an unsaved Profile draft, read-only, when the binding breaks mid-edit', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-memory'))
    const prefs = await within(dialog).findByRole('textbox', { name: 'Preferences' })
    fireEvent.change(prefs, { target: { value: 'typed but not saved' } })
    // Another session re-owns the store; the editor's config read refetches.
    mockApi.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {}, 'oncall-mem': { memory_version: 2, owner_member: 'someone-else' } } })
    await act(async () => { await lastQc!.refetchQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(await within(dialog).findByText(/belongs to another member/)).toBeInTheDocument()
    const kept = within(dialog).getByRole('textbox', { name: 'Preferences' })
    expect(kept).toHaveValue('typed but not saved')
    expect(kept).toBeDisabled()
  })
})

describe('CrewEditorNextDialog — save, chat hand-off, and sharing collisions', () => {
  it('footer Save persists the crew-level fields with the edited values', async () => {
    const dialog = await openNew()
    // Dirty a crew-level field so the roster-only save has something to persist.
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'On-Call Pager' } })
    fireEvent.click(screen.getByTestId('ctl-save'))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    const call = mockApi.updateKirocrewAgent.mock.calls.at(-1)
    expect(call?.[0]).toBe('oncall')
    expect(call?.[1]?.display_name).toBe('On-Call Pager')
  })

  it('reports mates sharing the edited crew\u2019s workspace or memory store', async () => {
    // A third crew shares oncall's workspace AND a fourth shares its store, so
    // collidingCrews is their de-duplicated union.
    mockApi.kirocrewAgents.mockResolvedValue({
      agents: [
        DEFAULT_CREW,
        OTHER_CREW,
        { name: 'pager', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'pager-mem' },
        { name: 'relay', kiro_agent: 'oncall-agent', workspace: 'relay-ws', memory_store: 'oncall-mem' },
      ],
      default_agent: 'kirocrew',
    })
    await openNew()
    await waitFor(() => {
      const text = screen.getByTestId('ctl-colliding').textContent ?? ''
      expect(text.split(',').filter(Boolean).sort()).toEqual(['pager', 'relay'])
    })
  })
})
