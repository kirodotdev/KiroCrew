/**
 * CrewEditorDialog + useCrewEditor — the in-place bot editor (CREW-18688).
 *
 * MembersPage.test.tsx mocks BOTH of these modules (it asserts the pill opens
 * the editor, not the editor's own behaviour), so their real code is exercised
 * here against the same harness pattern the sibling crew-manager editor test
 * uses. This drives the REAL hook through a tiny host that mirrors how
 * MembersPage wires it: a roster query gated on `editingCrew`, handed into the
 * hook, with CrewEditorDialog rendering the controller.
 *
 * Covers the findings this PR fixes:
 *  - the option reads (installed templates / workspaces / config / models) are
 *    gated on the editor being open, not fired by merely mounting the host;
 *  - a failed options read renders the options-load-error notice (F2), never
 *    the ['kirocrew']/['default'] fallbacks presented as authoritative;
 *  - the editor shows a loading state during the roster-read window rather than
 *    being a silent dead click.
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
}))
vi.mock('../api/client', () => ({ api: mockApi }))

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

/* The capability / avatar / wake / webhook sub-panels reach live services and
   are not this test's subject; stub them to plain markers so the dialog mounts. */
vi.mock('../components/crew/CrewCapabilitiesPane', () => ({ default: () => <div data-testid="stub-capabilities" /> }))
const wakeProps = vi.hoisted(() => ({ last: null as null | { crew?: string; memberId?: string } }))
vi.mock('../components/CrewWakeSection', () => ({
  default: (props: { crew?: string; memberId?: string }) => {
    wakeProps.last = { crew: props.crew, memberId: props.memberId }
    return <div data-testid="stub-wake" data-member-id={props.memberId} data-crew={props.crew} />
  },
}))
vi.mock('../components/CrewWebhookSection', () => ({ default: () => <div data-testid="stub-webhook" /> }))
vi.mock('../components/CrewAvatarBuilder', () => ({
  default: ({ open, onSave }: { open?: boolean; onSave?: (a: unknown) => void }) =>
    open
      ? (
        <div data-testid="stub-avatar-builder">
          <button
            data-testid="stub-apply-image-avatar"
            onClick={() => onSave?.({ kind: 'image', v: 2, pendingData: 'data:image/png;base64,aGVsbG8=' })}
          >apply image</button>
        </div>
      )
      : null,
}))

import CrewEditorDialog from '../components/crew/CrewEditorDialog'
import { useCrewEditor } from '../components/crew/useCrewEditor'
import type { KiroCrewAgent } from '../api/types'

const DEFAULT_CREW = { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'core-ws', memory_store: 'core-mem' }
const OTHER_CREW = { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-mem' }
const AGENTS_RESPONSE = { agents: [DEFAULT_CREW, OTHER_CREW], default_agent: 'kirocrew' }
/** The Model pane's list and effort answer arrive through a chain: resolved-model names the pin's
 *  backend, then that backend's model list loads (or refetches, after a Retry). */
const PIN_CHAIN = { timeout: 5000 }

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
})

/* A tiny host mirroring MembersPage's wiring: the pill click sets editingCrew,
   which gates the roster read and drives the hook. `leaveSpy` stands in for the
   page's navigation guard so the Chat / Manage-memory route-leaving buttons can
   be asserted to go through it. */
const leaveSpy = vi.fn((perform: () => void | Promise<void>) => { void perform() })

function Host({ memberId }: { memberId?: string } = {}) {
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
      <button data-testid="open-editor" onClick={() => setEditingCrew('oncall')}>edit</button>
      <CrewEditorDialog ctl={ctl} />
    </>
  )
}

