/**
 * The crew editor guards ALL dirty dismissals, not just the schedule draft.
 *
 * #5539 added a discard confirm to the editor's close paths (footer Cancel,
 * Escape, overlay click) but keyed it on the inline schedule draft alone, so
 * the other seven fields `dirtyPanes` tracks were still destroyed silently.
 * The follow-up keys the confirm on `dirtyPanes.size > 0`. These tests pin the
 * generalized behavior against the REAL page: a tracked field edited on any
 * pane must raise the discard confirm before the sheet closes, keep the edit
 * when the user backs out, and drop it only on explicit Discard. A clean sheet,
 * a successful save, and a dismissal with the committing PUT already in flight
 * all close with no prompt at all — the last because a confirm cannot un-send a
 * PUT. The staging half of that save is the opposite case and still prompts:
 * nothing is committed there, so a discard is honest and a silent close would
 * drop the whole save.
 *
 * If the guard were reverted to a bare `closeSheet`, the dirty-Cancel and
 * dirty-Escape cases would fail: the sheet would vanish with no confirm.
 *
 * framer-motion is stubbed so the confirm Modal (wrapped in AnimatePresence)
 * renders synchronously, matching the harness in the sibling page specs.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

/**
 * Records the `modal` prop the editor's Radix `Dialog` is rendered with on every
 * render, so a test can pin that the trap is RELEASED while the discard confirm
 * is up. jsdom does not run Radix's real FocusScope, so the runtime harm (focus
 * cycling back to Save behind a body-portal confirm, Enter persisting the very
 * edits the confirm asks to discard) cannot be reproduced by driving the DOM --
 * the prop wiring that prevents it is the honest thing to assert.
 */
const editorModalRenders: Array<unknown> = []
vi.mock('@radix-ui/react-dialog', async () => {
  const actual = await vi.importActual<typeof import('@radix-ui/react-dialog')>('@radix-ui/react-dialog')
  const React = await import('react')
  const Root = (props: Record<string, unknown>) => {
    // Only the editor Dialog is a controlled Root driven by open+onOpenChange;
    // the nested WorkspaceModal Root is too but stays modal-default and never
    // renders false, so asserting on the false value isolates the editor.
    if ('onOpenChange' in props) editorModalRenders.push(props.modal)
    const RealRoot = (actual as unknown as { Root: React.ComponentType<Record<string, unknown>> }).Root
    return React.createElement(RealRoot, props)
  }
  return { ...actual, Root }
})

/**
 * The real avatar builder decodes a picked file through a canvas, which jsdom has
 * no pixel pipeline for. Its whole contract with this page is one callback —
 * `onSave` hands back the draft override — so a stub firing exactly that isolates
 * the page's guard from the builder's cropping. Covered on its own by
 * CrewAvatarBuilder's spec.
 */
vi.mock('../components/CrewAvatarBuilder', async () => {
  const React = await import('react')
  return {
    default: ({ open, onSave }: { open: boolean; onSave: (v: unknown) => void }) =>
      open
        ? React.createElement(
          'button',
          {
            type: 'button',
            onClick: () => onSave({ kind: 'image', pendingData: 'data:image/png;base64,iVBORw0KGgo=' }),
          },
          'stub-pick-picture',
        )
        : null,
  }
})

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
  uploadCrewAvatar: vi.fn(),
  models: vi.fn(),
  crons: vi.fn(),
  webhooks: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mockApi }))

import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

const REVIEWER = {
  name: 'reviewer',
  kiro_agent: 'reviewer-agent',
  workspace: 'default',
  memory_store: 'default',
  model: 'claude-opus-5',
  reasoning_effort: 'max',
  triggers: 'incidents',
  session_color: '',
}

function renderPage() {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
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

/** Render the roster with one crew and open its editor sheet. */
async function openEditor(crew: Record<string, unknown> = {}): Promise<HTMLElement> {
  mockApi.kirocrewAgents.mockResolvedValue({
    agents: [{ ...REVIEWER, ...crew }],
    default_agent: 'kirocrew',
  })
  renderPage()
  await waitFor(() => expect(screen.getAllByTestId('crew-card')).toHaveLength(1))
  fireEvent.click(screen.getByRole('button', { name: 'Edit agent reviewer' }))
  return screen.findByRole('dialog', { name: 'Edit agent reviewer' })
}

/** Move to the routing pane and edit the Triggers field so `dirtyPanes` is
 *  non-empty. Triggers is one of the seven fields #5539 left unguarded. */
function makeDirty(sheet: HTMLElement, value = 'incidents, prod outages') {
  fireEvent.click(within(sheet).getByTestId('crew-rail-routing'))
  fireEvent.change(within(sheet).getByRole('textbox', { name: 'Triggers' }), {
    target: { value },
  })
}

/** The discard confirm is a separate dialog named by its title. Scoping to it
 *  disambiguates its "Cancel" from the editor footer's identical "Cancel". */
function discardConfirm() {
  return screen.getByRole('dialog', { name: 'Discard unsaved changes?' })
}

beforeEach(() => {
  vi.clearAllMocks()
  editorModalRenders.length = 0
  mockApi.agentsInstalled.mockResolvedValue([{ name: 'reviewer-agent' }])
  mockApi.workspaces.mockResolvedValue({ workspaces: [{ name: 'default' }] })
  mockApi.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {} } })
  mockApi.agentResolvedModel.mockResolvedValue({
    model: 'claude-opus-5',
    pinned: true,
    kiro_agent: 'reviewer-agent',
    reasoning_effort: 'max',
    effort_pinned: true,
  })
  mockApi.models.mockResolvedValue([{ model_name: 'claude-opus-5' }])
  mockApi.crons.mockResolvedValue({ jobs: [] })
  mockApi.webhooks.mockResolvedValue({ tokens: [], switch_on: true })
  mockApi.createKirocrewAgent.mockResolvedValue({})
  mockApi.updateKirocrewAgent.mockResolvedValue({})
  mockApi.deleteKirocrewAgent.mockResolvedValue({})
  mockApi.setDefaultAgent.mockResolvedValue({})
})

