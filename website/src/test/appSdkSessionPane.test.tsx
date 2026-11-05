/**
 * `useSessionPane()` — the App SDK hook a `contributes.sessionKinds` pane reads its
 * binding with.
 *
 * Driven through the REAL `AppHost` and the real SDK provider; only the bundle
 * import is stood in, by a module whose component reads the hook from the SDK
 * barrel. That is the same function an installed bundle reaches at runtime
 * (import map → `/vendor/kirocrew-app-sdk.mjs` → `window.__kirocrew_modules`),
 * which the last case pins.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, render, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { useSessionPane } from '../app-sdk'
import type { SessionPane } from '../app-sdk/sessionPane'

function Probe() {
  const pane = useSessionPane()
  return <pre data-testid="probe">{JSON.stringify(pane)}</pre>
}

vi.mock('../components/appBundleImport', () => ({
  importAppBundle: vi.fn(() => Promise.resolve({ default: Probe })),
}))

import AppHost, { type AppHostProps } from '../components/AppHost'

const app: AppHostProps['app'] = {
  name: 'project-apps', displayName: 'Project apps', version: '0.1.0', enabled: true,
  manifest: { version: '0.1.0', ui: { entry: 'index.mjs' }, permissions: { api: [] } },
}

const binding: SessionPane = {
  slotKey: 'chat-1-100', sessionKey: 'dashboard:chat-1-100',
  app: 'project-apps', kind: 'project', object: '0123456789abcdef0123456789abcdef',
}

afterEach(() => vi.clearAllMocks())

describe('useSessionPane', () => {
  it('returns the binding inside an AppHost mounted as a session pane', async () => {
    renderWithProviders(<AppHost app={app} entry="dist/pane.mjs" sessionKey={binding.sessionKey} sessionPane={binding} />)
    const probe = await screen.findByTestId('probe')
    expect(JSON.parse(probe.textContent || 'null')).toEqual(binding)
  })

  it('returns null inside an AppHost that is not a session pane', async () => {
    renderWithProviders(<AppHost app={app} sessionKey="dashboard:ui" />)
    expect((await screen.findByTestId('probe')).textContent).toBe('null')
  })

  it('returns null outside any host', () => {
    render(<Probe />)
    expect(screen.getByTestId('probe').textContent).toBe('null')
  })

  it('does not leak into a nested host that has no binding', async () => {
    // A pane that mounts another app surface must not hand that surface its own
    // session: the nested host publishes null.
    const { importAppBundle } = await import('../components/appBundleImport')
    function Outer() {
      return (
        <>
          <Probe />
          <AppHost app={{ ...app, name: 'inner' }} entry="inner.mjs" sessionKey="dashboard:ui" />
        </>
      )
    }
    vi.mocked(importAppBundle).mockImplementation(path =>
      Promise.resolve({ default: path.includes('/apps/inner/') ? Probe : Outer }))
    renderWithProviders(<AppHost app={app} entry="dist/pane.mjs" sessionKey={binding.sessionKey} sessionPane={binding} />)
    await waitFor(() => expect(screen.getAllByTestId('probe')).toHaveLength(2))
    const values = screen.getAllByTestId('probe').map(p => JSON.parse(p.textContent || 'null'))
    expect(values).toEqual([binding, null])
  })

  it('is the same function the host registers for app bundles to import', async () => {
    await import('../app-sdk/shared-modules')
    expect(window.__kirocrew_modules['@kirocrew/app-sdk'].useSessionPane).toBe(useSessionPane)
  })
})
