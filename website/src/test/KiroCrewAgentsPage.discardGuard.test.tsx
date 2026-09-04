/**
 * The crew editor's dismissal guard covers EVERY dirty pane, not just a typed
 * schedule.
 *
 * #5539 added the confirm on the editor's close paths (footer Cancel, Escape,
 * overlay click) but keyed it on the inline schedule draft alone, so the seven
 * other fields `dirtyPanes` tracks were still destroyed silently (#8284). The
 * fix keys `requestClose` on `dirtyPanes.size > 0` and reuses the SAME nested
 * confirm the schedule draft already had, widening its copy instead of adding a
 * second dialog.
 *
 * These tests pin the generalized behavior against the real page: a tracked
 * field edited on any pane raises the confirm before the sheet closes, keeps the
 * edit when the user backs out, and drops it only on an explicit Discard. Three
 * paths deliberately do NOT prompt — a clean sheet, a successful save, and a
 * dismissal with the committing PUT already away (a confirm cannot un-send it).
 * The staging half of that save is the opposite case and still prompts, because
 * nothing is committed there and a silent close would drop the whole save.
 *
 * If `requestClose` were reverted to `if (schedDraft) ... else closeSheet()`,
 * the dirty-Cancel and dirty-Escape cases fail: the sheet vanishes with no
 * confirm at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

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
  uploadCrewAvatar: vi.fn(),
  models: vi.fn(),
  crons: vi.fn(),
  webhooks: vi.fn(),
  createCron: vi.fn(),
  updateCron: vi.fn(),
  toggleCron: vi.fn(),
  runCron: vi.fn(),
  cancelCron: vi.fn(),
  cronToChat: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mockApi }))

/**
 * The real avatar builder decodes a picked file through a canvas, which jsdom
 * has no pixel pipeline for. Its whole contract with this page is one callback
 * — `onSave` hands back the draft override — so a stub firing exactly that
 * isolates the page's guard from the builder's cropping. The builder is covered
 * on its own by CrewAvatarBuilder's spec.
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

import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

/** Open the seeded crew's edit sheet. */
async function openEditor(): Promise<HTMLElement> {
  renderWithProviders(<KiroCrewAgentsPage />)
  fireEvent.click(await screen.findByTestId('crew-card'))
  return screen.findByRole('dialog', { name: 'Edit agent oncall' })
}

/** Edit Triggers so `dirtyPanes` is non-empty. Triggers is one of the seven
 *  fields #5539 left unguarded, and it lives on a pane the dismissal does not
 *  have to be on — the guard is about the crew, not the visible pane. */
function makeDirty(sheet: HTMLElement, value = 'incidents, prod outages') {
  fireEvent.click(within(sheet).getByTestId('crew-rail-routing'))
  fireEvent.change(within(sheet).getByRole('textbox', { name: 'Triggers' }), { target: { value } })
}

/** Put a TYPED schedule draft on screen. The draft guard keys on typed work,
 *  not on the form merely being open. */
async function openSchedDraft(sheet: HTMLElement) {
  fireEvent.click(within(sheet).getByTestId('crew-rail-schedules'))
  fireEvent.click(await screen.findByTestId('crew-wake-add'))
  await screen.findByTestId('crew-wake-create')
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
}

/** Pick a picture, which stages an upload on the next Save. */
async function pickPicture(sheet: HTMLElement) {
  fireEvent.click(within(sheet).getByTestId('open-avatar-builder'))
  fireEvent.click(await screen.findByRole('button', { name: 'stub-pick-picture' }))
}

/** The confirm is a dialog named by its own title, which disambiguates its
 *  buttons from the editor footer's. */
const GENERIC = 'Discard unsaved changes?'
const NARROW = 'Discard the new schedule?'
const confirmBox = (name = GENERIC) => screen.getByRole('dialog', { name })

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.kirocrewAgents.mockResolvedValue({
    agents: [{
      name: 'oncall',
      kiro_agent: 'kirocrew',
      workspace: 'default',
      memory_store: 'default',
      triggers: 'incidents',
      session_color: '',
    }],
    default_agent: 'kirocrew',
  })
  mockApi.agentsInstalled.mockResolvedValue([{ name: 'kirocrew' }])
  mockApi.workspaces.mockResolvedValue({ workspaces: [{ name: 'default', dir: 'workspace' }] })
  mockApi.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {} } })
  mockApi.agentResolvedModel.mockResolvedValue({ model: '' })
  mockApi.models.mockResolvedValue([])
  mockApi.crons.mockResolvedValue({ jobs: [] })
  mockApi.webhooks.mockResolvedValue({ tokens: [] })
  mockApi.createKirocrewAgent.mockResolvedValue({ ok: true })
  mockApi.updateKirocrewAgent.mockResolvedValue({})
  mockApi.deleteKirocrewAgent.mockResolvedValue({})
  mockApi.setDefaultAgent.mockResolvedValue({})
  mockApi.createWorkspace.mockResolvedValue({})
})

