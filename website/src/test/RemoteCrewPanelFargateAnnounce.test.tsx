import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'

// Its own file, not a case inside RemoteCrewPanel.test.tsx, because `vi.mock` is
// hoisted file-wide: mocking the announce module there would silently replace it
// for every other case in that suite.
vi.mock('../lib/chainAnnounce', () => ({
  announceChainedCrew: vi.fn(() => true),
  clearChainRefusal: vi.fn(),
  readChainRefusal: vi.fn(() => null),
  subscribeChainRefusal: vi.fn(() => () => {}),
}))

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
import { announceChainedCrew } from '../lib/chainAnnounce'

const BASE = {
  id: 'x1',
  name: 'crew-x',
  connection_method: 'ssm' as const,
  ssm_target: 'i-0abc123456789def0',
  ssh_host: '',
  aws_profile: '',
  aws_region: 'us-west-2',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'x1', state: 'disconnected' as const },
}

/** A disconnected row of the given transport, so its primary action is Connect. */
function row(over: Record<string, unknown>) {
  return { ...BASE, ...over }
}

/**
 * Press the row's Connect, on the tab the row lives on.
 *
 * A Fargate row is a MATE -- one agent, no dashboard, no roster -- so it lists under
 * Remote mates; every other row is a gateway and stays under Remote crews.
 */
async function clickConnect(tab: 'crews' | 'mates' = 'crews') {
  const u = userEvent.setup()
  if (tab === 'mates') {
    await u.click(await screen.findByRole('button', { name: /Remote mates/i }))
  }
  await u.click(await screen.findByRole('button', { name: /^\s*Connect\s*$/i }))
}

describe('announcing a crew connected inside a pane', () => {
  beforeEach(() => {
    vi.mocked(announceChainedCrew).mockClear()
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  })

  it('does not announce a fargate crew, which has no dashboard for the host to open', async () => {
    // A fargate connect returns a `local_port` like any other, so a gate that reads
    // only that port announced it -- and the host persists the chained row BEFORE it
    // tries to connect it, leaving a crew in its registry that promises a tab the
    // announced port cannot serve. The only recovery was the user pressing Remove.
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [
        row({
          id: 'f1',
          name: 'fargate-crew',
          connection_method: 'fargate' as const,
          ssm_target: 'ecs:crew_0123456789abcdef0123456789abcdef_0123456789abcdef0123456789abcdef-0123456789',
          remote_port: 8080,
        }),
      ],
    })
    vi.mocked(api.connectInstance).mockResolvedValue({
      instance_id: 'f1',
      state: 'connected',
      local_port: 7790,
      turn_url: 'http://127.0.0.1:7790/v1/chat/completions',
    })

    renderWithProviders(<RemoteCrewPanel />)
    await clickConnect('mates')

    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('f1'))
    expect(announceChainedCrew).not.toHaveBeenCalled()
  })

  it('still announces an ssm crew, which does have a dashboard', async () => {
    // The control. Without it, a gate that announced nothing at all would pass the
    // case above and read as the fix.
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [row({ id: 's1', name: 'ssm-crew' })],
    })
    vi.mocked(api.connectInstance).mockResolvedValue({
      instance_id: 's1',
      state: 'connected',
      local_port: 7791,
    })

    renderWithProviders(<RemoteCrewPanel />)
    await clickConnect()

    await waitFor(() => expect(announceChainedCrew).toHaveBeenCalledTimes(1))
    expect(vi.mocked(announceChainedCrew).mock.calls[0][0]).toMatchObject({
      id: 's1',
      port: 7791,
    })
  })
})
