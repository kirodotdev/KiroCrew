/**
 * Crew roster (KiroCrewAgentsPage) — card grid, compact table, editor dialog.
 *
 * The page used to be a StatCard row plus an HTML table, and its tests read the
 * DOM structurally (`table tr`, nth-child cells). Those assertions could not
 * survive the rewrite and, more importantly, never covered the behaviour that
 * actually matters: which card is the default, what the editor is pre-filled
 * with, the ordering of the promote-then-save writes, and the nested-dialog
 * keyboard case. Everything here is queried by accessible name or an explicit
 * test id so a restyle cannot turn a green suite red.
 *
 * The list view IS a table again, but the assertions go through roles
 * (`columnheader`, the row's own control) rather than cell positions, so
 * reordering a column does not break them.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'

/* Render framer-motion elements as plain DOM. The side sheet is an
   AnimatePresence child with a 240ms x-translate exit, so a real
   AnimatePresence keeps the closing sheet mounted for the duration of that
   transition — which would make every "Escape closes / Escape is ignored"
   assertion pass or fail on timing rather than on behaviour. */
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
  // One component type per tag, cached: a proxy minting a fresh type per read
  // would hand React a new element type each render and remount the subtree.
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

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
  agentResolvedModel: vi.fn(),
  setDefaultAgent: vi.fn(),
  createChatSlot: vi.fn(),
  models: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mockApi }))

import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'
import CrewAvatar from '../components/CrewAvatar'

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

/* The default crew deliberately does NOT point at a workspace or memory store
   called "default": otherwise the literal "default" appears three times inside
   its own card and the `default` badge could not be asserted by text. */
const DEFAULT_CREW = {
  name: 'kirocrew',
  kiro_agent: 'kirocrew',
  workspace: 'core-ws',
  memory_store: 'core-mem',
}
const OTHER_CREW = {
  name: 'oncall',
  kiro_agent: 'oncall-agent',
  workspace: 'oncall',
  memory_store: 'oncall-mem',
  model: 'claude-opus-5',
}

const AGENTS_RESPONSE = { agents: [DEFAULT_CREW, OTHER_CREW], default_agent: 'kirocrew' }
const WORKSPACES_RESPONSE = {
  workspaces: [{ name: 'default' }, { name: 'core-ws' }, { name: 'oncall' }],
}
const INSTALLED_RESPONSE = [{ name: 'kirocrew' }, { name: 'oncall-agent' }]
const CONFIG_RESPONSE = { memory_stores: { default: {}, 'core-mem': {}, 'oncall-mem': {} } }

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.kirocrewAgents.mockResolvedValue(AGENTS_RESPONSE)
  mockApi.agentsInstalled.mockResolvedValue(INSTALLED_RESPONSE)
  mockApi.workspaces.mockResolvedValue(WORKSPACES_RESPONSE)
  mockApi.kirocrewConfig.mockResolvedValue(CONFIG_RESPONSE)
  mockApi.agentResolvedModel.mockResolvedValue({ model: '', pinned: false, kiro_agent: 'kirocrew' })
  mockApi.models.mockResolvedValue([{ model_name: 'claude-opus-5' }])
  // The mutation hooks read `.error` off the resolved body, so an undefined
  // resolution (a bare vi.fn()) would throw inside onSuccess.
  mockApi.createKirocrewAgent.mockResolvedValue({})
  mockApi.updateKirocrewAgent.mockResolvedValue({})
  mockApi.deleteKirocrewAgent.mockResolvedValue({})
  mockApi.setDefaultAgent.mockResolvedValue({})
  mockApi.createWorkspace.mockResolvedValue({ name: 'staging' })
})

/** Wait until the roster has rendered real data rather than the empty state. */
async function renderRoster(expectCards = 2) {
  const rendered = renderPage()
  await waitFor(() => expect(screen.getAllByTestId('crew-card')).toHaveLength(expectCards))
  await waitFor(() => expect(mockApi.workspaces).toHaveBeenCalled())
  await waitFor(() => expect(mockApi.kirocrewConfig).toHaveBeenCalled())
  return rendered
}

