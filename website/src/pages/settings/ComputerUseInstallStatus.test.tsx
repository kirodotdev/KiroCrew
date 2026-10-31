import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { AgentDesktopStatus } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    getAgentDesktopStatus: vi.fn(),
    installAgentDesktop: vi.fn(),
    openAgentDesktopPermissionsGuide: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { ComputerUseInstallStatus } from './ComputerUseInstallStatus'

function status(overrides: Partial<AgentDesktopStatus> = {}): AgentDesktopStatus {
  return {
    installed: false,
    version: null,
    pinned: '0.4.0',
    path: null,
    driver_active: 'native',
    permissions: { accessibility: 'denied', screen_recording: 'unknown' },
    job: { state: 'idle', log_tail: '', error: null },
    ...overrides,
  }
}

const getStatus = () => vi.mocked(api.getAgentDesktopStatus)

async function renderCard(platform = 'macos') {
  renderWithProviders(<ComputerUseInstallStatus platform={platform} />)
  return screen.findByTestId('agent-desktop-status')
}

describe('ComputerUseInstallStatus', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows a missing install with Install and the permission guide', async () => {
    getStatus().mockResolvedValue(status())
    const card = await renderCard()
    expect(within(card).getByText('Not installed')).toBeInTheDocument()
    expect(within(card).getByText('0.4.0')).toBeInTheDocument()
    expect(within(card).getByText('Built-in')).toBeInTheDocument()
    expect(within(card).getByText('Not granted')).toBeInTheDocument()
    expect(within(card).getByText('Could not check')).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: /^install$/i })).toBeEnabled()
    expect(within(card).getByRole('button', { name: /open permission guide/i })).toBeInTheDocument()
  })

  it('offers no button when the pinned version is installed and granted', async () => {
    getStatus().mockResolvedValue(
      status({
        installed: true,
        version: '0.4.0',
        path: '/opt/agent-desktop',
        driver_active: 'agent-desktop',
        permissions: { accessibility: 'granted', screen_recording: 'granted' },
      }),
    )
    const card = await renderCard()
    expect(within(card).getByText('Up to date')).toBeInTheDocument()
    expect(within(card).getByText('/opt/agent-desktop')).toBeInTheDocument()
    expect(within(card).queryByRole('button')).toBeNull()
  })

  it('offers Update when the installed version is behind the pin', async () => {
    getStatus().mockResolvedValue(status({ installed: true, version: '0.3.0' }))
    const card = await renderCard()
    expect(within(card).getByText('Update available')).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: /^update$/i })).toBeInTheDocument()
  })

  it('hides the permission rows off macOS', async () => {
    getStatus().mockResolvedValue(status())
    const card = await renderCard('windows')
    expect(within(card).queryByText('Accessibility')).toBeNull()
    expect(within(card).queryByRole('button', { name: /open permission guide/i })).toBeNull()
  })

  it('starts an install and shows the running job with its log tail', async () => {
    getStatus().mockResolvedValue(status())
    vi.mocked(api.installAgentDesktop).mockResolvedValue(
      status({ job: { state: 'running', log_tail: 'downloading 0.4.0', error: null } }),
    )
    const card = await renderCard()
    getStatus().mockResolvedValue(status({ job: { state: 'running', log_tail: 'downloading 0.4.0', error: null } }))
    fireEvent.click(within(card).getByRole('button', { name: /^install$/i }))
    expect(await screen.findByTestId('agent-desktop-log')).toHaveTextContent('downloading 0.4.0')
    expect(screen.getByRole('status')).toHaveTextContent(/installing agent-desktop/i)
    expect(within(card).getByRole('button', { name: /^install$/i })).toBeDisabled()
    expect(api.installAgentDesktop).toHaveBeenCalledTimes(1)
  })

  it('shows a failed job with its error and output', async () => {
    getStatus().mockResolvedValue(status({ job: { state: 'failed', log_tail: 'npm ERR! 403', error: 'exit 1' } }))
    await renderCard()
    const failed = await screen.findByTestId('agent-desktop-failed')
    expect(failed).toHaveTextContent('Install failed')
    expect(failed).toHaveTextContent('exit 1')
    expect(failed).toHaveTextContent('npm ERR! 403')
  })

  it('reports an install request that failed', async () => {
    getStatus().mockResolvedValue(status())
    vi.mocked(api.installAgentDesktop).mockRejectedValue(new Error('boom'))
    const card = await renderCard()
    fireEvent.click(within(card).getByRole('button', { name: /^install$/i }))
    expect(await screen.findByText('Could not start the install.')).toBeInTheDocument()
  })

  it('opens the permission guide and says so', async () => {
    getStatus().mockResolvedValue(status())
    vi.mocked(api.openAgentDesktopPermissionsGuide).mockResolvedValue({ ok: true })
    const card = await renderCard()
    fireEvent.click(within(card).getByRole('button', { name: /open permission guide/i }))
    expect(await screen.findByText('The permission guide opened on the host.')).toBeInTheDocument()
    expect(api.openAgentDesktopPermissionsGuide).toHaveBeenCalledTimes(1)
  })

  it('shows an error with Retry when the status cannot be read', async () => {
    getStatus().mockRejectedValue(new Error('down'))
    renderWithProviders(<ComputerUseInstallStatus platform="macos" />)
    const box = await screen.findByTestId('agent-desktop-status-error')
    expect(box).toHaveTextContent('Could not read the desktop driver status.')
    getStatus().mockResolvedValue(status())
    fireEvent.click(within(box).getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(screen.getByTestId('agent-desktop-status')).toBeInTheDocument())
  })
})
