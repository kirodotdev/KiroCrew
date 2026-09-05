/**
 * ChatStatusTagsPage — the app homepage that views and edits the hourly
 * reconciler's prompt.
 *
 * What is worth pinning: the load -> edit -> save round-trip re-seeds "unchanged"
 * from the server's canonical reply (so Save disables again after a write); the
 * Default badge tracks `isDefault`; the two-click Reset sends the empty-string
 * reset per the contract; and the two failure modes are distinguished — a 403
 * (app disabled) reads differently from a generic server error, and a save error
 * surfaces inline.
 *
 * The HTTP seam (./api) is mocked, so nothing dials.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'

import { ChatStatusTagsApiError } from './api'

const reconcilePrompt = vi.fn()
const setReconcilePrompt = vi.fn()
const repairCron = vi.fn()
const fetchSettings = vi.fn()
const updateSettings = vi.fn()

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    chatStatusTagsApi: {
      reconcilePrompt: (...a: unknown[]) => reconcilePrompt(...a),
      setReconcilePrompt: (...a: unknown[]) => setReconcilePrompt(...a),
      repairCron: (...a: unknown[]) => repairCron(...a),
      fetchSettings: (...a: unknown[]) => fetchSettings(...a),
      updateSettings: (...a: unknown[]) => updateSettings(...a),
    },
  }
})

import ChatStatusTagsPage from './ChatStatusTagsPage'

const DEFAULT_PROMPT = 'Default reconcile prompt: check merged PRs.'
const CUSTOM_PROMPT = 'Custom prompt with different GitHub PR behavior.'

// A healthy cron the GET returns alongside the prompt. Spread into each mock so
// the page's schedule row renders; individual tests override `cron` for the
// missing / disabled / scheduler-unavailable states.
const CRON_OK = { present: true, enabled: true, schedule: 'every hour' }

// Both automation switches enabled — the shipped default. Set in beforeEach so
// every existing test renders the Automation section without wiring it up; the
// automation-specific tests below override these two mocks.
const SETTINGS_ON = { reconcilerEnabled: true, autoResumeEnabled: true }

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <ChatStatusTagsPage />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  fetchSettings.mockResolvedValue(SETTINGS_ON)
})

describe('ChatStatusTagsPage load', () => {
  it('loads the prompt into the textarea and shows the Default badge when default', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    renderPage()

    const ta = (await screen.findByTestId('cst-reconcile-prompt')) as HTMLTextAreaElement
    expect(ta.value).toBe(DEFAULT_PROMPT)
    // Default badge present; nothing unsaved; Save disabled (unchanged).
    expect(screen.getByTestId('cst-default-badge')).toBeInTheDocument()
    expect(screen.queryByTestId('cst-unsaved')).not.toBeInTheDocument()
    expect((screen.getByTestId('cst-save') as HTMLButtonElement).disabled).toBe(true)
    // Reset is disabled when the stored prompt already IS the default.
    expect((screen.getByTestId('cst-reset') as HTMLButtonElement).disabled).toBe(true)
  })

  it('hides the Default badge and enables Reset for a customized prompt', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: CUSTOM_PROMPT,
      isDefault: false,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    renderPage()

    await screen.findByTestId('cst-reconcile-prompt')
    expect(screen.queryByTestId('cst-default-badge')).not.toBeInTheDocument()
    expect((screen.getByTestId('cst-reset') as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('ChatStatusTagsPage edit -> save', () => {
  it('enables Save after an edit, writes the prompt, and re-disables on success', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    setReconcilePrompt.mockResolvedValue({
      prompt: CUSTOM_PROMPT,
      isDefault: false,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    const user = userEvent.setup()
    renderPage()

    const ta = (await screen.findByTestId('cst-reconcile-prompt')) as HTMLTextAreaElement
    await user.clear(ta)
    await user.type(ta, CUSTOM_PROMPT)

    // Now dirty: Save enabled, unsaved marker shown.
    expect((screen.getByTestId('cst-save') as HTMLButtonElement).disabled).toBe(false)
    expect(screen.getByTestId('cst-unsaved')).toBeInTheDocument()

    await user.click(screen.getByTestId('cst-save'))

    await waitFor(() => expect(setReconcilePrompt).toHaveBeenCalledWith(CUSTOM_PROMPT))
    // Server reply re-seeds "unchanged": Save disables again, marker clears,
    // and the Default badge is gone because the write returned isDefault:false.
    await waitFor(() =>
      expect((screen.getByTestId('cst-save') as HTMLButtonElement).disabled).toBe(true),
    )
    expect(screen.queryByTestId('cst-unsaved')).not.toBeInTheDocument()
  })

  it('keeps Save disabled when the draft is only whitespace', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: CUSTOM_PROMPT,
      isDefault: false,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    const user = userEvent.setup()
    renderPage()

    const ta = (await screen.findByTestId('cst-reconcile-prompt')) as HTMLTextAreaElement
    await user.clear(ta)
    await user.type(ta, '   ')
    // Dirty vs. the stored prompt, but a whitespace-only save would be a reset —
    // that path belongs to the Reset button, so Save stays disabled. Polled:
    // the disabled state derives from React state that settles a tick after
    // the last keystroke, and asserting synchronously raced it in CI.
    await waitFor(() => expect(ta.value).toBe('   '))
    await waitFor(() =>
      expect((screen.getByTestId('cst-save') as HTMLButtonElement).disabled).toBe(true),
    )
  })

  it('surfaces a save error inline', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    setReconcilePrompt.mockRejectedValue(new ChatStatusTagsApiError('write failed', 500))
    const user = userEvent.setup()
    renderPage()

    const ta = (await screen.findByTestId('cst-reconcile-prompt')) as HTMLTextAreaElement
    await user.type(ta, ' more')
    await user.click(screen.getByTestId('cst-save'))

    const err = await screen.findByTestId('cst-save-error')
    expect(err.textContent).toContain('write failed')
  })
})

describe('ChatStatusTagsPage reset', () => {
  it('resets to default with a two-click confirm sending an empty string', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: CUSTOM_PROMPT,
      isDefault: false,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    setReconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    const user = userEvent.setup()
    renderPage()

    await screen.findByTestId('cst-reconcile-prompt')
    const reset = screen.getByTestId('cst-reset')

    // First click arms (no request yet); second click sends the reset.
    await user.click(reset)
    expect(setReconcilePrompt).not.toHaveBeenCalled()
    await user.click(reset)

    await waitFor(() => expect(setReconcilePrompt).toHaveBeenCalledWith(''))
    // After the reset the textarea holds the default and the badge reappears.
    await waitFor(() =>
      expect((screen.getByTestId('cst-reconcile-prompt') as HTMLTextAreaElement).value).toBe(
        DEFAULT_PROMPT,
      ),
    )
    expect(screen.getByTestId('cst-default-badge')).toBeInTheDocument()
  })

  it('an edit disarms a pending reset confirm', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: CUSTOM_PROMPT,
      isDefault: false,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })
    const user = userEvent.setup()
    renderPage()

    const ta = (await screen.findByTestId('cst-reconcile-prompt')) as HTMLTextAreaElement
    await user.click(screen.getByTestId('cst-reset')) // arm
    await user.type(ta, 'x') // disarm via edit
    await user.click(screen.getByTestId('cst-reset')) // arms again, does NOT send
    expect(setReconcilePrompt).not.toHaveBeenCalled()
  })
})

describe('ChatStatusTagsPage failure states', () => {
  it('shows the disabled state on a 403', async () => {
    reconcilePrompt.mockRejectedValue(new ChatStatusTagsApiError('forbidden', 403))
    renderPage()
    const disabled = await screen.findByTestId('cst-disabled')
    expect(disabled).toBeInTheDocument()
    expect(screen.queryByTestId('cst-reconcile-prompt')).not.toBeInTheDocument()
  })

  it('shows a generic error on a non-403 load failure', async () => {
    reconcilePrompt.mockRejectedValue(new ChatStatusTagsApiError('boom', 500))
    renderPage()
    const err = await screen.findByTestId('cst-error')
    expect(err.textContent).toContain('boom')
  })
})

describe('ChatStatusTagsPage reconcile cron', () => {
  it('renders the schedule when the cron is healthy, with no Repair button', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: { present: true, enabled: true, schedule: 'every hour' },
    })
    renderPage()

    const ok = await screen.findByTestId('cst-cron-ok')
    // The schedule is interpolated into the healthy status line.
    expect(ok.textContent).toContain('every hour')
    expect(screen.queryByTestId('cst-cron-repair')).not.toBeInTheDocument()
    expect(screen.queryByTestId('cst-cron-warn')).not.toBeInTheDocument()
  })

  it('shows a warning and repairs a missing cron, flipping to the healthy state', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: { present: false, enabled: false, schedule: '' },
    })
    repairCron.mockResolvedValue({
      ok: true,
      cron: { present: true, enabled: true, schedule: 'every hour' },
    })
    const user = userEvent.setup()
    renderPage()

    // Missing: warning shown, Repair enabled, no healthy line yet.
    await screen.findByTestId('cst-cron-warn')
    const repair = screen.getByTestId('cst-cron-repair') as HTMLButtonElement
    expect(repair.disabled).toBe(false)
    expect(screen.queryByTestId('cst-cron-ok')).not.toBeInTheDocument()

    await user.click(repair)

    await waitFor(() => expect(repairCron).toHaveBeenCalledTimes(1))
    // The repair response flips the row to the healthy schedule line.
    const ok = await screen.findByTestId('cst-cron-ok')
    expect(ok.textContent).toContain('every hour')
    expect(screen.queryByTestId('cst-cron-warn')).not.toBeInTheDocument()
  })

  it('shows the paused state for a present-but-disabled cron and offers Repair', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: { present: true, enabled: false, schedule: 'every hour' },
    })
    repairCron.mockResolvedValue({
      ok: true,
      cron: { present: true, enabled: true, schedule: 'every hour' },
    })
    const user = userEvent.setup()
    renderPage()

    const warn = await screen.findByTestId('cst-cron-warn')
    // Paused copy, not the missing copy.
    expect(warn.textContent).toContain('paused')
    const repair = screen.getByTestId('cst-cron-repair') as HTMLButtonElement
    expect(repair.disabled).toBe(false)

    await user.click(repair)
    await waitFor(() => expect(repairCron).toHaveBeenCalledTimes(1))
    await screen.findByTestId('cst-cron-ok')
  })

  it('disables Repair when the scheduler is unavailable', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: { present: false, enabled: false, schedule: '', schedulerUnavailable: true },
    })
    renderPage()

    await screen.findByTestId('cst-cron-warn')
    const repair = screen.getByTestId('cst-cron-repair') as HTMLButtonElement
    expect(repair.disabled).toBe(true)
    expect(repairCron).not.toHaveBeenCalled()
  })

  it('surfaces a repair error inline', async () => {
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: { present: false, enabled: false, schedule: '' },
    })
    repairCron.mockRejectedValue(new ChatStatusTagsApiError('scheduler unavailable', 503))
    const user = userEvent.setup()
    renderPage()

    const repair = await screen.findByTestId('cst-cron-repair')
    await user.click(repair)

    const err = await screen.findByTestId('cst-cron-error')
    expect(err.textContent).toContain('scheduler unavailable')
    // Still missing — the failed repair left the warning in place.
    expect(screen.getByTestId('cst-cron-warn')).toBeInTheDocument()
  })
})

describe('ChatStatusTagsPage automation toggles', () => {
  // A healthy prompt GET so the page's main card renders; these tests exercise
  // the independent Automation section below it.
  const promptOk = () =>
    reconcilePrompt.mockResolvedValue({
      prompt: DEFAULT_PROMPT,
      isDefault: true,
      defaultPrompt: DEFAULT_PROMPT,
      cron: CRON_OK,
    })

  const reconcilerSwitch = () => screen.getByRole('switch', { name: 'Hourly reconciler' })
  const autoResumeSwitch = () => screen.getByRole('switch', { name: 'Auto-resume dropped chats' })

  it('renders both switches from fetched state, both on', async () => {
    promptOk()
    fetchSettings.mockResolvedValue({ reconcilerEnabled: true, autoResumeEnabled: true })
    renderPage()

    await screen.findByTestId('cst-automation')
    expect(reconcilerSwitch().getAttribute('aria-checked')).toBe('true')
    expect(autoResumeSwitch().getAttribute('aria-checked')).toBe('true')
  })

  it('reflects a disabled switch from fetched state', async () => {
    promptOk()
    fetchSettings.mockResolvedValue({ reconcilerEnabled: true, autoResumeEnabled: false })
    renderPage()

    await screen.findByTestId('cst-automation')
    expect(reconcilerSwitch().getAttribute('aria-checked')).toBe('true')
    expect(autoResumeSwitch().getAttribute('aria-checked')).toBe('false')
  })

  it('flipping the auto-resume switch PUTs only that key and reflects the response', async () => {
    promptOk()
    fetchSettings.mockResolvedValue({ reconcilerEnabled: true, autoResumeEnabled: true })
    updateSettings.mockResolvedValue({ reconcilerEnabled: true, autoResumeEnabled: false })
    const user = userEvent.setup()
    renderPage()

    await screen.findByTestId('cst-automation')
    await user.click(autoResumeSwitch())

    // Only the flipped key is sent — not both.
    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith({ autoResumeEnabled: false }),
    )
    expect(updateSettings).toHaveBeenCalledTimes(1)
    // The server's fresh state drives the rendered position.
    await waitFor(() => expect(autoResumeSwitch().getAttribute('aria-checked')).toBe('false'))
    expect(reconcilerSwitch().getAttribute('aria-checked')).toBe('true')
  })

  it('flipping the reconciler switch PUTs only reconcilerEnabled', async () => {
    promptOk()
    fetchSettings.mockResolvedValue({ reconcilerEnabled: true, autoResumeEnabled: true })
    updateSettings.mockResolvedValue({ reconcilerEnabled: false, autoResumeEnabled: true })
    const user = userEvent.setup()
    renderPage()

    await screen.findByTestId('cst-automation')
    await user.click(reconcilerSwitch())

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith({ reconcilerEnabled: false }),
    )
    await waitFor(() => expect(reconcilerSwitch().getAttribute('aria-checked')).toBe('false'))
  })

  it('shows a generic inline error on a failed toggle and keeps the fetched state', async () => {
    promptOk()
    fetchSettings.mockResolvedValue({ reconcilerEnabled: true, autoResumeEnabled: true })
    updateSettings.mockRejectedValue(new ChatStatusTagsApiError('boom', 500))
    const user = userEvent.setup()
    renderPage()

    await screen.findByTestId('cst-automation')
    await user.click(autoResumeSwitch())

    const err = await screen.findByTestId('cst-auto-resume-error')
    expect(err.textContent).toContain("Couldn't save that change")
    // The write failed, so the cache is untouched: the switch stays where the
    // fetched state put it.
    expect(autoResumeSwitch().getAttribute('aria-checked')).toBe('true')
  })

  it('shows the scheduler-unavailable message on a 503 reconciler write', async () => {
    promptOk()
    fetchSettings.mockResolvedValue({ reconcilerEnabled: true, autoResumeEnabled: true })
    updateSettings.mockRejectedValue(new ChatStatusTagsApiError('unavailable', 503))
    const user = userEvent.setup()
    renderPage()

    await screen.findByTestId('cst-automation')
    await user.click(reconcilerSwitch())

    const err = await screen.findByTestId('cst-reconciler-error')
    expect(err.textContent).toContain('scheduler is unavailable')
    expect(reconcilerSwitch().getAttribute('aria-checked')).toBe('true')
  })

  it('disables a switch while its own write is in flight, then re-enables it', async () => {
    promptOk()
    fetchSettings.mockResolvedValue({ reconcilerEnabled: true, autoResumeEnabled: true })
    // Hold the PUT open so we can observe the pending (disabled) state.
    let resolve!: (v: AutomationSettingsResp) => void
    updateSettings.mockReturnValue(
      new Promise<AutomationSettingsResp>((r) => {
        resolve = r
      }),
    )
    const user = userEvent.setup()
    renderPage()

    await screen.findByTestId('cst-automation')
    await user.click(reconcilerSwitch())

    // Mid-flight: THIS switch is disabled; the other one is not.
    await waitFor(() =>
      expect(reconcilerSwitch().getAttribute('aria-disabled')).toBe('true'),
    )
    expect(autoResumeSwitch().getAttribute('aria-disabled')).toBeNull()

    resolve({ reconcilerEnabled: false, autoResumeEnabled: true })
    // Once the write settles the switch is interactive again.
    await waitFor(() =>
      expect(reconcilerSwitch().getAttribute('aria-disabled')).toBeNull(),
    )
  })
})

type AutomationSettingsResp = { reconcilerEnabled: boolean; autoResumeEnabled: boolean }