/** Escape, dispatched where Radix listens for it.
 *
 *  Radix's DismissableLayer binds `keydown` on `document`; the hand-rolled dialog
 *  this page used to render bound it on `window`. An event dispatched directly AT
 *  `window` never passes through `document`, so `fireEvent.keyDown(window, ...)`
 *  is invisible to Radix — it is not a faithful simulation either way, since a
 *  real keypress targets the focused element and bubbles up through both. */
function pressEscape() {
  fireEvent.keyDown(document, { key: 'Escape' })
}

/** A roster card, addressed by the accessible name the card exposes. */
function crewCard(name: string) {
  return screen.getByRole('button', { name: `Edit crew ${name}` })
}

/** Open the editor dialog on `name` and return the dialog element. */
async function openEditor(name: string): Promise<HTMLElement> {
  fireEvent.click(crewCard(name))
  return await screen.findByRole('dialog', { name: `Edit crew ${name}` })
}

/** Open the editor dialog in create mode and return the dialog element. */
async function openCreate(): Promise<HTMLElement> {
  fireEvent.click(screen.getByTestId('new-crew'))
  return await screen.findByRole('dialog', { name: 'Create a new crew' })
}

describe('crew roster — cards', () => {
  it('renders one card per crew and badges only the default one', async () => {
    await renderRoster()

    const cards = screen.getAllByTestId('crew-card')
    expect(cards).toHaveLength(2)

    const defaultCard = crewCard('kirocrew')
    expect(within(defaultCard).getByText('default')).toBeInTheDocument()
    expect(within(defaultCard).getByText('Used for all new chats')).toBeInTheDocument()

    const otherCard = crewCard('oncall')
    expect(within(otherCard).queryByText('default')).not.toBeInTheDocument()

    // Bindings are on the card itself — that is the whole point of the grid.
    expect(within(otherCard).getByText('oncall-agent')).toBeInTheDocument()
    expect(within(otherCard).getByText('oncall-mem')).toBeInTheDocument()
    expect(within(otherCard).getByText('claude-opus-5')).toBeInTheDocument()
    // No per-crew pin on the default crew → the model reads as inherited.
    expect(within(defaultCard).getByText('Inherited')).toBeInTheDocument()
    // Nothing collides in this fixture, so no store is flagged as shared.
    expect(within(otherCard).queryByText('shared')).not.toBeInTheDocument()
    expect(within(defaultCard).queryByText('shared')).not.toBeInTheDocument()
  })

  it('flags only the store that a second crew also points at', async () => {
    // Both crews on one memory store, distinct workspaces: the marker must land
    // on MEMORY STORE and nowhere else. A bare "Shared" badge in the header was
    // read by a first-run reviewer as "shared with my teammates", so the point
    // of this shape is that it names WHICH store is doubled up.
    mockApi.kirocrewAgents.mockResolvedValue({
      agents: [
        { ...DEFAULT_CREW, memory_store: 'core-mem' },
        { ...OTHER_CREW, workspace: 'oncall', memory_store: 'core-mem' },
      ],
      default_agent: 'kirocrew',
    })
    await renderRoster()

    for (const name of ['kirocrew', 'oncall']) {
      const card = crewCard(name)
      // One marker per card — the workspaces are distinct, so files are not shared.
      expect(within(card).getAllByText('shared')).toHaveLength(1)
    }
  })
})

