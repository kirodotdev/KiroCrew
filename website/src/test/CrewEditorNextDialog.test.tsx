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
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { CapabilityView, CapabilityPreview } from '../api/crewCapabilities'

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
vi.mock('../api/crewCapabilities', async (orig) => {
  const real = await orig<typeof import('../api/crewCapabilities')>()
  return { ...real, crewCapabilitiesApi: mockCaps }
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
vi.mock('../pages/overview/MemoryDocCard', () => ({ default: () => <div data-testid="stub-memory-doc" /> }))
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
    { section: 'autoApprove', id: '@github/create_issue', label: 'create_issue', state: 'inherited', present: false, value: null },
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
      <CrewEditorModal ctl={ctl} />
    </>
  )
}

function renderHost(opts?: { memberId?: string; openCrew?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
    // No classic dialog, and the retired Classic/New toggle is gone.
    expect(screen.queryByRole('dialog', { name: /Edit crewmate/ })).toBeNull()
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
      ['behavior', 'next-pane-behavior'],
      ['memory', 'next-pane-memory'],
      ['danger', 'next-pane-danger'],
    ] as const) {
      fireEvent.click(within(dialog).getByTestId(`next-rail-${key}`))
      await waitFor(() => expect(within(dialog).getByTestId(`next-rail-${key}`)).toHaveAttribute('aria-selected', 'true'))
      if (pane) expect(await within(dialog).findByTestId(pane)).toBeInTheDocument()
    }
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

describe('CrewEditorNextDialog — Built From inspectors (read-only)', () => {
  it('shows loading then the details stats once capabilities resolve', async () => {
    let resolveCaps: (v: CapabilityView) => void = () => {}
    mockCaps.get.mockReturnValue(new Promise((res) => { resolveCaps = res }))
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    expect(await within(dialog).findByText(/loading/i)).toBeInTheDocument()
    resolveCaps(CAPS_VIEW)
    // Details stats: tools jump-tile shows the present tools count (2).
    const toolsStat = await within(dialog).findByTestId('next-builtfrom-stat-tools')
    expect(toolsStat).toHaveTextContent('2')
  })

  it('surfaces an error when the capabilities read fails', async () => {
    mockCaps.get.mockRejectedValue(new Error('caps boom'))
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    expect(await within(dialog).findByTestId('next-builtfrom-error')).toBeInTheDocument()
  })

  it('lists tools grouped with read-only / can-modify badges', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    await within(dialog).findByTestId('next-builtfrom-details')
    fireEvent.click(within(dialog).getByTestId('next-builtfrom-tab-tools'))
    const tools = await within(dialog).findByTestId('next-builtfrom-tools')
    expect(within(tools).getByText('fs_read')).toBeInTheDocument()
    expect(within(tools).getByText('fs_write')).toBeInTheDocument()
  })

  it('expands an MCP server row to env/header NAMES only, never a secret value', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    await within(dialog).findByTestId('next-builtfrom-details')
    fireEvent.click(within(dialog).getByTestId('next-builtfrom-tab-mcp'))
    const mcp = await within(dialog).findByTestId('next-builtfrom-mcp')

    // Expand the stdio server: its env NAME shows, its value must not.
    fireEvent.click(within(mcp).getByTestId('next-mcp-row-local-fs'))
    const detail = await within(dialog).findByTestId('next-mcp-detail-local-fs')
    expect(within(detail).getByText('API_KEY')).toBeInTheDocument()
    expect(within(detail).queryByText(/SECRET-VALUE/)).toBeNull()

    // Expand the HTTP server: its header NAME shows, the token value must not.
    fireEvent.click(within(mcp).getByTestId('next-mcp-row-github'))
    const httpDetail = await within(dialog).findByTestId('next-mcp-detail-github')
    expect(within(httpDetail).getByText(/Authorization/)).toBeInTheDocument()
    expect(within(httpDetail).queryByText(/SECRET-TOKEN/)).toBeNull()
  })

  it('lists skills with a de-slugified label and expands to the raw id', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    await within(dialog).findByTestId('next-builtfrom-details')
    fireEvent.click(within(dialog).getByTestId('next-builtfrom-tab-skills'))
    const skills = await within(dialog).findByTestId('next-builtfrom-skills')
    // "writing-tests" → "Writing Tests"; owner "kirocrew-core" shown muted.
    expect(within(skills).getByText('Writing Tests')).toBeInTheDocument()
    fireEvent.click(within(skills).getByTestId('next-skill-row-kirocrew-core/writing-tests'))
    const detail = await within(dialog).findByTestId('next-skill-detail-kirocrew-core/writing-tests')
    expect(within(detail).getByText('kirocrew-core/writing-tests')).toBeInTheDocument()
  })

  it('jumps from a details stat tile to its tab', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-builtFrom'))
    const skillsStat = await within(dialog).findByTestId('next-builtfrom-stat-skills')
    fireEvent.click(skillsStat)
    expect(await within(dialog).findByTestId('next-builtfrom-skills')).toBeInTheDocument()
  })
})

