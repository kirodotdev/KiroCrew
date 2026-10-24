import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

// Split out of RemoteCrewPanel.test.tsx, which sits at the 2,000-line cap: the
// rows for a connection method this build has no mapping for (#13337).

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    isAuthExpiredError: (e: unknown) =>
      e instanceof ApiError && (e as { authRequired?: boolean }).authRequired === true,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      updateInstance: vi.fn(),
      patchConfig: vi.fn(),
      cloudLaunches: vi.fn(),
      cloudPreflight: vi.fn(),
      cloudProvisioners: vi.fn(),
      cloudIamPolicy: vi.fn(),
      cloudLaunch: vi.fn(),
      cloudIdentity: vi.fn(),
      cloudLaunchStatus: vi.fn(),
      cloudLaunchCancel: vi.fn(),
      cloudLaunchSignin: vi.fn(),
      cloudStop: vi.fn(),
      cloudStart: vi.fn(),
      cloudDestroy: vi.fn(),
    },
  }
})

import { api } from '../api/client'

const MANUAL_INSTANCE = {
  id: 'm1',
  name: 'dev-box-1',
  connection_method: 'ssh' as const,
  ssm_target: '',
  ssh_host: 'dev-box-1',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'm1', state: 'disconnected' as const },
}

describe('RemoteCrewPanel: an unmapped connection method', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('explains an unmapped connection method on its badge instead of leaving it bare', async () => {
    // A row whose method this build has no copy for shows the raw method name and
    // no address. Without a hint nothing on the row says why; the hint names the
    // method and says this build cannot connect over it. The mapped SSH row beside
    // it keeps its own hint, so the fallback never replaces real copy.
    const unmapped = { ...MANUAL_INSTANCE, id: 'u1', name: 'future-crew', connection_method: 'outbound' }
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [MANUAL_INSTANCE, unmapped as unknown as typeof MANUAL_INSTANCE],
    })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    const row = (await screen.findByText('future-crew')).closest('[data-crew-id]') as HTMLElement
    const badge = within(row).getByText('outbound')
    expect(badge).toHaveAttribute('title', expect.stringMatching(/doesn't support the outbound connection method/))
    expect(badge).toHaveAttribute('aria-label', badge.getAttribute('title'))

    const sshRow = screen.getByText('dev-box-1', { selector: '.font-medium' }).closest('[data-crew-id]') as HTMLElement
    expect(within(sshRow).getByText('SSH')).toHaveAttribute('title', 'Connects over SSH.')
  })

  it('adds no inline hint and keeps Connect primary, like any disconnected crew', async () => {
    // First Principles items 5 and 7 on #13337: the badge's title explains the
    // method, and the gateway's refusal to Connect says the rest, so the row
    // carries no second copy of it. The address line still does not start with
    // an orphaned separator when there is no target.
    const unmapped = { ...MANUAL_INSTANCE, id: 'u1', name: 'future-crew', connection_method: 'outbound' }
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [MANUAL_INSTANCE, unmapped as unknown as typeof MANUAL_INSTANCE],
    })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    const row = (await screen.findByText('future-crew')).closest('[data-crew-id]') as HTMLElement
    expect(within(row).queryByText(/doesn't support the outbound connection method/)).toBeNull()
    expect(within(row).getByText(/^disconnected$/i)).toBeInTheDocument()
    const connect = within(row).getByRole('button', { name: /connect/i })
    expect(connect).toBeEnabled()
    expect(connect).not.toHaveAttribute('aria-describedby')
    expect(connect.className).toMatch(/bg-accent/)
    expect(row.textContent).not.toMatch(/outbound\s*·/)
    expect(within(row).getByText(/remote port 5476/)).toBeInTheDocument()
  })
})