describe('crew roster — isolation preview notice', () => {
  const NOTICE = /Isolated memory per crew is on the way/
  const TIP = /Isolated memory per crew is still being built/

  /* The view choice persists to localStorage, so a test here that switches to
     List would otherwise hand every later block a table instead of the cards
     they query. Cleared on both edges: before, so this block starts on cards
     whatever ran earlier; after, so it cannot leak forward. */
  beforeEach(() => localStorage.clear())
  afterEach(() => localStorage.clear())

  it('says the bindings are a preview, in both views', async () => {
    await renderRoster()
    // Page-level, so it is on screen before the user picks a view.
    expect(screen.getByText(NOTICE)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    await screen.findByRole('table')
    // Switching the layout must not take the caveat away with the cards.
    expect(screen.getByText(NOTICE)).toBeInTheDocument()
  })

  it('is not repeated on every card', async () => {
    await renderRoster()
    // The claim is about the whole surface. Two crews, one notice — a per-card
    // copy would put the same sentence on the page as many times as there are
    // crews, and the roster runs to dozens.
    expect(screen.getAllByText(NOTICE)).toHaveLength(1)
  })

  it('hangs the same caveat off the workspace and memory bindings', async () => {
    await renderRoster()
    const sheet = await openEditor('oncall')
    // Two tips, one per binding the notice is about. The editor is an overlay,
    // so the page-level notice is not readable from here — the tooltip is the
    // only place this caveat reaches a user who is mid-edit.
    expect(within(sheet).getAllByTitle(TIP)).toHaveLength(2)
  })

  it('marks the workspace and memory columns in the list view', async () => {
    await renderRoster()
    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    const table = await screen.findByRole('table')
    expect(within(table).getAllByTitle(TIP)).toHaveLength(2)
    // Each tip is a named control rather than announcing as "question mark",
    // and the header above keeps its own short name regardless.
    expect(within(table).getAllByRole('button', { name: 'More information' })).toHaveLength(2)
  })
})

describe('crew roster — filtering', () => {
  it('narrows the visible cards', async () => {
    await renderRoster()
    fireEvent.change(screen.getByRole('textbox', { name: 'Filter crews…' }), {
      target: { value: 'oncall' },
    })
    await waitFor(() => expect(screen.getAllByTestId('crew-card')).toHaveLength(1))
    expect(crewCard('oncall')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit crew kirocrew' })).not.toBeInTheDocument()
  })

  it('shows the filter empty state when nothing matches', async () => {
    await renderRoster()
    fireEvent.change(screen.getByRole('textbox', { name: 'Filter crews…' }), {
      target: { value: 'no-such-crew' },
    })
    await waitFor(() => expect(screen.queryAllByTestId('crew-card')).toHaveLength(0))
    expect(screen.getByTestId('empty-state-title')).toHaveTextContent('No crews match your filter')
  })

  it('shows the zero-crew empty state when there are no crews at all', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
    renderPage()
    await waitFor(() =>
      expect(screen.getByTestId('empty-state-title')).toHaveTextContent('No crews'),
    )
    // Distinct copy from the filter case — a first run is not a failed search.
    expect(screen.getByTestId('empty-state-title')).not.toHaveTextContent('match your filter')
    expect(screen.queryAllByTestId('crew-card')).toHaveLength(0)
  })
})

describe('crew roster — description', () => {
  it('clamps a long description to two lines and keeps the full text reachable', async () => {
    // The card used to `truncate` to ONE line, which cut nearly every real
    // description mid-word. Two lines plus the full text in the tooltip.
    const long =
      'Paged-alert triage crew — owns the runbooks, keeps the escalation ladder ' +
      'warm, and files the follow-up tickets after every page.'
    mockApi.kirocrewAgents.mockResolvedValue({
      agents: [DEFAULT_CREW, { ...OTHER_CREW, description: long }],
      default_agent: 'kirocrew',
    })
    await renderRoster()

    const desc = within(crewCard('oncall')).getByText(long)
    expect(desc.className).toContain('line-clamp-2')
    // Height is pinned alongside the clamp: without it the clamp leaks a sliver
    // of a third line, and short-description cards sit shorter than their
    // neighbours so the binding grids stop lining up across the row.
    expect(desc.className).toContain('h-[34px]')
    expect(desc).toHaveAttribute('title', long)
  })

  it('does not put an empty title on a crew with no description', async () => {
    await renderRoster()
    // DEFAULT_CREW has no description, so the card shows the default-crew line
    // instead — and must not advertise a tooltip that would render as blank.
    const filler = within(crewCard('kirocrew')).getByText('Used for all new chats')
    expect(filler).not.toHaveAttribute('title')
  })

  it('falls back to the same text in the card and the row', async () => {
    // The two views drifted: a crew with no description was blank in the card
    // but italic "No description" in the row, so the same crew read differently
    // depending on which layout you were in.
    await renderRoster()
    // OTHER_CREW is non-default with no description -> the placeholder.
    expect(within(crewCard('oncall')).getByText('No description')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    await screen.findByRole('table')
    const row = screen.getByRole('button', { name: 'Edit crew oncall' }).closest('tr')!
    expect(within(row).getByText('No description')).toBeInTheDocument()

    // And the default crew keeps its own hint in BOTH views, rather than one
    // view explaining why it matters and the other calling it undescribed.
    const defaultRow = screen.getByRole('button', { name: 'Edit crew kirocrew' }).closest('tr')!
    expect(within(defaultRow).getByText('Used for all new chats')).toBeInTheDocument()
  })
})

describe('crew roster — view toggle', () => {
  beforeEach(() => localStorage.clear())

  it('defaults to cards and switches to a table on List', async () => {
    await renderRoster()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'List' }))

    const table = await screen.findByRole('table')
    expect(screen.getAllByTestId('crew-row')).toHaveLength(2)
    // The cards are gone, not merely hidden underneath.
    expect(screen.queryAllByTestId('crew-card')).toHaveLength(0)
    // Bindings move into columns, so the header names them once instead of
    // repeating a label per card. Exact names: the workspace and memory headers
    // carry the preview InfoTip, and each `th` pins its own `aria-label` so the
    // tip's name is not concatenated into the column's.
    expect(within(table).getByRole('columnheader', { name: 'Workspace' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: 'Memory Store' })).toBeInTheDocument()
  })

  it('carries each crew’s bindings into its row', async () => {
    await renderRoster()
    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    await screen.findByRole('table')

    const row = screen.getByRole('button', { name: 'Edit crew oncall' }).closest('tr')!
    expect(within(row).getByText('oncall-agent')).toBeInTheDocument()
    expect(within(row).getByText('oncall-mem')).toBeInTheDocument()
    expect(within(row).getByText('claude-opus-5')).toBeInTheDocument()
  })

  it('opens the editor from a row', async () => {
    await renderRoster()
    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    await screen.findByRole('table')

    fireEvent.click(screen.getByRole('button', { name: 'Edit crew oncall' }))
    expect(await screen.findByRole('dialog', { name: 'Edit crew oncall' })).toBeInTheDocument()
  })

  it('opens the editor exactly once when the row itself is clicked', async () => {
    // The row is a click target for convenience AND contains a real control
    // with the same action. One gesture must not fire both.
    await renderRoster()
    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    await screen.findByRole('table')

    const nameControl = screen.getByRole('button', { name: 'Edit crew oncall' })
    fireEvent.click(nameControl)
    await screen.findByRole('dialog', { name: 'Edit crew oncall' })
    // A second dialog would mean the row handler fired on top of the control's.
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
  })

  it('remembers the choice across mounts', async () => {
    const { unmount } = await renderRoster()
    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    await screen.findByRole('table')
    expect(localStorage.getItem('mc-crews-view')).toBe('list')

    // A fresh mount reads the stored layout rather than snapping back to cards.
    unmount()
    renderPage()
    await waitFor(() => expect(screen.getAllByTestId('crew-row')).toHaveLength(2))
  })

  it('flags a doubled-up store in the row, naming which one', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({
      agents: [
        { ...DEFAULT_CREW, memory_store: 'core-mem' },
        { ...OTHER_CREW, workspace: 'oncall', memory_store: 'core-mem' },
      ],
      default_agent: 'kirocrew',
    })
    await renderRoster()
    fireEvent.click(screen.getByRole('button', { name: 'List' }))
    await screen.findByRole('table')

    const row = screen.getByRole('button', { name: 'Edit crew oncall' }).closest('tr')!
    // Memory is doubled; the workspaces are distinct, so exactly one marker.
    expect(within(row).getAllByText('shared')).toHaveLength(1)
  })

  it('is not offered when there are no crews to lay out', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
    renderPage()
    await waitFor(() =>
      expect(screen.getByTestId('empty-state-title')).toHaveTextContent('No crews'),
    )
    expect(screen.queryByRole('button', { name: 'List' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cards' })).not.toBeInTheDocument()
  })
})

