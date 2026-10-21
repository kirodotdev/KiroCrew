/* The crewmate editor's schedule-draft guard.
 *
 * An open inline schedule-create form is component-local state that unmounts
 * with its section, so a typed draft must survive every path that would
 * unmount it. The editor's footer already refuses Save while the draft is open
 * (it would close the editor); these tests pin the rest: the section rail is
 * locked while the draft is typed, and dismissal and the form's own cancel
 * toggle both confirm before discarding.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

vi.mock('../api/client', () => ({
  api: {
    kirocrewAgents: vi.fn().mockResolvedValue({
      agents: [{ name: 'oncall', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' }],
      default_agent: 'kirocrew',
    }),
    agentsInstalled: vi.fn().mockResolvedValue([{ name: 'kirocrew' }]),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [{ name: 'default', dir: 'workspace' }] }),
    kirocrewConfig: vi.fn().mockResolvedValue({ memory_stores: { default: {} } }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: '' }),
    createKirocrewAgent: vi.fn().mockResolvedValue({ ok: true }),
    updateKirocrewAgent: vi.fn().mockResolvedValue({}),
    deleteKirocrewAgent: vi.fn().mockResolvedValue({}),
    setDefaultAgent: vi.fn().mockResolvedValue({}),
    createWorkspace: vi.fn().mockResolvedValue({}),
    crons: vi.fn().mockResolvedValue({ jobs: [] }),
    webhooks: vi.fn().mockResolvedValue({ tokens: [] }),
    models: vi.fn().mockResolvedValue([]),
    createCron: vi.fn().mockResolvedValue({}),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cancelCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
  },
}))

/** Opening the editor is a lazy `CrewEditorModal` chunk import followed by a
 *  fresh-roster read (`staleTime: 0`) before the dialog renders, which can
 *  outlast RTL's 1s default on a loaded runner. An explicit deadline. */
const EDITOR_OPEN_TIMEOUT_MS = 5000

/** Open the editor for the seeded crew and put a TYPED schedule draft on
 *  screen. The guard keys on typed work, not on the form being open, so the
 *  helper types into Name — a pristine form must never arm the guard. */
async function openDraft() {
  renderWithProviders(<KiroCrewAgentsPage />)
  fireEvent.click(await screen.findByTestId('crew-card'))
  fireEvent.click(await screen.findByTestId('next-rail-schedules', {}, { timeout: EDITOR_OPEN_TIMEOUT_MS }))
  fireEvent.click(await screen.findByTestId('crew-wake-add'))
  await screen.findByTestId('crew-wake-create')
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
}

/** The rail is locked while a typed draft is open: the Schedules section stays
 *  live, every other section is disabled, so a click cannot unmount the form. */
const railLocked = () => (screen.getByTestId('next-rail-general') as HTMLButtonElement).disabled

beforeEach(() => vi.clearAllMocks())