describe('crew editor — dirty dismissal is guarded on every pane', () => {
  it('footer Cancel while dirty prompts, and backing out keeps the sheet and the edit', async () => {
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Cancel' }))

    // The confirm is raised rather than the sheet silently closing.
    await waitFor(() => expect(confirmBox()).toBeInTheDocument())
    expect(within(confirmBox()).getByText(
      'This crew has edits that were never saved. Closing now throws them away.',
    )).toBeInTheDocument()

    // Back out: the sheet stays and the edited value survives.
    fireEvent.click(within(confirmBox()).getByTestId('crew-sched-discard-keep'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: GENERIC })).not.toBeInTheDocument())
    expect(screen.getByRole('dialog', { name: 'Edit agent oncall' })).toBeInTheDocument()
    expect(within(sheet).getByRole('textbox', { name: 'Triggers' })).toHaveValue('incidents, prod outages')
  })

  it('confirming Discard closes the sheet without saving', async () => {
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(confirmBox()).toBeInTheDocument())
    expect(within(confirmBox()).getByTestId('crew-sched-discard-confirm')).toHaveTextContent('Discard changes')

    fireEvent.click(within(confirmBox()).getByTestId('crew-sched-discard-confirm'))

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent oncall' })).not.toBeInTheDocument())
    // A dismissal, never a save.
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('Escape while dirty also prompts instead of dropping the edit', async () => {
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    await waitFor(() => expect(confirmBox()).toBeInTheDocument())
    // The editor is still mounted behind the confirm. Queried by test id, not
    // by role: Radix drops the outer dialog from the ACCESSIBILITY tree while a
    // nested one is open, which is the correct layering — the confirm is the
    // active layer — and is exactly the property a body-portal confirm outside
    // this DialogContent would have to fake by releasing the focus scope.
    expect(sheet).toBeInTheDocument()
    expect(screen.getByTestId('crew-rail-routing')).toBeInTheDocument()
  })

  it('a clean sheet closes immediately with no discard prompt', async () => {
    const sheet = await openEditor()
    // No edits: dirtyPanes is empty, so there is nothing to ask about.
    fireEvent.click(within(sheet).getByRole('button', { name: 'Cancel' }))

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent oncall' })).not.toBeInTheDocument())
    expect(screen.queryByRole('dialog', { name: GENERIC })).not.toBeInTheDocument()
  })

  it('a rail pane switch while dirty stays unprompted', async () => {
    // The guard is scoped to DISMISSAL on purpose. Every field except the
    // schedule draft lives in the page's own state and survives a pane switch,
    // so prompting there would be a warning about a loss that cannot happen —
    // and those are what train people to click through the ones that matter.
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByTestId('crew-rail-overview'))

    expect(screen.queryByRole('dialog', { name: GENERIC })).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: NARROW })).not.toBeInTheDocument()
    // The switch landed, and coming back shows the edit intact.
    fireEvent.click(within(sheet).getByTestId('crew-rail-routing'))
    expect(within(sheet).getByRole('textbox', { name: 'Triggers' })).toHaveValue('incidents, prod outages')
  })

  it('a dismissal while the committing PUT is away closes without promising a discard', async () => {
    // Once the PUT is gone the confirm cannot honor "Discard changes": the
    // request is not cancellable, so the backend keeps the edits whatever the
    // user answers, and the values on screen are the ones they just submitted
    // rather than unsaved work. Offering the confirm there is a promise the app
    // breaks. Without the skip `dirtyPanes` is still non-empty (the roster
    // refetch only happens on success), so the confirm WOULD be raised.
    mockApi.updateKirocrewAgent.mockImplementation(() => new Promise(() => {}))
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent oncall' })).not.toBeInTheDocument())
    expect(screen.queryByRole('dialog', { name: GENERIC })).not.toBeInTheDocument()
  })

  it('a dismissal while the avatar upload is only STAGING still prompts', async () => {
    // The skip above must read the COMMITTING write, not `sheetBusy`. saveEdit's
    // picture leg holds sheetBusy while it stages the upload, and its own epoch
    // check abandons the PUT if the sheet closes underneath it — so nothing is
    // committed yet and every edit is still genuinely discardable. Skipping the
    // confirm there closes the editor and drops the WHOLE save silently, which
    // is worse than the false promise the skip exists to remove.
    mockApi.uploadCrewAvatar.mockImplementation(() => new Promise(() => {}))
    const sheet = await openEditor()
    makeDirty(sheet)
    await pickPicture(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    // Staging has started; the committing PUT has not been fired.
    await waitFor(() => expect(mockApi.uploadCrewAvatar).toHaveBeenCalled())
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    await waitFor(() => expect(confirmBox()).toBeInTheDocument())
    expect(sheet).toBeInTheDocument()

    // Discarding here is honest: the staged picture is never promoted and the
    // abandoned save never reaches the crew record.
    fireEvent.click(within(confirmBox()).getByTestId('crew-sched-discard-confirm'))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent oncall' })).not.toBeInTheDocument())
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('Discard answered while a staged upload lands still abandons the save', async () => {
    // The confirm and the staging save race. The upload can finish WHILE the
    // question is on screen, and saveEdit's epoch check cannot see an answer
    // that has not arrived — so without the discardAnswer interlock the PUT
    // fires mid-question and Discard closes the editor over edits the server has
    // already been told to keep.
    let releaseUpload: (v: { ok: boolean; token: string }) => void = () => {}
    mockApi.uploadCrewAvatar.mockImplementation(() => new Promise(res => { releaseUpload = res }))
    const sheet = await openEditor()
    makeDirty(sheet)
    await pickPicture(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.uploadCrewAvatar).toHaveBeenCalled())
    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(confirmBox()).toBeInTheDocument())

    // The staging completes while the user is still deciding.
    await act(async () => { releaseUpload({ ok: true, token: 'tok_1' }) })
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()

    fireEvent.click(within(confirmBox()).getByTestId('crew-sched-discard-confirm'))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent oncall' })).not.toBeInTheDocument())
    await act(async () => {})
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('backing out of that confirm lets the staged save finish', async () => {
    // The other half of the interlock: holding the PUT must not KILL it. The
    // user pressed Save and then chose to keep editing, so the save they asked
    // for completes and promotes the picture it staged.
    let releaseUpload: (v: { ok: boolean; token: string }) => void = () => {}
    mockApi.uploadCrewAvatar.mockImplementation(() => new Promise(res => { releaseUpload = res }))
    const sheet = await openEditor()
    makeDirty(sheet)
    await pickPicture(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(mockApi.uploadCrewAvatar).toHaveBeenCalled())
    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(confirmBox()).toBeInTheDocument())

    await act(async () => { releaseUpload({ ok: true, token: 'tok_1' }) })
    fireEvent.click(within(confirmBox()).getByTestId('crew-sched-discard-keep'))

    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    expect(mockApi.updateKirocrewAgent.mock.calls[0][1]).toMatchObject({
      avatar: { kind: 'image', promote: true, token: 'tok_1' },
    })
  })

  it('the schedule confirm names the crew edits it also destroys', async () => {
    // An open schedule draft keeps its OWN confirm — only that one can lock
    // Discard while the draft's create POST is in flight — and that confirm
    // closes the WHOLE sheet. Asking only about "the schedule you typed" while
    // Discard also throws away a Model or Triggers edit is the same silent loss
    // this guard exists to end, except behind a dialog, which is worse than no
    // dialog at all.
    const sheet = await openEditor()
    makeDirty(sheet)
    await openSchedDraft(sheet)

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    const ask = await screen.findByRole('dialog', { name: GENERIC })
    expect(within(ask).getByText(
      'The schedule you typed has not been saved and will be lost.',
    )).toBeInTheDocument()
    expect(within(ask).getByTestId('crew-sched-discard-also-crew')).toBeInTheDocument()
    expect(within(ask).getByTestId('crew-sched-discard-confirm')).toHaveTextContent('Discard changes')
  })

  it('a schedule draft alone keeps the narrow schedule question', async () => {
    // The widening must be conditional: over a pristine crew the only thing at
    // stake IS the typed schedule, and a dialog claiming otherwise is the false
    // warning that trains people to click through the ones that matter.
    const sheet = await openEditor()
    await openSchedDraft(sheet)

    fireEvent.keyDown(sheet, { key: 'Escape', code: 'Escape' })

    const ask = await screen.findByRole('dialog', { name: NARROW })
    expect(within(ask).queryByTestId('crew-sched-discard-also-crew')).not.toBeInTheDocument()
    expect(within(ask).getByTestId('crew-sched-discard-confirm')).toHaveTextContent('Discard schedule')
  })

  it('a successful save closes without a discard prompt', async () => {
    const sheet = await openEditor()
    makeDirty(sheet)

    fireEvent.click(within(sheet).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalled())
    // The save path routes through settleFor/closeSheet, never the guard.
    expect(screen.queryByRole('dialog', { name: GENERIC })).not.toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit agent oncall' })).not.toBeInTheDocument())
  })
})