describe('crew editor — opening', () => {
  it('opens pre-filled with the clicked crew’s bindings', async () => {
    await renderRoster()
    const sheet = await openEditor('oncall')

    expect(within(sheet).getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('oncall')
    expect(within(sheet).getByRole('combobox', { name: 'Memory Store' })).toHaveTextContent('oncall-mem')
    expect(within(sheet).getByRole('combobox', { name: 'Agent Template' })).toHaveTextContent('oncall-agent')
    expect(within(sheet).getByRole('combobox', { name: 'Edit default model' })).toHaveTextContent('claude-opus-5')
  })

  it('opens the create dialog from "New crew"', async () => {
    await renderRoster()
    const sheet = await openCreate()
    // Create mode has no crew to edit yet, so the bindings start on the defaults.
    expect(within(sheet).getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('default')
    expect(within(sheet).getByRole('combobox', { name: 'Memory Store' })).toHaveTextContent('default')
  })
})

describe('crew editor — create', () => {
  it('refuses an empty name without calling the api', async () => {
    await renderRoster()
    const sheet = await openCreate()

    fireEvent.click(within(sheet).getByRole('button', { name: 'Create' }))

    expect(await within(sheet).findByText('Name is required')).toBeInTheDocument()
    expect(mockApi.createKirocrewAgent).not.toHaveBeenCalled()
    // The dialog stays open so the user can fix it in place.
    expect(screen.getByRole('dialog', { name: 'Create a new crew' })).toBeInTheDocument()
  })

  it('creates the crew with the chosen bindings', async () => {
    await renderRoster()
    const sheet = await openCreate()

    const user = userEvent.setup()
    await user.type(within(sheet).getByPlaceholderText('e.g. oncall'), 'staging')
    fireEvent.click(within(sheet).getByRole('button', { name: 'Create' }))

    await waitFor(() =>
      expect(mockApi.createKirocrewAgent).toHaveBeenCalledWith({
        name: 'staging',
        kiro_agent: 'kirocrew',
        workspace: 'default',
        memory_store: 'default',
        triggers: '',
      }),
    )
  })
})

describe('crew editor — save', () => {
  it('saves the bindings for the edited crew', async () => {
    await renderRoster()
    const sheet = await openEditor('oncall')

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', {
      kiro_agent: 'oncall-agent',
      workspace: 'oncall',
      memory_store: 'oncall-mem',
      triggers: '',
      model: 'claude-opus-5',
    })
  })

  it('sends edited routing triggers', async () => {
    await renderRoster()
    const sheet = await openEditor('oncall')

    fireEvent.change(within(sheet).getByLabelText('Triggers'), {
      target: { value: 'incident, prod outage' },
    })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith(
      'oncall',
      expect.objectContaining({ triggers: 'incident, prod outage' }),
    )
  })

  it('does not touch the default from the editor at all', async () => {
    // Promotion lives on the roster bar now, not per-crew: a per-crew control
    // could only ever offer promotion (the backend refuses to unset a default
    // without naming a replacement), which read as a broken switch.
    await renderRoster()
    const sheet = await openEditor('oncall')

    expect(within(sheet).queryByRole('switch')).not.toBeInTheDocument()
    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    expect(mockApi.setDefaultAgent).not.toHaveBeenCalled()
  })
})

