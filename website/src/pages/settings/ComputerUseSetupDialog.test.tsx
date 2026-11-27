import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { grants, setupDoc } from '../../test/computerUseSetupFixtures'
import type { ComputerUseConfigData, ComputerUseSetupDoc, ComputerUseSetupState } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    getComputerUseConfig: vi.fn(),
    saveComputerUseConfig: vi.fn(),
    getComputerUseSetup: vi.fn(),
    runComputerUseSetupAction: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { ComputerUseSetupDialog } from './ComputerUseSetupDialog'
import { ComputerUseSetupStatus } from './ComputerUseSetupStatus'
import { SETUP_INSTALL_POLL_MAX_MS, SETUP_POLL_MAX_MS, SETUP_POLL_MS, SETUP_STATES, type SetupView } from './computerUseSetup'

const getSetup = api.getComputerUseSetup as ReturnType<typeof vi.fn>
const runAction = api.runComputerUseSetupAction as ReturnType<typeof vi.fn>
const saveConfig = api.saveComputerUseConfig as ReturnType<typeof vi.fn>

function config(overrides: Partial<ComputerUseConfigData> = {}): ComputerUseConfigData {
  return {
    enabled: false,
    supported: true,
    platform: 'macos',
    reason: '',
    max_tree_nodes: 1200,
    max_tree_depth: 64,
    text_limit: 500,
    attach_screenshot: true,
    screenshot_max_px: 1280,
    screenshot_jpeg_quality: 55,
    cursor_motion: false,
    cursor_motion_supported: true,
    allowed_apps: [],
    extra_denied_apps: [],
    permissions: { accessibility: 'granted', screen_recording: 'granted', responsible_hint: '' },
    limits: {},
    ...overrides,
  }
}

function renderDialog(doc: ComputerUseSetupDoc, props: { cfg?: ComputerUseConfigData; onOpenChange?: (o: boolean) => void } = {}) {
  getSetup.mockResolvedValue(doc)
  const onOpenChange = props.onOpenChange ?? vi.fn()
  const utils = renderWithProviders(<ComputerUseSetupDialog open onOpenChange={onOpenChange} config={props.cfg ?? config()} />)
  return { ...utils, onOpenChange }
}

async function view(): Promise<string | null> {
  const dialog = await screen.findByTestId('cu-setup-dialog')
  await waitFor(() => expect(dialog.getAttribute('data-view')).not.toBe('loading'))
  return dialog.getAttribute('data-view')
}

/** Advance fake time inside act, so every timer-driven update is flushed. */
async function tick(ms = 50) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** Expected view for each contract state, per platform. The mapping is total. */
const EXPECTED: Record<ComputerUseSetupState, SetupView> = {
  disabled: 'intro',
  not_configured: 'install',
  installing: 'installing',
  install_failed: 'install_failed',
  validating: 'verify',
  ready: 'ready',
  temporarily_unavailable: 'unavailable',
  unsupported: 'unsupported',
  error: 'error',
}

const PLATFORM_DOC: Record<'macos' | 'windows' | 'linux', Parameters<typeof setupDoc>[1]> = {
  // macOS has the built-in driver only: the open-source one is not offered there.
  macos: {
    platform: 'macos',
    session: { kind: 'aqua' },
    driver: { kind: 'native', version: '' },
  },
  windows: {
    platform: 'windows',
    session: { kind: 'windows_interactive' },
  },
  linux: {
    platform: 'linux',
    session: { kind: 'x11', detail: 'DISPLAY=:0, AT-SPI bus up' },
  },
}

