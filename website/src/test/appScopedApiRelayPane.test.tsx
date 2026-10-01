/**
 * AppScopedApiProvider issues its scoped requests under the pane's capability
 * prefix when the dashboard runs as a relayed pane.
 *
 * This is the production boundary the Chromium proof could not reach: it mounts
 * the real provider, resolves the runtime to a relayed pane, and drives a real
 * `api.get`, so the asserted URL is the migrated call site's own output rather
 * than a helper recomputed in the test. Removing the relocation at
 * `scopedApi.ts`'s fetch makes the request fall back to the bare root path and
 * this test fails.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { AppScopedApiProvider, type AppApi } from '../app-sdk/scopedApi'
import { useAppApi } from '../app-sdk'
import { initDashboardRuntime } from '../lib/dashboardRuntime'

// Pin the runtime to a relayed pane before anything resolves the singleton.
// This file is a fresh module (vitest isolates per file), so the pane prefix is
// the only shape the runtime takes here.
initDashboardRuntime({ pathname: '/instance-pane/K_cap01/' })

const ACCOUNTS_PATH = '/api/apps/aws-control/accounts'
const PANE_ACCOUNTS = '/instance-pane/K_cap01/api/apps/aws-control/accounts'

function ApiProbe({ onApi }: { onApi: (api: AppApi) => void }) {
  onApi(useAppApi())
  return null
}

describe('AppScopedApiProvider under a relayed pane', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  const payload = [{ id: 'acct-1', name: 'zzq' }]

  beforeEach(() => {
    fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      text: async () => JSON.stringify(payload),
    }))
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('fetches a scoped GET under the capability prefix and returns the body', async () => {
    let api: AppApi | undefined
    render(
      <AppScopedApiProvider
        appName="aws-control"
        allowedApiPaths={['/api/apps/aws-control/*']}
        navigateFn={() => {}}
      >
        <ApiProbe onApi={a => { api = a }} />
      </AppScopedApiProvider>,
    )
    const result = await api!.get(ACCOUNTS_PATH)
    // A literal target, not a helper recomputed here: the scoped client relocated
    // its own permission-checked path under the pane prefix. Drop that relocation
    // and the call arrives at the bare root path, failing this assertion.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe(PANE_ACCOUNTS)
    expect(result).toEqual(payload)
  })
})