describe('crew editor — stale writes', () => {
  it('does not close the panel when a write for a DIFFERENT crew lands', async () => {
    // Save A, dismiss while it is in flight, then open B: A's success must not
    // dismiss B's panel or discard B's edits.
    let resolveA: (v: unknown) => void = () => {}
    mockApi.updateKirocrewAgent.mockImplementation(() => new Promise(res => { resolveA = res }))
    await renderRoster()

    const sheetA = await openEditor('oncall')
    fireEvent.click(within(sheetA).getByRole('button', { name: 'Save changes' }))
    pressEscape()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit crew oncall' })).not.toBeInTheDocument(),
    )

    const sheetB = await openEditor('kirocrew')
    resolveA({ ok: true })

    // B survives, and A's outcome is not reported against it.
    await waitFor(() => expect(mockApi.kirocrewAgents).toHaveBeenCalled())
    expect(screen.getByRole('dialog', { name: 'Edit crew kirocrew' })).toBeInTheDocument()
    expect(within(sheetB).queryByRole('button', { name: 'Save changes' })).toBeInTheDocument()
  })

  it('does not report a stale write\u2019s error against the crew now open', async () => {
    let rejectA: (e: unknown) => void = () => {}
    mockApi.updateKirocrewAgent.mockImplementation(() => new Promise((_res, rej) => { rejectA = rej }))
    await renderRoster()

    const sheetA = await openEditor('oncall')
    fireEvent.click(within(sheetA).getByRole('button', { name: 'Save changes' }))
    pressEscape()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit crew oncall' })).not.toBeInTheDocument(),
    )

    const sheetB = await openEditor('kirocrew')
    rejectA(new Error('oncall write blew up'))

    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Edit crew kirocrew' })).toBeInTheDocument())
    expect(within(sheetB).queryByText('oncall write blew up')).not.toBeInTheDocument()
  })

  it('does not close a REOPENED panel for the same crew', async () => {
    // The narrower case a name comparison could not catch: dismiss and reopen
    // the SAME crew, and the stale completion still matched by name.
    let resolveA: (v: unknown) => void = () => {}
    mockApi.updateKirocrewAgent.mockImplementation(() => new Promise(res => { resolveA = res }))
    await renderRoster()

    const first = await openEditor('oncall')
    fireEvent.click(within(first).getByRole('button', { name: 'Save changes' }))
    pressEscape()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit crew oncall' })).not.toBeInTheDocument(),
    )

    await openEditor('oncall')
    resolveA({ ok: true })

    await waitFor(() => expect(mockApi.kirocrewAgents).toHaveBeenCalled())
    expect(screen.getByRole('dialog', { name: 'Edit crew oncall' })).toBeInTheDocument()
  })

  it('does not navigate away when a stale chat request completes', async () => {
    // Navigation is the most disruptive outcome on this page, so it is guarded
    // by the same panel identity as the writes.
    let resolveSlot: (v: unknown) => void = () => {}
    mockApi.createChatSlot.mockImplementation(() => new Promise(res => { resolveSlot = res }))
    await renderRoster()

    const sheet = await openEditor('oncall')
    fireEvent.click(within(sheet).getByRole('button', { name: 'Chat with this crew' }))
    pressEscape()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit crew oncall' })).not.toBeInTheDocument(),
    )

    const replacement = await openEditor('kirocrew')
    resolveSlot({ key: 'slot-1', title: 'oncall' })

    // The replacement panel survives; the user is not thrown into /chat.
    await waitFor(() => expect(mockApi.createChatSlot).toHaveBeenCalled())
    expect(replacement).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Edit crew kirocrew' })).toBeInTheDocument()
  })
})

