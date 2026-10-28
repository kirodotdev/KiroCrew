/**
 * The host side of `useAppUpdate().requestUpdate()`.
 *
 * What is pinned: the request only ever names the app the host mounted, every
 * refusal before the dialog leaves the update route uncalled, the user's Cancel
 * changes nothing, and a confirmed update runs the App Library's own route and
 * then offers a reload. The two consent cases the detail page owns (execution
 * trust, a new chat-control grant) are handed to it rather than answered here.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import type { AppUpdateResult } from '../app-sdk'

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  updateApp: vi.fn(),
  getApp: vi.fn(),
  trusted: true,
}))

// jsdom marks every event untrusted, including fireEvent's. Treat test presses
// as real input unless a test says otherwise.
vi.mock('../components/trustedPress', () => ({
  isTrustedPress: () => mocks.trusted,
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mocks.navigate }
})

vi.mock('../api/client', () => ({
  api: { updateApp: mocks.updateApp, getApp: mocks.getApp },
}))

import { useAppSelfUpdate, type SelfUpdateTarget } from '../components/appSelfUpdate'

const AVAILABLE = { available: true, latestVersion: '0.2.1', checkedAt: null }

function target(over: Partial<SelfUpdateTarget> = {}): SelfUpdateTarget {
  return { name: 'ledger-lens', label: 'Ledger Lens', version: '0.2.0', update: AVAILABLE, ...over }
}

/** Mounts the hook as AppHost does and exposes the app's button. */
function Harness({ t, active = true, onResult }: {
  t: SelfUpdateTarget
  active?: boolean
  onResult: (r: AppUpdateResult) => void
}) {
  const { requestUpdate, dialog } = useAppSelfUpdate(t, active)
  return (
    <>
      <button onClick={() => { void requestUpdate().then(onResult) }}>
        app-update-button
      </button>
      {dialog}
    </>
  )
}

async function press(t: SelfUpdateTarget, active = true): Promise<() => AppUpdateResult | null> {
  let result: AppUpdateResult | null = null
  render(<Harness t={t} active={active} onResult={r => { result = r }} />)
  fireEvent.click(screen.getByText('app-update-button'))
  return () => result
}

beforeEach(() => {
  mocks.navigate.mockReset()
  mocks.updateApp.mockReset()
  mocks.getApp.mockReset()
  mocks.trusted = true
})

