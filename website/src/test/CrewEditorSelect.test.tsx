/**
 * Crew editor interactions that go THROUGH a binding select.
 *
 * Split out of CrewRoster.test.tsx because of one hard harness limit: the editor
 * is a Radix Dialog and the binding pickers are Radix Selects, and Radix commits
 * its discrete events with `ReactDOM.flushSync(() => target.dispatchEvent(e))`
 * (@radix-ui/react-primitive). Testing Library runs every interaction inside
 * `act()`, which is already a flush, and React throws "Should not already be
 * working." on a flushSync nested inside one. It is not a product defect — the
 * same two interactions are driven for real, in a real browser, by
 * `scripts/verify-crews-dialog-select.mjs`, which asserts the value commits, the
 * warning names the colliding crew, Escape closes only the nested layer, and the
 * console stays clean.
 *
 * So SimpleSelect is stubbed HERE (and only here) with a plain listbox that
 * keeps the same accessible surface: a `combobox` labelled the same way showing
 * the current value, an `option` per choice, and the extra action item. What is
 * under test is this page's own logic — that the collision warning reads the
 * IN-FLIGHT picker value rather than the persisted one, and that the nested
 * dialog owns Escape alone — not Radix's select machinery.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
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

/* Plain-DOM stand-in for SimpleSelect. Options are always rendered rather than
   gated behind opening the trigger: the trigger click is kept in the tests below
   so they still read as a user flow, but nothing depends on a portal. */
vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options, value, onChange, action, optionLabels, clearLabel, 'aria-label': ariaLabel,
  }: {
    options: string[]
    value: string
    onChange: (v: string) => void
    action?: { label: string; onSelect: () => void }
    optionLabels?: string[]
    clearLabel?: string
    'aria-label'?: string
  }) => (
    <div>
      <button type="button" role="combobox" aria-label={ariaLabel} aria-expanded={false}>
        {optionLabels?.[options.indexOf(value)] ?? value ?? clearLabel}
      </button>
      {clearLabel && (
        <button type="button" role="option" aria-selected={value === ''} onClick={() => onChange('')}>
          {clearLabel}
        </button>
      )}
      {options.map((o, i) => (
        <button
          key={o}
          type="button"
          role="option"
          aria-selected={o === value}
          onClick={() => onChange(o)}
        >
          {optionLabels?.[i] ?? o}
        </button>
      ))}
      {action && (
        <button type="button" onClick={action.onSelect}>{action.label}</button>
      )}
    </div>
  ),
}))


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

describe('crew editor — collision warning', () => {
  it('warns as soon as the picker points at a store another crew uses', async () => {
    // Reading the PERSISTED binding here meant the warning only appeared after
    // a save and a reopen — by which point the collision it exists to prevent
    // has already happened.
    await renderRoster()
    const sheet = await openEditor('oncall')

    // oncall starts on its own store, so nothing collides yet.
    expect(within(sheet).queryByText(/Also used by/)).not.toBeInTheDocument()

    // `userEvent`, not `fireEvent`, for a Radix Select inside a Radix Dialog.
    // Radix dispatches its discrete events through
    // `ReactDOM.flushSync(() => target.dispatchEvent(event))`
    // (@radix-ui/react-primitive). `fireEvent` delivers those synchronously
    // inside React's current batch, so the flushSync lands DURING a render and
    // React throws "Should not already be working." `userEvent` awaits between
    // steps, which is also what a real browser does — the same interaction is
    // proven end-to-end in scripts/verify-crews-dialog-select.mjs.
    const user = userEvent.setup()
    await user.click(within(sheet).getByRole('combobox', { name: 'Memory Store' }))
    await user.click(await screen.findByRole('option', { name: 'core-mem' }))

    // kirocrew is already on core-mem, so the warning must name it immediately.
    await waitFor(() =>
      expect(within(sheet).getByText(/Also used by kirocrew/)).toBeInTheDocument(),
    )
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })
})

describe('crew editor — keyboard (via a binding select)', () => {
  it('gives the nested workspace dialog sole ownership of Escape', async () => {
    await renderRoster()
    const sheet = await openCreate()

    fireEvent.click(within(sheet).getByRole('combobox', { name: 'Workspace' }))
    fireEvent.click(await screen.findByText('+ New workspace…'))
    const modal = await screen.findByRole('dialog', { name: 'Create Workspace' })

    // Focus moves INTO the nested layer — Radix's FocusScope owns this, where the
    // hand-rolled version had to be told to stop trapping. Focus on `body` would
    // mean Tab walks the obscured page behind both overlays.
    const active = document.activeElement as HTMLElement
    expect(active).not.toBe(document.body)
    expect(modal.contains(active)).toBe(true)

    // While the nested layer is up, the editor beneath is `aria-hidden` and so is
    // deliberately NOT exposed as a dialog to assistive tech. That is Radix doing
    // the right thing, and it is why this is asserted through the DOM rather than
    // by role: the editor must still be MOUNTED (the form is not destroyed) even
    // though it is hidden from AT.
    const editorEl = document.querySelector('[aria-label="Create a new crew"]')
    expect(editorEl).toBeTruthy()
    expect(editorEl!.closest('[aria-hidden="true"]')).toBeTruthy()

    pressEscape()

    // The inner dialog takes the key; the editor must NOT close underneath it, or
    // the user loses the whole form to one keypress. Radix's layer stack does this
    // natively — the old implementation needed a `paused` flag threaded down.
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Create Workspace' })).not.toBeInTheDocument(),
    )
    // ...and with the nested layer gone the editor is exposed to AT again.
    expect(screen.getByRole('dialog', { name: 'Create a new crew' })).toBeInTheDocument()

    // Once the nested dialog is gone the editor owns Escape again.
    pressEscape()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Create a new crew' })).not.toBeInTheDocument(),
    )
  })
})