describe('crew roster — default crew bar', () => {
  it('names the current default and switches it on pick, with no Save step', async () => {
    await renderRoster()

    const picker = screen.getByRole('combobox', { name: 'New sessions use' })
    expect(picker).toHaveTextContent('kirocrew')

    fireEvent.click(picker)
    fireEvent.click(await screen.findByRole('option', { name: 'oncall' }))

    // The write is immediate — this control is not part of any form.
    await waitFor(() => expect(mockApi.setDefaultAgent).toHaveBeenCalledWith('oncall'))
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('is hidden when there is nothing to choose between', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({ agents: [DEFAULT_CREW], default_agent: 'kirocrew' })
    await renderRoster(1)

    expect(screen.getByTestId('crew-card')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'New sessions use' })).not.toBeInTheDocument()
  })
})

/* The collision warning's test lives in CrewCollision.test.tsx, not here.
   It is the only test on this page that drives a Radix Select to completion from
   INSIDE the Radix Dialog, and that combination cannot run in this harness:
   Radix commits discrete events via `ReactDOM.flushSync(...)`, Testing Library
   wraps interactions in `act()`, and React throws "Should not already be
   working." on a flushSync nested inside a flush. That file mocks SimpleSelect
   to keep the assertion; the REAL Radix path is verified end-to-end in
   scripts/verify-crews-dialog-select.mjs. */