describe('crew editor — schedule-draft discard guard', () => {
  it('locks the section rail while a typed draft is open, keeping the draft alive', async () => {
    await openDraft()
    expect(railLocked()).toBe(true)
    expect(screen.getByTestId('next-rail-schedules')).toBeEnabled()
    fireEvent.click(screen.getByTestId('next-rail-general'))
    // Nothing unmounted: same section, same form, same typed value.
    expect(screen.getByTestId('crew-wake-create')).toBeTruthy()
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('draft')
  })

  it('editor dismissal asks first; Keep editing returns to the intact draft', async () => {
    await openDraft()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'half-typed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByTestId('crew-sched-discard-keep'))
    await waitFor(() => expect(screen.queryByText('Discard the new schedule?')).toBeNull())
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('half-typed')
  })

  it('editor dismissal is guarded by the same confirm, and discard closes the editor', async () => {
    await openDraft()
    // The footer's own Cancel — the widest destruction path the draft has.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByTestId('crew-sched-discard-confirm'))
    await waitFor(() => expect(screen.queryByTestId('crew-wake-create')).toBeNull())
    expect(screen.queryByTestId('next-rail-schedules')).toBeNull()
  })

  it('without a draft, section switches and dismissal stay unprompted', async () => {
    renderWithProviders(<KiroCrewAgentsPage />)
    fireEvent.click(await screen.findByTestId('crew-card'))
    fireEvent.click(await screen.findByTestId('next-rail-schedules', {}, { timeout: EDITOR_OPEN_TIMEOUT_MS }))
    fireEvent.click(screen.getByTestId('next-rail-general'))
    expect(screen.queryByText('Discard the new schedule?')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('next-rail-general')).toBeNull())
  })

  it('a PRISTINE open form never arms the guard — looking is not work', async () => {
    // "The schedule you typed will be lost" over nothing typed is a false
    // warning that trains users to click through the confirm guarding real
    // drafts. Open the form, type nothing, leave: no lock, no prompt.
    renderWithProviders(<KiroCrewAgentsPage />)
    fireEvent.click(await screen.findByTestId('crew-card'))
    fireEvent.click(await screen.findByTestId('next-rail-schedules', {}, { timeout: EDITOR_OPEN_TIMEOUT_MS }))
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    await screen.findByTestId('crew-wake-create')
    expect(railLocked()).toBe(false)
    fireEvent.click(screen.getByTestId('next-rail-general'))
    expect(screen.queryByText('Discard the new schedule?')).toBeNull()
    // The switch went through: the schedules section (and its form) unmounted.
    expect(screen.queryByTestId('crew-wake-create')).toBeNull()
  })

  it('typing and erasing returns the form to not-a-draft', async () => {
    await openDraft()
    expect(railLocked()).toBe(true)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: '' } })
    await waitFor(() => expect(railLocked()).toBe(false))
  })

  it("the section's own cancel toggle asks before collapsing a dirty draft", async () => {
    // The toggle is a bare icon-only X at narrow widths: without the guard,
    // one misclick erases everything typed while every sibling path asks.
    await openDraft()
    fireEvent.click(screen.getByTestId('crew-wake-add'))
    expect(await screen.findByText('Discard the new schedule?')).toBeTruthy()
    // Keep editing: the intact draft is still there.
    fireEvent.click(screen.getByTestId('crew-sched-discard-keep'))
    await waitFor(() => expect(screen.queryByText('Discard the new schedule?')).toBeNull())
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('draft')
    // Discard: the collapse the toggle asked for goes through.
    fireEvent.click(screen.getByTestId('crew-wake-add'))
    fireEvent.click(await screen.findByTestId('crew-sched-discard-confirm'))
    await waitFor(() => expect(screen.queryByTestId('crew-wake-create')).toBeNull())
    expect(railLocked()).toBe(false)
  })

  it('the toggle collapses a PRISTINE form directly, no confirm', async () => {
    renderWithProviders(<KiroCrewAgentsPage />)
    fireEvent.click(await screen.findByTestId('crew-card'))
    fireEvent.click(await screen.findByTestId('next-rail-schedules', {}, { timeout: EDITOR_OPEN_TIMEOUT_MS }))
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    await screen.findByTestId('crew-wake-create')
    fireEvent.click(screen.getByTestId('crew-wake-add'))
    expect(screen.queryByText('Discard the new schedule?')).toBeNull()
    expect(screen.queryByTestId('crew-wake-create')).toBeNull()
  })

  it('the footer note points at the section’s own Save while the draft is open', async () => {
    await openDraft()
    // Visible text, not a hover-only `title`: keyboard and touch users see a
    // disabled Save and must be told why in the note itself.
    expect(screen.getByTestId('next-unsaved-note').textContent)
      .toBe('Not saved — use Save in this section')
  })

  it('Discard is locked while the create request is in flight', async () => {
    // Discarding mid-flight unmounts the form without cancelling the POST --
    // the schedule the user watched being "discarded" would persist. The
    // confirm still opens (the intent is legitimate) but its destructive
    // button waits for the request to settle.
    const { api } = await import('../api/client')
    let resolveCreate!: (v: unknown) => void
    ;(api.createCron as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(res => { resolveCreate = res }))
    await openDraft()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'm' } })
    fireEvent.click(screen.getByTestId('crew-wake-create-submit'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    const discard = await screen.findByTestId('crew-sched-discard-confirm')
    await waitFor(() => expect(discard).toBeDisabled())
    // The reason is VISIBLE text in the dialog, not a hover title keyboard
    // and touch users never see.
    expect(screen.getByTestId('next-sched-discard-saving-note')).toBeTruthy()
    fireEvent.click(discard)
    // Still on the schedules section, form still mounted, nothing destroyed.
    expect(screen.getByTestId('crew-wake-create')).toBeTruthy()
    resolveCreate({})
    // The save lands: the form collapses on its own and Discard unlocks.
    await waitFor(() => expect(screen.queryByTestId('crew-wake-create')).toBeNull())
    expect(screen.getByTestId('crew-sched-discard-confirm')).not.toBeDisabled()
  })

  it('a HUNG create unlocks Discard after the grace period, with the may-persist note', async () => {
    // The create POST has no client timeout. Without this escape, a stalled
    // request seals EVERY exit from the modal editor for as long as it
    // stalls: the toggle is disabled, footer Cancel routes to a confirm whose
    // Discard is locked, and Escape is gated the same way.
    // shouldAdvanceTime keeps RTL waitFor/findBy live under fake timers
    // (repo convention, see AboutPanel.inAppApply.test.tsx).
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const { api } = await import('../api/client')
      ;(api.createCron as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => { /* never settles */ }))
      await openDraft()
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n' } })
      fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'm' } })
      fireEvent.click(screen.getByTestId('crew-wake-create-submit'))
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      const discard = await screen.findByTestId('crew-sched-discard-confirm')
      expect(discard).toBeDisabled()
      // Before the grace elapses, the note names the lock, not the caveat.
      expect(screen.getByTestId('next-sched-discard-saving-note').textContent)
        .not.toMatch(/may still be created/i)
      await vi.advanceTimersByTimeAsync(8000)
      // The escape: Discard unlocks and the visible note switches to the
      // honest caveat — the request is not cancelled, so the schedule may
      // still be created.
      expect(discard).not.toBeDisabled()
      expect(screen.getByTestId('next-sched-discard-saving-note').textContent)
        .toMatch(/may still be created/i)
      fireEvent.click(discard)
      // The forced discard completes the close: the draft is gone.
      await waitFor(() => expect(screen.queryByTestId('crew-wake-create')).toBeNull())
    } finally {
      vi.useRealTimers()
    }
  })
})
