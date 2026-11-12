import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { setupDoc } from '../../test/computerUseSetupFixtures'
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

function currentView(): string | null {
  return screen.getByTestId('cu-setup-dialog').getAttribute('data-view')
}

/** Expected view for each contract state, per platform. A Windows or Linux
 *  document never names a permission screen, so the permission states are
 *  macOS-only in practice, but the mapping is still total. */
const EXPECTED: Record<ComputerUseSetupState, SetupView> = {
  disabled: 'intro',
  not_configured: 'install',
  installing: 'installing',
  install_failed: 'install_failed',
  requesting_accessibility: 'accessibility',
  accessibility_denied: 'accessibility',
  requesting_screen_recording: 'screen_recording',
  screen_recording_denied: 'screen_recording',
  waiting_for_system_settings: 'waiting',
  restart_required: 'restart',
  validating: 'verify',
  ready: 'ready',
  temporarily_unavailable: 'unavailable',
  permission_revoked: 'revoked',
  unsupported: 'unsupported',
  error: 'error',
}

const PLATFORM_DOC: Record<'macos' | 'windows' | 'linux', Parameters<typeof setupDoc>[1]> = {
  // macOS has the built-in driver only: the open-source one is not offered there.
  macos: {
    platform: 'macos',
    session: { kind: 'aqua' },
    driver: { kind: 'native', version: '' },
    permissions: { accessibility: 'granted', screen_recording: 'granted', owner_name: 'Terminal' },
  },
  windows: {
    platform: 'windows',
    session: { kind: 'windows_interactive' },
    permissions: { accessibility: 'not_applicable', screen_recording: 'not_applicable' },
  },
  linux: {
    platform: 'linux',
    session: { kind: 'x11', detail: 'DISPLAY=:0, AT-SPI bus up' },
    permissions: { accessibility: 'not_applicable', screen_recording: 'not_applicable' },
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
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} />)
    const row = await screen.findByTestId('cu-status-driver')
    expect(row.textContent).not.toMatch(/preview/i)
  })

  it('names the app to enable exactly as the document does', async () => {
    renderDialog(setupDoc('accessibility_denied', { permissions: { owner_name: 'Example Helper', accessibility: 'missing' } }))
    await view()
    const dialog = screen.getByTestId('cu-setup-dialog')
    expect(within(dialog).getAllByText(/Example Helper/).length).toBeGreaterThan(0)
    expect(within(dialog).queryByText(/CuaDriver/)).not.toBeInTheDocument()
  })

  it('opens System Settings once per click and never on its own', async () => {
    vi.useFakeTimers()
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      renderDialog(setupDoc('requesting_accessibility', { permissions: { accessibility: 'missing' } }))
      await tick()
      expect(open).not.toHaveBeenCalled()
      fireEvent.click(screen.getByTestId('cu-setup-open-accessibility'))
      expect(open).toHaveBeenCalledTimes(1)
      expect(open).toHaveBeenCalledWith(
        'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility',
        '_blank',
        'noopener,noreferrer',
      )
      // Rechecks and a state change to the next grant do not open anything.
      runAction.mockResolvedValue(setupDoc('requesting_screen_recording', { permissions: { screen_recording: 'missing' } }))
      await tick(SETUP_POLL_MS * 3)
      expect(currentView()).toBe('screen_recording')
      expect(open).toHaveBeenCalledTimes(1)
      fireEvent.click(screen.getByTestId('cu-setup-open-screen_recording'))
      expect(open).toHaveBeenCalledTimes(2)
      expect(open).toHaveBeenLastCalledWith(
        'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
        '_blank',
        'noopener,noreferrer',
      )
    } finally {
      open.mockRestore()
    }
  })

  it('rechecks a grant only after the user opened System Settings, then stops at the bound', async () => {
    vi.useFakeTimers()
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      const waiting = setupDoc('requesting_accessibility', { permissions: { accessibility: 'missing' } })
      renderDialog(waiting)
      runAction.mockResolvedValue(waiting)
      await tick()
      const reads = getSetup.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SETUP_POLL_MS * 5)
      })
      expect(runAction).not.toHaveBeenCalled()
      fireEvent.click(screen.getByTestId('cu-setup-open-accessibility'))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SETUP_POLL_MS * 2 + 50)
      })
      // The wait asks the driver (a non-prompting recheck), not the recorded grants.
      expect(runAction).toHaveBeenCalledWith('recheck')
      expect(getSetup.mock.calls.length).toBe(reads)
      expect(screen.getByText(/waiting for you to turn on/i)).toBeInTheDocument()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SETUP_POLL_MAX_MS)
      })
      const atBound = runAction.mock.calls.length
      // Past the bound the spinner gives way to "taking longer", and the rechecks stop.
      expect(screen.queryByText(/waiting for you to turn on/i)).not.toBeInTheDocument()
      expect(screen.getByText(/taking longer than expected/i)).toBeInTheDocument()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SETUP_POLL_MAX_MS)
      })
      expect(runAction.mock.calls.length).toBe(atBound)
    } finally {
      open.mockRestore()
    }
  })

  it('a granted switch moves the view through the recheck', async () => {
    vi.useFakeTimers()
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      renderDialog(setupDoc('requesting_accessibility', { permissions: { accessibility: 'missing' } }))
      runAction.mockResolvedValue(setupDoc('requesting_screen_recording', { permissions: { screen_recording: 'missing' } }))
      await tick()
      fireEvent.click(screen.getByTestId('cu-setup-open-accessibility'))
      await tick(SETUP_POLL_MS + 50)
      expect(currentView()).toBe('screen_recording')
    } finally {
      open.mockRestore()
    }
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
    getSetup.mockResolvedValue(setupDoc('accessibility_denied', { permissions: { accessibility: 'missing' } }))
    const { rerender } = renderWithProviders(<ComputerUseSetupDialog open onOpenChange={() => {}} config={config()} />)
    expect(await view()).toBe('accessibility')
    rerender(<ComputerUseSetupDialog open={false} onOpenChange={() => {}} config={config()} />)
    await waitFor(() => expect(screen.queryByTestId('cu-setup-dialog')).not.toBeInTheDocument())
    rerender(<ComputerUseSetupDialog open onOpenChange={() => {}} config={config()} />)
    expect(await view()).toBe('accessibility')
    // Reopening re-ran nothing: no install, no reset.
    expect(runAction).not.toHaveBeenCalled()
  })

  it('turns the feature on from the intro through the existing config save', async () => {
    saveConfig.mockResolvedValue(config({ enabled: true, sessions_reset: 1 }))
    renderDialog(setupDoc('disabled'))
    await view()
    getSetup.mockResolvedValue(setupDoc('not_configured', { driver: { installed: false, healthy: false } }))
    fireEvent.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(saveConfig).toHaveBeenCalledWith({ enabled: true }))
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'install'))
    expect(runAction).not.toHaveBeenCalled()
  })

  it('says so when the enable saved but the agent spec was not rewritten', async () => {
    saveConfig.mockResolvedValue(config({ enabled: true, agent_spec_rewritten: false } as Partial<ComputerUseConfigData>))
    renderDialog(setupDoc('disabled'))
    await view()
    getSetup.mockResolvedValue(setupDoc('not_configured', { driver: { installed: false, healthy: false } }))
    fireEvent.click(screen.getByRole('button', { name: /continue/i }))
    expect(await screen.findByTestId('agent-spec-not-rewritten')).toBeInTheDocument()
  })

  it('keeps Continue disabled when policy locks the feature', async () => {
    renderDialog(setupDoc('disabled'), { cfg: config({ locked_by_policy: true }) })
    await view()
    expect(screen.getByRole('button', { name: /continue/i })).toBeDisabled()
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
    ['restart_required', /restart driver/i, 'restart_driver'],
    ['validating', /run check/i, 'verify'],
    ['temporarily_unavailable', /restart driver/i, 'restart_driver'],
    ['error', /reset setup/i, 'reset'],
    ['accessibility_denied', /check again/i, 'recheck'],
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
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} part="summary" />)
    expect(await screen.findByTestId('cu-status-blocked')).toHaveTextContent(/is using the built-in driver/i)
    expect(screen.queryByTestId('cu-open-setup')).not.toBeInTheDocument()
  })

  it('explains Restart driver in one line beside it', async () => {
    getSetup.mockResolvedValue(setupDoc('ready', { driver: { kind: 'upstream', installed: true, healthy: true } }))
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} part="details" />)
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
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} />)
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

  it('offers Run check in the card after a failed desktop check', async () => {
    getSetup.mockResolvedValue(setupDoc('error', { platform: 'linux', problem: { code: 'verify_failed', message: 'x', recoverable: true } }))
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} part="details" />)
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
    renderWithProviders(<ComputerUseSetupStatus onOpenSetup={vi.fn()} part="details" />)
    fireEvent.click(await screen.findByRole('button', { name: /restart driver/i }))
    expect(await screen.findByText(/did not go through/i)).toBeInTheDocument()
  })

  it('says so when the background grant recheck fails, instead of waiting silently', async () => {
    vi.useFakeTimers()
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      renderDialog(setupDoc('requesting_accessibility', { permissions: { accessibility: 'missing' } }))
      runAction.mockRejectedValue(new Error('network'))
      await tick()
      expect(screen.queryByText(/could not read the computer use setup status/i)).not.toBeInTheDocument()
      fireEvent.click(screen.getByTestId('cu-setup-open-accessibility'))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SETUP_POLL_MS * 2 + 50)
      })
      expect(runAction).toHaveBeenCalledWith('recheck')
      expect(screen.getByText(/could not read the computer use setup status/i)).toBeInTheDocument()
    } finally {
      open.mockRestore()
    }
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
    renderDialog(setupDoc('unsupported', { platform: 'windows', session: { kind: 'session0' } }))
    expect(await view()).toBe('session0')
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent(/no desktop to control/i)
    expect(screen.getAllByText(/Session 0/).length).toBeGreaterThan(0)
  })

  it('explains a missing Linux desktop', async () => {
    renderDialog(setupDoc('unsupported', { platform: 'linux', session: { kind: 'none' } }))
    expect(await view()).toBe('no_desktop')
    expect(screen.getByText(/X11 or Wayland/)).toBeInTheDocument()
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
      setupDoc('requesting_accessibility', {
        steps: [
          { id: 'install', status: 'done' },
          { id: 'accessibility', status: 'current' },
          { id: 'screen_recording', status: 'pending' },
          { id: 'verify', status: 'pending' },
          { id: 'ready', status: 'pending' },
        ],
      }),
    )
    await view()
    const current = screen.getByTestId('cu-setup-steps').querySelector('[aria-current="step"]')
    expect(current?.getAttribute('data-step')).toBe('accessibility')
  })

  it('moves focus to the new step heading when the view changes', async () => {
    runAction.mockResolvedValue(setupDoc('requesting_accessibility', { permissions: { accessibility: 'missing' } }))
    renderDialog(setupDoc('not_configured'))
    await view()
    fireEvent.click(screen.getByRole('button', { name: /install driver/i }))
    await waitFor(() => expect(screen.getByTestId('cu-setup-dialog')).toHaveAttribute('data-view', 'accessibility'))
    await waitFor(() => expect(document.activeElement?.id).toBe('cu-setup-view-title'))
  })
})