describe('crew editor — dirty dismissal is guarded on every pane', () => {
  it('footer Cancel while dirty prompts, and backing out keeps the sheet and the edit', async () => {
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Cancel' }))

    // The confirm is raised rather than the sheet silently closing.
    await waitFor(() => expect(discardConfirm()).toBeInTheDocument())
    expect(within(discardConfirm()).getByText('Discard changes')).toBeInTheDocument()

    // Back out: the sheet stays and the edited value survives.
    fireEvent.click(within(discardConfirm()).getByRole('button', { name: 'Cancel' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('dialog', { name: 'Edit agent reviewer' })).toBeInTheDocument()
    expect(within(sheet).getByRole('textbox', { name: 'Triggers' })).toHaveValue('incidents, prod outages')
  })

  it('confirming Discard closes the sheet', async () => {
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(discardConfirm()).toBeInTheDocument())

    fireEvent.click(within(discardConfirm()).getByRole('button', { name: 'Discard changes' }))

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent reviewer' })).not.toBeInTheDocument(),
    )
    // A dismissal, never a save.
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('Escape while dirty also prompts instead of dropping the edit', async () => {
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    await waitFor(() => expect(discardConfirm()).toBeInTheDocument())
    // The editor is still open behind the confirm.
    expect(screen.getByRole('dialog', { name: 'Edit agent reviewer' })).toBeInTheDocument()
  })

  it('a clean sheet closes immediately with no discard prompt', async () => {
    const sheet = await openEditor()
    // No edits: dirtyPanes is empty.
    fireEvent.click(within(sheet).getByRole('button', { name: 'Cancel' }))

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent reviewer' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument()
  })

  it('releases the editor focus trap (modal=false) while the discard confirm is open', async () => {
    // Regression: the discard confirm is a body-portal Modal OUTSIDE the editor's
    // Radix DialogContent. If the editor keeps modal=true while it is open, Radix's
    // focus scope pulls focus back onto Save behind the confirm, and Enter then
    // persists the edits the confirm is asking to discard. modal={!confirmOpen}
    // drops the scope so the confirm's own trap governs.
    const sheet = await openEditor()
    makeDirty(sheet)

    // While only the editor is open it is a true modal (nothing rendered false yet).
    expect(editorModalRenders).toContain(true)
    expect(editorModalRenders).not.toContain(false)

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(discardConfirm()).toBeInTheDocument())

    // With the confirm up the editor Dialog must have re-rendered with modal=false.
    expect(editorModalRenders).toContain(false)
  })

  it('a dismissal while the save is in flight closes without promising a discard', async () => {
    // Regression: once the PUT is away the confirm cannot honor "Discard
    // changes" — the request is not cancellable, so the backend keeps the edits
    // whatever the user answers, and the values on screen are the ones they
    // just submitted rather than unsaved work. Offering the confirm there is a
    // promise the app breaks, so the guard is skipped while a write is in
    // flight and the dismissal closes as it did before the guard existed.
    // Without that skip `dirtyPanes` is still non-empty (the roster refetch
    // only happens on success), so the confirm WOULD be raised here.
    mockApi.updateKirocrewAgent.mockImplementation(() => new Promise(() => {}))
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent reviewer' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument()
  })

  it('a dismissal while the avatar upload is only STAGING still prompts', async () => {
    // Regression: the in-flight skip above must read the committing write, not
    // `sheetBusy`. saveEdit's picture leg holds sheetBusy while it stages the
    // upload, and its own epoch check abandons the PUT if the sheet closes
    // underneath it — so nothing is committed yet and every edit is still
    // genuinely discardable. Skipping the confirm there closes the editor and
    // drops the WHOLE save silently, which is worse than the false promise the
    // skip exists to remove.
    mockApi.uploadCrewAvatar.mockImplementation(() => new Promise(() => {}))
    const sheet = await openEditor()
    makeDirty(sheet)
    fireEvent.click(within(sheet).getByTestId('open-avatar-builder'))
    fireEvent.click(await screen.findByRole('button', { name: 'stub-pick-picture' }))

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    // Staging has started; the committing PUT has not been fired.
    await waitFor(() => expect(mockApi.uploadCrewAvatar).toHaveBeenCalled())
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    await waitFor(() => expect(discardConfirm()).toBeInTheDocument())
    expect(screen.getByRole('dialog', { name: 'Edit agent reviewer' })).toBeInTheDocument()

    // Discarding here is honest: the staged picture is never promoted and the
    // abandoned save never reaches the crew record.
    fireEvent.click(within(discardConfirm()).getByRole('button', { name: 'Discard changes' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent reviewer' })).not.toBeInTheDocument(),
    )
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('Discard answered while a staged upload lands still abandons the save', async () => {
    // Regression: the confirm and the staging save race. The upload can finish
    // WHILE the question is on screen, and saveEdit's epoch check cannot see an
    // answer that has not arrived — so without the discardAnswer interlock the
    // PUT fires mid-question and Discard closes the editor over edits the server
    // has already been told to keep.
    let releaseUpload: (v: { ok: boolean; token: string }) => void = () => {}
    mockApi.uploadCrewAvatar.mockImplementation(
      () => new Promise(res => { releaseUpload = res }),
    )
    const sheet = await openEditor()
    makeDirty(sheet)
    fireEvent.click(within(sheet).getByTestId('open-avatar-builder'))
    fireEvent.click(await screen.findByRole('button', { name: 'stub-pick-picture' }))

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.uploadCrewAvatar).toHaveBeenCalled())
    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(discardConfirm()).toBeInTheDocument())

    // The staging completes while the user is still deciding.
    await act(async () => { releaseUpload({ ok: true, token: 'tok_1' }) })
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()

    fireEvent.click(within(discardConfirm()).getByRole('button', { name: 'Discard changes' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent reviewer' })).not.toBeInTheDocument(),
    )
    await act(async () => {})
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('backing out of that confirm lets the staged save finish', async () => {
    // The other half of the interlock: holding the PUT must not KILL it. The
    // user pressed Save and then chose to keep editing, so the save they asked
    // for completes and promotes the picture it staged.
    let releaseUpload: (v: { ok: boolean; token: string }) => void = () => {}
    mockApi.uploadCrewAvatar.mockImplementation(
      () => new Promise(res => { releaseUpload = res }),
    )
    const sheet = await openEditor()
    makeDirty(sheet)
    fireEvent.click(within(sheet).getByTestId('open-avatar-builder'))
    fireEvent.click(await screen.findByRole('button', { name: 'stub-pick-picture' }))

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.uploadCrewAvatar).toHaveBeenCalled())
    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(discardConfirm()).toBeInTheDocument())

    await act(async () => { releaseUpload({ ok: true, token: 'tok_1' }) })
    fireEvent.click(within(discardConfirm()).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    expect(mockApi.updateKirocrewAgent.mock.calls[0][1]).toMatchObject({
      avatar: { kind: 'image', promote: true, token: 'tok_1' },
    })
  })

  it('the schedule confirm names the crew edits it also destroys', async () => {
    // Regression: an open schedule draft routes dismissal to the schedule's own
    // confirm, and that confirm closes the WHOLE sheet. Asking only about "the
    // schedule you typed" while Discard also throws away a Model or Triggers
    // edit is the same silent loss this guard exists to end — it just happens
    // behind a dialog, which is worse than no dialog at all.
    const sheet = await openEditor()
    makeDirty(sheet)
    fireEvent.click(within(sheet).getByTestId('crew-rail-schedules'))
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'draft' } })

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    // The schedule's own dialog, because only it can lock Discard while the
    // draft's create POST is in flight — but widened to the real scope.
    const ask = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })
    expect(within(ask).getByText('The schedule you typed has not been saved and will be lost.')).toBeInTheDocument()
    expect(within(ask).getByTestId('crew-sched-discard-also-crew')).toBeInTheDocument()
    expect(within(ask).getByTestId('crew-sched-discard-confirm')).toHaveTextContent('Discard changes')
  })

  it('a schedule draft alone keeps the narrow schedule question', async () => {
    // The widening must be conditional: over a pristine crew the only thing at
    // stake IS the typed schedule, and a dialog claiming otherwise is the false
    // warning that trains people to click through the ones that matter.
    const sheet = await openEditor()
    fireEvent.click(within(sheet).getByTestId('crew-rail-schedules'))
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'draft' } })

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    const ask = await screen.findByRole('dialog', { name: 'Discard the new schedule?' })
    expect(within(ask).queryByTestId('crew-sched-discard-also-crew')).not.toBeInTheDocument()
    expect(within(ask).getByTestId('crew-sched-discard-confirm')).toHaveTextContent('Discard schedule')
  })

  it('a successful save closes without a discard prompt', async () => {
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    // The save path routes through settleFor/closeSheet, never the guard.
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent reviewer' })).not.toBeInTheDocument(),
    )
  })
})