describe('ComputerUseSetupDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  describe.each(['macos', 'windows', 'linux'] as const)('on %s', platform => {
    it.each(SETUP_STATES)('renders a specific, actionable view for %s', async state => {
      renderDialog(setupDoc(state, PLATFORM_DOC[platform]))
      expect(await view()).toBe(EXPECTED[state])
      const heading = screen.getByRole('heading', { level: 3 })
      expect(heading.textContent?.trim()).toBeTruthy()
      // Never "ready" for a state that is not `ready`.
      if (state !== 'ready') expect(screen.queryByText(/computer use is ready/i)).not.toBeInTheDocument()
      // Every view offers at least one way forward or out.
      expect(within(screen.getByTestId('cu-setup-dialog')).getAllByRole('button').length).toBeGreaterThan(0)
    })

    it('lists only the steps that exist on this platform', async () => {
      renderDialog(setupDoc('not_configured', PLATFORM_DOC[platform]))
      await view()
      const rail = screen.getByTestId('cu-setup-steps')
      const ids = Array.from(rail.querySelectorAll('li')).map(li => li.getAttribute('data-step'))
      if (platform === 'macos') expect(ids).toEqual(['verify', 'ready'])
      else expect(ids).toEqual(['install', 'session', 'verify', 'ready'])
    })
  })

  it('explains on a Mac that the open-source driver is not offered and offers the built-in one', async () => {
    runAction.mockResolvedValue(setupDoc('ready', { ...PLATFORM_DOC.macos }))
    renderDialog(
      setupDoc('unsupported', {
        platform: 'macos',
        session: { kind: 'aqua' },
        driver: { kind: 'upstream', version: '', installed: false, healthy: false },
        problem: { code: 'unsupported_platform', message: 'not offered on macOS', recoverable: false },
        steps: [{ id: 'verify', status: 'failed' }, { id: 'ready', status: 'pending' }],
      }),
    )
    expect(await view()).toBe('unsupported')
    const dialog = screen.getByTestId('cu-setup-dialog')
    expect(within(dialog).getByText(/not offered on macOS yet/i)).toBeInTheDocument()
    expect(within(dialog).queryByText(/cannot run Computer Use/i)).not.toBeInTheDocument()
    // The heading names the refused choice; it never says Computer Use is unavailable.
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('The open-source driver is not offered on macOS')
    expect(within(dialog).queryByText(/not available here/i)).not.toBeInTheDocument()
    // Said once, in the body: the backend's sentence is not repeated as a notice.
    expect(within(dialog).queryByTestId('cu-setup-problem')).not.toBeInTheDocument()
    // No install step anywhere on a Mac.
    const ids = Array.from(screen.getByTestId('cu-setup-steps').querySelectorAll('li')).map(li => li.getAttribute('data-step'))
    expect(ids).toEqual(['verify', 'ready'])
    fireEvent.click(screen.getByTestId('cu-use-builtin'))
    await waitFor(() => expect(runAction).toHaveBeenCalledWith('use_builtin_driver'))
  })

  it('offers no built-in driver switch on an unsupported Linux desktop', async () => {
    renderDialog(setupDoc('unsupported', { ...PLATFORM_DOC.linux, problem: { code: 'unsupported_session', message: 'x', recoverable: false } }))
    expect(await view()).not.toBe('install')
    expect(screen.queryByTestId('cu-use-builtin')).not.toBeInTheDocument()
  })

  it.each(['macos', 'windows', 'linux'] as const)('never labels the %s driver a preview', async platform => {
    getSetup.mockResolvedValue(setupDoc('ready', PLATFORM_DOC[platform]))
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} permissions={grants()} />)
    const row = await screen.findByTestId('cu-status-driver')
    expect(row.textContent).not.toMatch(/preview/i)
  })

  it('stops polling a moving state at its bound and says so', async () => {
    vi.useFakeTimers()
    renderDialog(setupDoc('validating'))
    await tick()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SETUP_POLL_MS * 3)
    })
    expect(getSetup.mock.calls.length).toBeGreaterThan(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SETUP_POLL_MAX_MS)
    })
    const atBound = getSetup.mock.calls.length
    expect(screen.getByText(/taking longer than expected/i)).toBeInTheDocument()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SETUP_POLL_MAX_MS)
    })
    expect(getSetup.mock.calls.length).toBe(atBound)
  })

  it('bounds the install wait too, then offers to retry', async () => {
    vi.useFakeTimers()
    renderDialog(setupDoc('installing', { driver: { installed: false, healthy: false } }))
    await tick()
    expect(screen.queryByRole('button', { name: /try the install again/i })).not.toBeInTheDocument()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SETUP_INSTALL_POLL_MAX_MS + 100)
    })
    expect(screen.getByText(/taking longer than expected/i)).toBeInTheDocument()
    runAction.mockResolvedValue(setupDoc('installing'))
    fireEvent.click(screen.getByRole('button', { name: /try the install again/i }))
    await tick()
    expect(runAction).toHaveBeenCalledWith('install')
  })

  it('resumes where it was after being closed', async () => {
    getSetup.mockResolvedValue(setupDoc('install_failed'))
    const { rerender } = renderWithProviders(<ComputerUseSetupDialog open onOpenChange={() => {}} config={config()} />)
    expect(await view()).toBe('install_failed')
    rerender(<ComputerUseSetupDialog open={false} onOpenChange={() => {}} config={config()} />)
    await waitFor(() => expect(screen.queryByTestId('cu-setup-dialog')).not.toBeInTheDocument())
    rerender(<ComputerUseSetupDialog open onOpenChange={() => {}} config={config()} />)
    expect(await view()).toBe('install_failed')
    // Reopening re-ran nothing: no install, no reset.
    expect(runAction).not.toHaveBeenCalled()
  })

  // Two ways to close: the `open` prop keeps the observer (an `enabled` change),
  // and the Settings panel unmounts the dialog (a new observer on reopen).
  it.each([
    { how: 'its open prop', unmount: false },
    { how: 'unmounting it, as the Settings panel does', unmount: true },
  ])('re-reads a cached ready document when reopened after closing through $how', async ({ unmount }) => {
    const dialog = (open: boolean) => <ComputerUseSetupDialog open={open} onOpenChange={() => {}} config={config()} />
    getSetup.mockResolvedValue(setupDoc('ready'))
    // The shipped client's staleTime, under which a cached entry never ages.
    const { rerender } = renderWithProviders(dialog(true), { queryDefaults: { staleTime: Infinity } })
    expect(await view()).toBe('ready')
    rerender(unmount ? <></> : dialog(false))
    await waitFor(() => expect(screen.queryByTestId('cu-setup-dialog')).not.toBeInTheDocument())
    // The driver stopped answering after that read.
    getSetup.mockResolvedValue(setupDoc('temporarily_unavailable', { problem: { code: 'crash_loop', message: 'x', recoverable: true } }))
    rerender(dialog(true))
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'unavailable'))
    expect(screen.getByRole('button', { name: /restart driver/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^done$/i })).not.toBeInTheDocument()
    expect(runAction).not.toHaveBeenCalled()
  })

  it('turns the feature on from the intro through the existing config save', async () => {
    saveConfig.mockResolvedValue(config({ enabled: true, sessions_reset: 1 }))
    renderDialog(setupDoc('disabled'))
    await view()
    getSetup.mockResolvedValue(setupDoc('not_configured', { driver: { installed: false, healthy: false } }))
    expect(screen.getByRole('button', { name: 'Turn on Computer Use' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: /turn on computer use/i }))
    await waitFor(() => expect(saveConfig).toHaveBeenCalledWith({ enabled: true }))
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'install'))
    expect(runAction).not.toHaveBeenCalled()
  })

  it('says so when the enable saved but the agent spec was not rewritten', async () => {
    saveConfig.mockResolvedValue(config({ enabled: true, agent_spec_rewritten: false } as Partial<ComputerUseConfigData>))
    renderDialog(setupDoc('disabled'))
    await view()
    getSetup.mockResolvedValue(setupDoc('not_configured', { driver: { installed: false, healthy: false } }))
    fireEvent.click(screen.getByRole('button', { name: /turn on computer use/i }))
    const notice = await screen.findByTestId('agent-spec-not-rewritten')
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('keeps Turn on Computer Use disabled when policy locks the feature', async () => {
    renderDialog(setupDoc('disabled'), { cfg: config({ locked_by_policy: true }) })
    await view()
    expect(screen.getByRole('button', { name: /turn on computer use/i })).toBeDisabled()
    expect(screen.getByText(/policy on this build/i)).toBeInTheDocument()
  })

  it('Not now closes without changing anything', async () => {
    const { onOpenChange } = renderDialog(setupDoc('disabled'))
    await view()
    fireEvent.click(screen.getByRole('button', { name: /not now/i }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(saveConfig).not.toHaveBeenCalled()
    expect(runAction).not.toHaveBeenCalled()
  })

  it.each([
    ['not_configured', /install driver/i, 'install'],
    ['install_failed', /try the install again/i, 'install'],
    ['validating', /run check/i, 'verify'],
    ['temporarily_unavailable', /restart driver/i, 'restart_driver'],
    ['error', /reset setup/i, 'reset'],
  ] as const)('%s posts its one action', async (state, name, action) => {
    runAction.mockResolvedValue(setupDoc('ready'))
    renderDialog(setupDoc(state))
    await view()
    fireEvent.click(screen.getByRole('button', { name }))
    await waitFor(() => expect(runAction).toHaveBeenCalledWith(action))
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'ready'))
  })

  it('shows a failed action as a notice and stays on the step', async () => {
    runAction.mockRejectedValue(new Error('network'))
    renderDialog(setupDoc('not_configured'))
    await view()
    fireEvent.click(screen.getByRole('button', { name: /install driver/i }))
    expect(await screen.findByText(/did not go through/i)).toBeInTheDocument()
    expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'install')
  })

  it('names endpoint security software, not a version, when the driver was stopped at launch', async () => {
    renderDialog(setupDoc('install_failed', { blocked: 'signal 9', user_action: 'English only' }))
    expect(await view()).toBe('install_failed')
    expect(screen.getByText(/stopped as soon as it started \(signal 9\)/i)).toBeInTheDocument()
    expect(screen.getByText(/keep using the built-in driver/i)).toBeInTheDocument()
    expect(screen.queryByText('English only')).not.toBeInTheDocument()
    expect(screen.queryByText(/version/i)).not.toBeInTheDocument()
  })

  it('says the built-in driver is in use when the upstream one was blocked', async () => {
    getSetup.mockResolvedValue(setupDoc('ready', { platform: 'windows', blocked: 'access denied', driver: { kind: 'native' } }))
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} permissions={grants()} part="summary" />)
    const notice = await screen.findByTestId('cu-status-blocked')
    expect(notice).toHaveTextContent(/is using the built-in driver/i)
    expect(notice).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('cu-open-setup')).not.toBeInTheDocument()
  })

  it('explains Restart driver in one line beside it', async () => {
    getSetup.mockResolvedValue(setupDoc('ready', { driver: { kind: 'upstream', installed: true, healthy: true } }))
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} permissions={grants()} part="details" />)
    expect(await screen.findByTestId('cu-restart-hint')).toHaveTextContent(/only if computer use stops responding/i)
  })

  it('shows the setup problem through an error notice in the card, never as a limitation', async () => {
    getSetup.mockResolvedValue(
      setupDoc('error', {
        platform: 'linux',
        problem: { code: 'verify_failed', message: 'no on-screen window was found to read', recoverable: true },
        user_action: 'Fix the reason above, then check again.',
        limitations: ['Key presses in the background need /dev/uinput.'],
      }),
    )
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} permissions={grants()} />)
    const notice = await screen.findByTestId('cu-status-problem')
    expect(within(notice).getByRole('alert')).toHaveTextContent(/no on-screen window was found to read\. Fix the reason above/i)
    expect(screen.queryByTestId('cu-status-user-action')).not.toBeInTheDocument()
    // The standing limit is still listed, and the failure is not.
    const list = screen.getByText(/background need \/dev\/uinput/i).closest('ul') as HTMLElement
    expect(within(list).queryByText(/no on-screen window/i)).not.toBeInTheDocument()
  })

  it('shows the problem in the dialog once, as an error notice', async () => {
    renderDialog(
      setupDoc('unsupported', {
        platform: 'linux',
        session: { kind: 'wayland' },
        problem: { code: 'unsupported_session', message: 'this Wayland session has no X server', recoverable: false },
        user_action: 'this Wayland session has no X server',
      }),
    )
    await view()
    const notice = screen.getByTestId('cu-setup-problem')
    expect(within(notice).getByRole('alert')).toHaveTextContent('this Wayland session has no X server')
    expect(screen.getAllByText('this Wayland session has no X server')).toHaveLength(1)
    expect(screen.queryByTestId('cu-setup-user-action')).not.toBeInTheDocument()
  })

  it('merges the install failure reason and the hint into one notice', async () => {
    renderDialog(
      setupDoc('install_failed', {
        problem: { code: 'install_failed', message: 'The download was blocked before it finished.', recoverable: true },
        user_action: 'Try the install again.',
      }),
    )
    expect(await view()).toBe('install_failed')
    const alerts = screen.getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(alerts[0]).toHaveTextContent(/blocked before it finished\. Try the install again\. Nothing was installed/i)
  })

  it('offers the built-in driver when the upstream one was blocked at launch', async () => {
    runAction.mockResolvedValue(setupDoc('ready', { driver: { kind: 'native' } }))
    renderDialog(setupDoc('install_failed', { blocked: 'signal 9', problem: { code: 'install_blocked', message: 'x', recoverable: true } }))
    expect(await view()).toBe('install_failed')
    fireEvent.click(screen.getByTestId('cu-use-builtin'))
    await waitFor(() => expect(runAction).toHaveBeenCalledWith('use_builtin_driver'))
  })

  it('offers no built-in driver on Linux, where none exists', async () => {
    renderDialog(setupDoc('install_failed', { platform: 'linux', blocked: 'signal 9', problem: { code: 'install_blocked', message: 'x', recoverable: true } }))
    expect(await view()).toBe('install_failed')
    expect(screen.queryByTestId('cu-use-builtin')).not.toBeInTheDocument()
  })

  it('names a blocked launch in the heading', async () => {
    renderDialog(setupDoc('temporarily_unavailable', { blocked: 'signal 9', problem: { code: 'launch_blocked', message: 'x', recoverable: true } }))
    expect(await view()).toBe('unavailable')
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('Something on this computer stopped the driver')
    expect(screen.queryByText('The driver is not answering')).not.toBeInTheDocument()
  })

  it('keeps the not-answering heading when nothing blocked the driver', async () => {
    renderDialog(setupDoc('temporarily_unavailable', { problem: { code: 'crash_loop', message: 'x', recoverable: true } }))
    expect(await view()).toBe('unavailable')
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('The driver is not answering')
  })

  it('keeps the blocked-driver view to two actions: retry, and the built-in driver', async () => {
    renderDialog(setupDoc('temporarily_unavailable', { blocked: 'signal 9', problem: { code: 'launch_blocked', message: 'x', recoverable: true } }))
    expect(await view()).toBe('unavailable')
    const footer = screen.getByTestId('cu-use-builtin').parentElement as HTMLElement
    expect(within(footer).getAllByRole('button')).toHaveLength(2)
    expect(within(footer).queryByRole('button', { name: /check again/i })).not.toBeInTheDocument()
  })

  it('reruns the desktop check from Check again after a failed check', async () => {
    runAction.mockResolvedValue(setupDoc('ready'))
    renderDialog(setupDoc('error', { problem: { code: 'verify_failed', message: 'no on-screen window', recoverable: true } }))
    await view()
    fireEvent.click(screen.getByRole('button', { name: /check again/i }))
    await waitFor(() => expect(runAction).toHaveBeenCalledWith('verify'))
  })

  it('re-reads the document from Check again, posting nothing, when no check failed', async () => {
    renderDialog(setupDoc('temporarily_unavailable', { platform: 'windows', session: { kind: 'session0' } }))
    expect(await view()).toBe('session0')
    const reads = getSetup.mock.calls.length
    getSetup.mockResolvedValue(setupDoc('ready'))
    fireEvent.click(screen.getByRole('button', { name: /check again/i }))
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'ready'))
    expect(getSetup.mock.calls.length).toBeGreaterThan(reads)
    expect(runAction).not.toHaveBeenCalled()
  })

  it('offers Run check in the card after a failed desktop check', async () => {
    getSetup.mockResolvedValue(setupDoc('error', { platform: 'linux', problem: { code: 'verify_failed', message: 'x', recoverable: true } }))
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} permissions={grants()} part="details" />)
    expect(await screen.findByRole('button', { name: /run check/i })).toBeInTheDocument()
  })

  it('tells the user the install keeps going if the dialog is closed', async () => {
    renderDialog(setupDoc('installing', { driver: { installed: false } }))
    expect(await view()).toBe('installing')
    expect(screen.getByTestId('cu-installing-keeps-going')).toHaveTextContent(/close this window; the install keeps going/i)
  })

  it('shows a failed Restart driver under More, where the button is', async () => {
    getSetup.mockResolvedValue(setupDoc('ready', { driver: { kind: 'upstream', installed: true, healthy: true } }))
    runAction.mockRejectedValue(new Error('network'))
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} permissions={grants()} part="details" />)
    fireEvent.click(await screen.findByRole('button', { name: /restart driver/i }))
    expect(await screen.findByText(/did not go through/i)).toBeInTheDocument()
  })

  it('renders a failed status read with Check again, never a spinner', async () => {
    getSetup.mockRejectedValue(new Error('down'))
    renderWithProviders(<ComputerUseSetupDialog open onOpenChange={() => {}} config={config()} />)
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'load_error'))
    getSetup.mockResolvedValue(setupDoc('ready'))
    fireEvent.click(screen.getByRole('button', { name: /check again/i }))
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'ready'))
    expect(runAction).not.toHaveBeenCalled()
  })

  it('explains Session 0 on Windows instead of a generic refusal', async () => {
    renderDialog(setupDoc('temporarily_unavailable', { platform: 'windows', session: { kind: 'session0' } }))
    expect(await view()).toBe('session0')
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent(/no desktop to control/i)
    expect(screen.getAllByText(/Session 0/).length).toBeGreaterThan(0)
  })

  it('explains a missing Linux desktop', async () => {
    renderDialog(setupDoc('temporarily_unavailable', { platform: 'linux', session: { kind: 'none' } }))
    expect(await view()).toBe('no_desktop')
    expect(screen.getByText(/X11 or Wayland/)).toBeInTheDocument()
    // Starting from a desktop session is the fix, not restarting the driver.
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('No desktop session found')
    expect(screen.queryByRole('button', { name: /restart driver/i })).not.toBeInTheDocument()
  })

  it('shows the Wayland screen-sharing note and the backend limitations', async () => {
    renderDialog(
      setupDoc('validating', {
        platform: 'linux',
        session: { kind: 'wayland', detail: 'GNOME Shell' },
        limitations: ['Pointer moves are not available on this compositor.'],
      }),
    )
    await view()
    const facts = screen.getByTestId('cu-setup-desktop')
    expect(within(facts).getByText(/approve screen sharing/i)).toBeInTheDocument()
    expect(within(facts).getByText('Pointer moves are not available on this compositor.')).toBeInTheDocument()
    expect(within(facts).getByText(/GNOME Shell/)).toBeInTheDocument()
  })

  it('marks the current step for assistive technology', async () => {
    renderDialog(
      setupDoc('not_configured', {
        platform: 'linux',
        steps: [
          { id: 'install', status: 'done' },
          { id: 'session', status: 'done' },
          { id: 'verify', status: 'current' },
          { id: 'ready', status: 'pending' },
        ],
      }),
    )
    await view()
    const current = screen.getByTestId('cu-setup-steps').querySelector('[aria-current="step"]')
    expect(current?.getAttribute('data-step')).toBe('verify')
  })

  it('moves focus to the new step heading when the view changes', async () => {
    runAction.mockResolvedValue(setupDoc('install_failed'))
    renderDialog(setupDoc('not_configured'))
    await view()
    fireEvent.click(screen.getByRole('button', { name: /install driver/i }))
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'install_failed'))
    await waitFor(() => expect(document.activeElement?.id).toBe('cu-setup-view-title'))
  })
})