describe('crew editor — chat with this crew', () => {
  it('keeps the panel open and surfaces the error when the session cannot be created', async () => {
    // `dispatch(thunk)` RESOLVES with a rejected action; only `unwrap()` throws.
    // Without it a failed create still closed the panel and navigated to /chat,
    // silently showing whatever session happened to be active.
    mockApi.createChatSlot.mockRejectedValue(new Error('gateway is offline'))
    await renderRoster()
    const sheet = await openEditor('oncall')

    fireEvent.click(within(sheet).getByRole('button', { name: 'Chat with this crew' }))

    await waitFor(() => expect(within(sheet).getByText('gateway is offline')).toBeInTheDocument())
    expect(screen.getByRole('dialog', { name: 'Edit crew oncall' })).toBeInTheDocument()
  })
})

describe('crew editor — delete', () => {
  it('deletes a non-default crew only after a confirm step', async () => {
    await renderRoster()
    const sheet = await openEditor('oncall')

    // First press arms the confirm; it must NOT delete. A one-click destructive
    // button in a slide-in panel was the flagged regret risk.
    fireEvent.click(within(sheet).getByRole('button', { name: 'Delete crew' }))
    expect(mockApi.deleteKirocrewAgent).not.toHaveBeenCalled()
    expect(within(sheet).getByText(/Delete crew oncall\?/)).toBeInTheDocument()

    fireEvent.click(within(sheet).getByTestId('confirm-delete-crew'))
    await waitFor(() => expect(mockApi.deleteKirocrewAgent).toHaveBeenCalledWith('oncall'))
  })

  it('abandons the delete when the confirm step is cancelled', async () => {
    await renderRoster()
    const sheet = await openEditor('oncall')

    fireEvent.click(within(sheet).getByRole('button', { name: 'Delete crew' }))
    fireEvent.click(within(sheet).getByTestId('cancel-delete-crew'))
    expect(within(sheet).queryByTestId('confirm-delete-crew')).not.toBeInTheDocument()
    expect(mockApi.deleteKirocrewAgent).not.toHaveBeenCalled()
  })

  it('hides the danger zone on the default crew', async () => {
    await renderRoster()
    const sheet = await openEditor('kirocrew')

    // The backend refuses to delete the default crew, so the affordance is not
    // offered rather than offered-then-rejected.
    expect(within(sheet).queryByRole('button', { name: 'Delete crew' })).not.toBeInTheDocument()
    expect(within(sheet).queryByText('Danger zone')).not.toBeInTheDocument()
  })
})

describe('crew editor — keyboard', () => {
  it('closes on Escape', async () => {
    await renderRoster()
    await openEditor('oncall')

    pressEscape()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit crew oncall' })).not.toBeInTheDocument(),
    )
  })

  /* The nested-dialog Escape test lives in CrewEditorSelect.test.tsx — reaching
     the nested dialog means driving a Radix Select from inside the Radix Dialog,
     which this harness cannot do (see that file's header). */
})

describe('CrewAvatar', () => {
  function renderAvatar(seed: string) {
    const { container, unmount } = render(<CrewAvatar seed={seed} size={38} />)
    const img = container.querySelector('img')!
    const src = img.getAttribute('src')!
    return { img, src, unmount }
  }

  it('renders a decorative img backed by a local data URI', async () => {
    const { img, src } = renderAvatar('kirocrew')
    expect(img).toBeTruthy()
    // Generated in-process — never an http(s) URL, so no crew name leaves the
    // machine and the roster works offline.
    expect(src.startsWith('data:image/svg+xml')).toBe(true)
    expect(img).toHaveAttribute('aria-hidden', 'true')
    expect(img).toHaveAttribute('alt', '')
  })

  it('is deterministic per seed and distinct across seeds', async () => {
    const first = renderAvatar('oncall')
    first.unmount()
    const second = renderAvatar('oncall')
    expect(second.src).toBe(first.src)

    const other = renderAvatar('kirocrew')
    expect(other.src).not.toBe(first.src)
  })
})