describe('useAppSelfUpdate', () => {
  it('updates only the mounted app after the user confirms, then offers a reload', async () => {
    mocks.updateApp.mockResolvedValue({ ok: true, name: 'ledger-lens' })
    mocks.getApp.mockResolvedValue({ name: 'ledger-lens', version: '0.2.1' })
    const reload = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, reload } })
    try {
      const result = await press(target())
      expect(await screen.findByText('Update Ledger Lens?')).toBeTruthy()
      expect(screen.getByText(/from 0\.2\.0 to 0\.2\.1/)).toBeTruthy()
      expect(mocks.updateApp).not.toHaveBeenCalled()

      fireEvent.click(screen.getByRole('button', { name: 'Update' }))
      await waitFor(() => expect(result()).toEqual({ status: 'updated', version: '0.2.1' }))
      // One argument: the name the host bound. No source override can ride along.
      expect(mocks.updateApp).toHaveBeenCalledTimes(1)
      expect(mocks.updateApp.mock.calls[0]).toEqual(['ledger-lens'])

      expect(await screen.findByText('Version 0.2.1 is installed')).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'Reload now' }))
      await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })

  it('keeps host-owned progress visible while the update is running', async () => {
    let finish!: (value: { ok: boolean; name: string }) => void
    mocks.updateApp.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    mocks.getApp.mockResolvedValue({ name: 'ledger-lens', version: '0.2.1' })
    const result = await press(target())
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    expect(await screen.findByRole('dialog', { name: 'Updating Ledger Lens…' })).toBeTruthy()
    act(() => finish({ ok: true, name: 'ledger-lens' }))
    await waitFor(() => expect(result()?.status).toBe('updated'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Updating Ledger Lens…' })).toBeNull())
  })

  it('ignores a press on Update that the browser did not report as real input', async () => {
    mocks.trusted = false
    const result = await press(target())
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await act(async () => {})
    expect(mocks.updateApp).not.toHaveBeenCalled()
    expect(result()).toBeNull()
    expect(screen.getByText('Update Ledger Lens?')).toBeTruthy()
    // A real press still goes through.
    mocks.trusted = true
    mocks.updateApp.mockResolvedValue({ ok: true, name: 'ledger-lens' })
    mocks.getApp.mockResolvedValue({ name: 'ledger-lens', version: '0.2.1' })
    fireEvent.click(screen.getByRole('button', { name: 'Update' }))
    await waitFor(() => expect(result()?.status).toBe('updated'))
  })

  it('leaves the app untouched when the user cancels', async () => {
    const result = await press(target())
    await screen.findByText('Update Ledger Lens?')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(result()).toEqual({ status: 'cancelled', version: '0.2.0' }))
    expect(mocks.updateApp).not.toHaveBeenCalled()
  })

  it.each([
    ['no newer version is published', target({ update: { ...AVAILABLE, available: false } }), true, 'no_update_available'],
    ['the host has no update answer', target({ update: undefined }), true, 'no_update_available'],
    ['the app manages its own updates', target({ lifecycle: 'app' }), true, 'lifecycle_not_gateway'],
    ['the app is locked', target({ lifecycle: 'locked' }), true, 'lifecycle_not_gateway'],
    ['the surface is hidden', target(), false, 'inactive'],
  ])('refuses with no dialog when %s', async (_why, t, active, error) => {
    const result = await press(t, active)
    await waitFor(() => expect(result()).toEqual({ status: 'failed', version: '0.2.0', error }))
    expect(screen.queryByText('Update Ledger Lens?')).toBeNull()
    expect(mocks.updateApp).not.toHaveBeenCalled()
  })

  it('refuses a call without a user gesture where the browser reports one', async () => {
    Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: false } })
    try {
      const result = await press(target())
      await waitFor(() => expect(result()?.error).toBe('no_user_gesture'))
      expect(screen.queryByText('Update Ledger Lens?')).toBeNull()
    } finally {
      delete (navigator as { userActivation?: unknown }).userActivation
    }
  })

  it('refuses a second request while the first dialog is open', async () => {
    let first: AppUpdateResult | null = null
    let second: AppUpdateResult | null = null
    function Twice() {
      const { requestUpdate, dialog } = useAppSelfUpdate(target(), true)
      return (
        <>
          <button onClick={() => { void requestUpdate().then(r => { first = r }); void requestUpdate().then(r => { second = r }) }}>
            twice
          </button>
          {dialog}
        </>
      )
    }
    render(<Twice />)
    fireEvent.click(screen.getByText('twice'))
    await waitFor(() => expect(second).toEqual({ status: 'failed', version: '0.2.0', error: 'busy' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(first?.status).toBe('cancelled'))
  })

  it('reports the gateway refusal to the app and the user', async () => {
    mocks.updateApp.mockRejectedValue(Object.assign(new Error('registry unreachable'), {
      body: JSON.stringify({ ok: false, error: 'registry unreachable', code: 'registry_fetch_failed' }),
    }))
    const result = await press(target())
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await waitFor(() => expect(result()).toEqual({
      status: 'failed', version: '0.2.0', error: 'registry_fetch_failed', message: 'registry unreachable',
    }))
    expect(await screen.findByRole('dialog', { name: 'Update failed' })).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('Could not update Ledger Lens')
    expect(screen.getByRole('alert').textContent).toContain('registry unreachable')
  })

  it('refuses a retry from the same captured callback while the failure dialog is open', async () => {
    mocks.updateApp.mockRejectedValue(new Error('registry unreachable'))
    let request: (() => Promise<AppUpdateResult>) | null = null
    function Capture() {
      const { requestUpdate, dialog } = useAppSelfUpdate(target(), true)
      if (!request) request = requestUpdate
      return <>{dialog}</>
    }
    render(<Capture />)
    let first: AppUpdateResult | null = null
    act(() => { void request!().then(r => { first = r }) })
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await waitFor(() => expect(first?.status).toBe('failed'))
    expect(await screen.findByText('Update failed')).toBeTruthy()
    let second: AppUpdateResult | null = null
    act(() => { void request!().then(r => { second = r }) })
    await waitFor(() => expect(second).toEqual({ status: 'failed', version: '0.2.0', error: 'busy' }))
    expect(mocks.updateApp).toHaveBeenCalledTimes(1)
  })

  it('hands a lapsed execution trust to the detail page, which owns that prompt', async () => {
    mocks.updateApp.mockRejectedValue({ code: 'app_execution_denied', body: '' })
    const result = await press(target())
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await waitFor(() => expect(result()?.error).toBe('app_execution_denied'))
    expect(mocks.navigate).toHaveBeenCalledWith('/apps/detail/ledger-lens', { state: { autoAction: 'update' } })
  })

  it('sends a version that newly asks to control chats to its details page, with no reload offer', async () => {
    mocks.updateApp.mockResolvedValue({ ok: true, name: 'ledger-lens', notice: 'session_approval_reconsent' })
    mocks.getApp.mockResolvedValue({ name: 'ledger-lens', version: '0.2.1' })
    const result = await press(target())
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await waitFor(() => expect(result()).toEqual({
      status: 'updated', version: '0.2.1', notice: 'session_approval_reconsent',
    }))
    expect(mocks.navigate).toHaveBeenCalledWith('/apps/detail/ledger-lens')
    await act(async () => {})
    expect(screen.queryByText('Version 0.2.1 is installed')).toBeNull()
  })

  it('sends a version with new api permissions awaiting approval to its details page', async () => {
    mocks.updateApp.mockResolvedValue({ ok: true, name: 'ledger-lens', message: 'new permissions stay off' })
    mocks.getApp.mockResolvedValue({
      name: 'ledger-lens',
      version: '0.2.1',
      approvedGrants: { api: ['/api/apps'], events: [] },
      manifest: { permissions: { api: ['/api/apps', '/api/sessions'], events: [] } },
    })
    const result = await press(target())
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await waitFor(() => expect(result()).toEqual({ status: 'updated', version: '0.2.1', notice: 'grants_staged' }))
    expect(mocks.navigate).toHaveBeenCalledWith('/apps/detail/ledger-lens')
    await act(async () => {})
    expect(screen.queryByText('Version 0.2.1 is installed')).toBeNull()
  })

  it('offers the reload when every declared permission is already approved', async () => {
    mocks.updateApp.mockResolvedValue({ ok: true, name: 'ledger-lens' })
    mocks.getApp.mockResolvedValue({
      name: 'ledger-lens',
      version: '0.2.1',
      approvedGrants: { api: ['/api/apps'], events: ['ledger:updated'] },
      manifest: { permissions: { api: ['/api/apps'], events: ['ledger:updated'] } },
    })
    const result = await press(target())
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await waitFor(() => expect(result()).toEqual({ status: 'updated', version: '0.2.1' }))
    expect(await screen.findByText('Version 0.2.1 is installed')).toBeTruthy()
    expect(mocks.navigate).not.toHaveBeenCalled()
  })

  it('does not offer the same release again after a declined reload on a host that keeps its record', async () => {
    mocks.updateApp.mockResolvedValue({ ok: true, name: 'ledger-lens' })
    mocks.getApp.mockResolvedValue({ name: 'ledger-lens', version: '0.2.1' })
    const results: AppUpdateResult[] = []
    render(<Harness t={target()} onResult={r => { results.push(r) }} />)
    fireEvent.click(screen.getByText('app-update-button'))
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await screen.findByText('Version 0.2.1 is installed')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByText('Version 0.2.1 is installed')).toBeNull())

    fireEvent.click(screen.getByText('app-update-button'))
    await waitFor(() => expect(results.at(-1)).toEqual({ status: 'failed', version: '0.2.1', error: 'no_update_available' }))
    expect(screen.queryByText('Update Ledger Lens?')).toBeNull()
    expect(mocks.updateApp).toHaveBeenCalledTimes(1)
  })

  it('stays busy while the reload offer is open, so a second request cannot dismiss it', async () => {
    mocks.updateApp.mockResolvedValue({ ok: true, name: 'ledger-lens' })
    mocks.getApp.mockResolvedValue({ name: 'ledger-lens', version: '0.2.1' })
    const results: AppUpdateResult[] = []
    render(<Harness t={target()} onResult={r => { results.push(r) }} />)
    fireEvent.click(screen.getByText('app-update-button'))
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await screen.findByText('Version 0.2.1 is installed')

    fireEvent.click(screen.getByText('app-update-button'))
    // Refused (the release is already in), and the open offer is left alone.
    await waitFor(() => expect(results.at(-1)).toEqual({ status: 'failed', version: '0.2.1', error: 'no_update_available' }))
    expect(screen.getByText('Version 0.2.1 is installed')).toBeTruthy()
    expect(mocks.updateApp).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByText('Version 0.2.1 is installed')).toBeNull())
    // Once the offer closes the hook is free again, and it now knows 0.2.1 is in.
    fireEvent.click(screen.getByText('app-update-button'))
    await waitFor(() => expect(results.at(-1)?.error).toBe('no_update_available'))
  })

  it('opens the details page when the installed record cannot be read back', async () => {
    mocks.updateApp.mockResolvedValue({ ok: true, name: 'ledger-lens' })
    mocks.getApp.mockRejectedValue(new Error('offline'))
    const result = await press(target())
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await waitFor(() => expect(result()).toEqual({ status: 'updated', version: '0.2.1' }))
    expect(mocks.navigate).toHaveBeenCalledWith('/apps/detail/ledger-lens')
    expect(screen.queryByText('Version 0.2.1 is installed')).toBeNull()
  })
})