describe('CrewEditorNextDialog — Behavior / Approvals', () => {
  it('renders the approvals editor and runs toggle → review → save', async () => {
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-behavior'))
    // Approvals is the default behavior tab.
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

  it('a dirty approval draft does not enable footer Save and blocks section nav', async () => {
    // GPT #1/#2/#4: footer Save runs the roster-only save and closes, so a
    // child draft (approvals/memory) it cannot persist must NOT enable it —
    // otherwise saving silently drops the draft. And navigating away must not
    // unmount-and-destroy the live draft. The unsaved note still shows.
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-behavior'))
    const approvals = await within(dialog).findByTestId('next-approvals')
    fireEvent.click(within(approvals).getByTestId('next-approval-toggle-fs_read'))
    // The editor now reports a dirty child pane.
    expect(await within(dialog).findByTestId('next-unsaved-note')).toBeInTheDocument()
    // Footer Save is NOT enabled by the child draft alone.
    expect(within(dialog).getByRole('button', { name: 'Save changes' })).toBeDisabled()
    // Navigating to another section is blocked while the draft is live, so the
    // approvals panel is not unmounted and the draft survives.
    fireEvent.click(within(dialog).getByTestId('next-rail-general'))
    expect(within(dialog).getByTestId('next-approvals')).toBeInTheDocument()
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
    fireEvent.click(within(dialog).getByTestId('next-rail-behavior'))
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
    fireEvent.click(within(dialog).getByTestId('next-rail-behavior'))
    const approvals = await within(dialog).findByTestId('next-approvals')
    expect(await within(approvals).findByTestId('next-approvals-runtime-failed')).toBeInTheDocument()
  })

  it('shows the ceiling-expansion warning when the preview flags it', async () => {
    mockCaps.preview.mockResolvedValue({
      ...CAPS_VIEW, preview_token: 'tok-1',
      impact: [{ section: 'allowedTools', id: 'fs_read', member: 'oncall', change: 'added', approval_expanded: true }],
    } as CapabilityPreview)
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-behavior'))
    const approvals = await within(dialog).findByTestId('next-approvals')
    fireEvent.click(within(approvals).getByTestId('next-approval-toggle-fs_read'))
    fireEvent.click(await within(approvals).findByTestId('next-approvals-save'))
    expect(await within(dialog).findByTestId('next-approvals-expanded')).toBeInTheDocument()
  })

  it('threads hideKindBadge and the slug member_id to the Schedules pane', async () => {
    wakeProps.last = null
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-behavior'))
    fireEvent.click(within(dialog).getByTestId('next-behavior-tab-schedules'))
    await within(dialog).findByTestId('stub-wake')
    // The redundant "schedule" chip is suppressed inside the schedules-only tab.
    expect(wakeProps.last?.hideKindBadge).toBe(true)
    // member_id defaults to the editing name when no slug is supplied.
    expect(wakeProps.last?.memberId).toBe('oncall')
  })

  it('renders the model readout on the Model tab', async () => {
    mockApi.agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5', pinned: true, kiro_agent: 'oncall-agent', reasoning_effort: '', effort_pinned: false })
    const dialog = await openNew()
    fireEvent.click(within(dialog).getByTestId('next-rail-behavior'))
    fireEvent.click(within(dialog).getByTestId('next-behavior-tab-model'))
    expect(await within(dialog).findByTestId('next-behavior-model')).toBeInTheDocument()
    await waitFor(() => expect(mockApi.agentResolvedModel).toHaveBeenCalled())
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

  it('hides the Danger Zone for the default crewmate (the backend 409s its delete)', async () => {
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
})