function renderHost(opts?: { memberId?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({ reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter><Host memberId={opts?.memberId} /></MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('CrewEditorDialog — in-place bot editor (CREW-18688)', () => {
  it('fires no option reads until the editor is opened, then loads them once open', async () => {
    renderHost()
    // Merely mounting the host must not read installed templates, workspaces,
    // config or the model list — those are the reads the ungated version fired
    // for an editor the user never opened (Opus perf finding).
    expect(mockApi.agentsInstalled).not.toHaveBeenCalled()
    expect(mockApi.workspaces).not.toHaveBeenCalled()
    expect(mockApi.kirocrewConfig).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('open-editor'))
    expect(await screen.findByRole('dialog', { name: /Edit crewmate/ })).toBeInTheDocument()
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
    await waitFor(() => expect(mockApi.kirocrewConfig).toHaveBeenCalled())
  })

  it('shows a loading state while the roster read is in flight, not a silent dead click', async () => {
    // Hold the roster read open so `open` cannot flip yet.
    let resolveRoster: (v: typeof AGENTS_RESPONSE) => void = () => {}
    mockApi.kirocrewAgents.mockReturnValue(new Promise((res) => { resolveRoster = res }))
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    expect(await screen.findByTestId('crew-editor-loading')).toBeInTheDocument()
    resolveRoster(AGENTS_RESPONSE)
    expect(await screen.findByRole('dialog', { name: /Edit crewmate/ })).toBeInTheDocument()
    expect(screen.queryByTestId('crew-editor-loading')).toBeNull()
  })

  it('surfaces a notice when the editor option reads fail, instead of the fallback lists (F2)', async () => {
    mockApi.agentsInstalled.mockRejectedValue(new Error('installed boom'))
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    await screen.findByRole('dialog', { name: /Edit crewmate/ })
    expect(await screen.findByTestId('crew-editor-options-load-error')).toBeInTheDocument()
  })

  it('walks the rail panes and renders each pane body', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    for (const key of ['template', 'model', 'place', 'schedules', 'webhook', 'routing', 'danger', 'overview']) {
      const tab = within(dialog).queryByTestId(`crew-rail-${key}`)
      if (!tab) continue
      fireEvent.click(tab)
      // A render after each switch — the pane body mounts (stubs for the
      // service-backed panes, real fields for the rest).
      await waitFor(() => expect(within(dialog).getByTestId(`crew-rail-${key}`)).toHaveAttribute('aria-selected', 'true'))
    }
  })

  it('threads the canonical slug (not the display name) to the Schedules pane as member_id', async () => {
    // Opus finding: a private schedule's `member_id` holds the crew's immutable
    // SLUG, which `wakesCrew` matches on. The pill sets the editing handle to
    // the display NAME; MembersPage passes the roster row's slug as `memberId`,
    // and the Schedules pane must receive THAT, or a renamed crewmate ("Radar"
    // → `radar`) reads as having no schedules.
    wakeProps.last = null
    renderHost({ memberId: 'oncall-slug' })
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    const schedTab = within(dialog).queryByTestId('crew-rail-schedules')
    if (schedTab) fireEvent.click(schedTab)
    await waitFor(() => expect(wakeProps.last?.memberId).toBe('oncall-slug'))
    // `crew` stays the display handle (the legacy-schedule comparison path).
    expect(wakeProps.last?.crew).toBe('oncall')
  })

  it('defaults member_id to the editing name when no slug is supplied (crew-manager caller)', async () => {
    wakeProps.last = null
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    const schedTab = within(dialog).queryByTestId('crew-rail-schedules')
    if (schedTab) fireEvent.click(schedTab)
    await waitFor(() => expect(wakeProps.last?.memberId).toBe('oncall'))
  })

  it('edits the display name and saves through the update mutation', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    const nameField = within(dialog).getByTestId('display-name-input')
    fireEvent.change(nameField, { target: { value: 'On-Call Bot' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
  })

  it('arms and runs the delete confirm in the danger pane', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    const dangerTab = within(dialog).queryByTestId('crew-rail-danger')
    if (!dangerTab) return
    fireEvent.click(dangerTab)
    // Two-step: 'Delete crewmate' arms the confirm row, then confirm runs it.
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Delete crewmate' }))
    fireEvent.click(await within(dialog).findByTestId('confirm-delete-crew'))
    await waitFor(() => expect(mockApi.deleteKirocrewAgent).toHaveBeenCalled())
  })

  it('offers the models of the harness that judges the crew\'s pin, not the configured one', async () => {
    // A crewmate DM thread on Claude Code under a kiro-cli default: kiro ids are not its choices.
    mockApi.agentResolvedModel.mockResolvedValue({ model: '', pinned: false, kiro_agent: 'oncall-agent', models_backend: 'claude' })
    mockApi.models.mockImplementation((backend?: string) => Promise.resolve(backend === 'claude'
      ? [{ model_name: 'global.anthropic.claude-opus-5-5[1m]' }]
      : [{ model_name: 'claude-fable-5.1' }]))
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    fireEvent.click(within(dialog).getByTestId('crew-rail-model'))

    expect(await within(dialog).findByRole('option', { name: 'global.anthropic.claude-opus-5-5[1m]' }, PIN_CHAIN)).toBeInTheDocument()
    expect(within(dialog).queryByRole('option', { name: 'claude-fable-5.1' })).toBeNull()
  })

  it('says only Inherited is left when the pin\'s harness list fails to load, and retries it', async () => {
    // That list keeps no last-good copy, so a failed fetch leaves only Inherited.
    mockApi.agentResolvedModel.mockResolvedValue({ model: '', pinned: false, kiro_agent: 'oncall-agent', models_backend: 'claude' })
    mockApi.models.mockImplementation((backend?: string) => (backend === 'claude'
      ? Promise.reject(new Error('Service Unavailable'))
      : Promise.resolve([{ model_name: 'claude-fable-5.1' }])))
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    fireEvent.click(within(dialog).getByTestId('crew-rail-model'))

    expect(await within(dialog).findByTestId('crew-editor-models-degraded', {}, PIN_CHAIN))
      .toHaveTextContent('Couldn\'t load Claude Code\'s models, so only “Inherited” is offered.')
    // The notice reads the degraded flag, which flips before the failed fetch settles.
    const retry = within(dialog).getByRole('button', { name: 'Retry' })
    await waitFor(() => expect(retry).toBeEnabled(), PIN_CHAIN)
    mockApi.models.mockResolvedValue([{ model_name: 'global.anthropic.claude-opus-5-5[1m]' }])
    fireEvent.click(retry)

    expect(await within(dialog).findByRole('option', { name: 'global.anthropic.claude-opus-5-5[1m]' }, PIN_CHAIN)).toBeInTheDocument()
    expect(within(dialog).queryByTestId('crew-editor-models-degraded')).toBeNull()
  })

  it('offers no effort pin where the crew\'s harness takes none', async () => {
    // A codex build that advertises no effort option drops every level a crew pins.
    mockApi.agentResolvedModel.mockResolvedValue({
      model: 'claude-opus-5', pinned: true, kiro_agent: 'oncall-agent', reasoning_effort: '',
      effort_pinned: false, effort_supported: false, effort_levels: [],
    })
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    fireEvent.click(within(dialog).getByTestId('crew-rail-model'))
    await waitFor(() => expect(mockApi.agentResolvedModel).toHaveBeenCalled(), PIN_CHAIN)

    await waitFor(() => expect(within(dialog).queryByRole('combobox', { name: 'Edit reasoning effort' })).toBeNull(), PIN_CHAIN)
  })

  it('offers an effort pin where the crew\'s harness takes one on a model the name check does not know', async () => {
    // A pi crew runs the operator's own model ids, and the name check recognises none of them.
    mockApi.agentResolvedModel.mockResolvedValue({
      model: 'ollama/qwen3', pinned: false, kiro_agent: 'oncall-agent', reasoning_effort: '',
      effort_pinned: false, effort_supported: true, effort_levels: ['low', 'high'],
    })
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    fireEvent.click(within(dialog).getByTestId('crew-rail-model'))

    expect(await within(dialog).findByRole('combobox', { name: 'Edit reasoning effort' }, PIN_CHAIN)).toBeInTheDocument()
  })

  it('edits fields on the model, place and routing panes and shows the resolved readout', async () => {
    mockApi.agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5', pinned: true, kiro_agent: 'oncall-agent', reasoning_effort: '', effort_pinned: false })
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())

    // Model pane: pick a model (dirties the pane, drives the resolved readout).
    fireEvent.click(within(dialog).getByTestId('crew-rail-model'))
    await waitFor(() => expect(mockApi.agentResolvedModel).toHaveBeenCalled())

    // Place pane: switch the workspace, then open the create-workspace modal.
    fireEvent.click(within(dialog).getByTestId('crew-rail-place'))
    const placeCombo = within(dialog).getAllByRole('option').find(o => o.textContent === 'oncall')
    if (placeCombo) fireEvent.click(placeCombo)

    // Routing pane: edit triggers and open the avatar builder.
    fireEvent.click(within(dialog).getByTestId('crew-rail-routing'))
    fireEvent.click(within(dialog).getByTestId('open-avatar-builder'))
    expect(await screen.findByTestId('stub-avatar-builder')).toBeInTheDocument()
  })

  it('raises the discard prompt when closing with unsaved edits and keeps editing on cancel', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    // Dirty the overview pane so a close must confirm.
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'Renamed' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    // The discard prompt appears; "Keep editing" dismisses it without closing.
    fireEvent.click(await screen.findByTestId('crew-sched-discard-keep'))
    expect(within(dialog).getByTestId('display-name-input')).toHaveValue('Renamed')
  })

  it('discards and closes when the discard prompt is confirmed', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'Renamed' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByTestId('crew-sched-discard-confirm'))
    // The editor closes: its dialog leaves the DOM.
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /Edit crewmate/ })).toBeNull())
  })

  it('closes cleanly on Escape when nothing is dirty (no discard prompt)', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /Edit crewmate/ })).toBeNull())
    expect(screen.queryByTestId('crew-sched-discard-confirm')).toBeNull()
  })

  it('closes on Escape while the roster read is still loading', async () => {
    let resolveRoster: (v: typeof AGENTS_RESPONSE) => void = () => {}
    mockApi.kirocrewAgents.mockReturnValue(new Promise((res) => { resolveRoster = res }))
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    expect(await screen.findByTestId('crew-editor-loading')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('crew-editor-loading')).toBeNull())
    resolveRoster(AGENTS_RESPONSE)
  })

  it('cancels the armed delete confirm in the danger pane', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    const dangerTab = within(dialog).queryByTestId('crew-rail-danger')
    if (!dangerTab) return
    fireEvent.click(dangerTab)
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Delete crewmate' }))
    fireEvent.click(await within(dialog).findByTestId('cancel-delete-crew'))
    // Back to the un-armed state: the confirm button is gone.
    expect(within(dialog).queryByTestId('confirm-delete-crew')).toBeNull()
  })

  it('offers Manage memory on the place pane and routes it through the leave guard', async () => {
    leaveSpy.mockClear()
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.kirocrewConfig).toHaveBeenCalled())
    fireEvent.click(within(dialog).getByTestId('crew-rail-place'))
    // oncall-mem is a private V2 store owned by oncall, so the Manage control
    // renders; its click LEAVES /members, so it must go through the page's
    // leave guard (Opus finding) rather than a raw navigate.
    const manage = await within(dialog).findByRole('button', { name: /manage/i })
    fireEvent.click(manage)
    expect(leaveSpy).toHaveBeenCalled()
  })

  it('clears the loading spinner and shows the options error when the roster read fails', async () => {
    // agentsLoaded is true on error (data !== undefined || isError), so the
    // editor's `loading` flag clears instead of leaving a permanent spinner
    // dialog whose overlay would hide the error surface (Opus finding).
    mockApi.kirocrewAgents.mockRejectedValue(new Error('roster boom'))
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    // No permanent loading dialog — it resolves (to closed, since the record
    // never loads) rather than spinning forever.
    await waitFor(() => expect(screen.queryByTestId('crew-editor-loading')).toBeNull())
  })

  it('prompts before Chat when an ordinary field is dirty, not just a schedule draft', async () => {
    // requestChat gates on dirtyPanes.size > 0; chatWith closes the editor
    // unconditionally, so an unsaved display-name edit must raise the discard
    // prompt rather than being silently lost on the Chat hand-off (GPT finding).
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'Renamed' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Chat with this crewmate' }))
    expect(await screen.findByTestId('crew-sched-discard-confirm')).toBeInTheDocument()
  })

  it('dismisses the discard prompt on Escape without closing the editor', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    fireEvent.change(within(dialog).getByTestId('display-name-input'), { target: { value: 'Renamed' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await screen.findByTestId('crew-sched-discard-confirm')
    // Escape dismisses the discard prompt (its own onOpenChange) — the editor
    // stays open with the edit intact.
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('crew-sched-discard-confirm')).toBeNull())
    expect(within(dialog).getByTestId('display-name-input')).toHaveValue('Renamed')
  })

  it('stages an uploaded image avatar and promotes it on save', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    // Open the avatar builder from the routing pane and apply an image with
    // pending data — this dirties the routing pane.
    fireEvent.click(within(dialog).getByTestId('crew-rail-routing'))
    fireEvent.click(within(dialog).getByTestId('open-avatar-builder'))
    fireEvent.click(await screen.findByTestId('stub-apply-image-avatar'))
    // Saving uploads the staged bytes and sends a promote token in the payload.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.uploadCrewAvatar).toHaveBeenCalled())
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    const payload = mockApi.updateKirocrewAgent.mock.calls.at(-1)?.[1] as { avatar?: { promote?: boolean; token?: string } }
    expect(payload.avatar).toMatchObject({ kind: 'image', promote: true, token: 'staged-token' })
  })

  it('persists a template switch immediately when a different template is picked', async () => {
    renderHost()
    fireEvent.click(screen.getByTestId('open-editor'))
    const dialog = await screen.findByRole('dialog', { name: /Edit crewmate/ })
    await waitFor(() => expect(mockApi.agentsInstalled).toHaveBeenCalled())
    fireEvent.click(within(dialog).getByTestId('crew-rail-template'))
    // The template SimpleSelect (stubbed) renders each option as a button; the
    // crew starts on 'oncall-agent', so picking 'kirocrew' fires the
    // instant-save template switch (persistTemplateSwitch).
    const kirocrewOption = within(dialog).getAllByRole('option').find(o => o.textContent === 'kirocrew')
    if (!kirocrewOption) return
    fireEvent.click(kirocrewOption)
    await waitFor(() =>
      expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', expect.objectContaining({ kiro_agent: 'kirocrew' })),
    )
  })
})
