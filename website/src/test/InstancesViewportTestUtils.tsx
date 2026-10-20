/**
 * Helpers shared by the InstancesViewport suites (InstancesViewport.test.tsx and
 * InstancesViewport.paneLoad.test.tsx). Each suite installs its own vi.mock of
 * '../api/client', so `api` here is the mock of whichever suite is running.
 */
import { vi } from 'vitest'
import { api } from '../api/client'

// Explicit connected-instance mock for the readiness/watchdog tests —
// earlier tests override listInstances and clearAllMocks() does NOT restore
// implementations, so relying on the module-level default is order-dependent.
export const mockConnectedCd1 = () =>
  vi.mocked(api.listInstances).mockResolvedValue({
    instances: [
      {
        id: 'cd-1',
        name: 'Cloud One',
        ssh_host: 'cd-1-alias',
        remote_port: 7777,
        local_port: 7778,
        ttl: '20h',
        remote_bin: '',
        status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
      },
    ],
    warm_set_cap: 5,
  })
