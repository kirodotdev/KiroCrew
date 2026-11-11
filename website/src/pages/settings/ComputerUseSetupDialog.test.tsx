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
  macos: { platform: 'macos', session: { kind: 'aqua' } },
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
      if (platform === 'macos') expect(ids).toEqual(['install', 'accessibility', 'screen_recording', 'verify', 'ready'])
      else expect(ids).toEqual(['install', 'session', 'verify', 'ready'])
    })
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
      // Polls and a state change to the next grant do not open anything.
      getSetup.mockResolvedValue(setupDoc('requesting_screen_recording', { permissions: { screen_recording: 'missing' } }))
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

  it('does not poll a grant screen until the user opened System Settings', async () => {
    vi.useFakeTimers()
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      renderDialog(setupDoc('requesting_accessibility', { permissions: { accessibility: 'missing' } }))
      await tick()
      const before = getSetup.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SETUP_POLL_MS * 5)
      })
      expect(getSetup.mock.calls.length).toBe(before)
      fireEvent.click(screen.getByTestId('cu-setup-open-accessibility'))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SETUP_POLL_MS * 2 + 50)
      })
      expect(getSetup.mock.calls.length).toBeGreaterThan(before)
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
