/**
 * StartupModules: which apps' `ui.startup` components mount, and that a dropped
 * admission unmounts one (running its cleanup) without a reload.
 *
 * Seams: the installed-app list (`useInstalledApps`), the bundle import, and the
 * safe-reload / embedded-pane probes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { useEffect, type ReactNode } from 'react'
import { renderWithProviders } from './helpers'
import type { InstalledApp } from '../components/appstore/types'

vi.mock('../app-sdk', () => ({
  AppApiProvider: (props: { children: ReactNode }) => <>{props.children}</>,
}))

const state = vi.hoisted(() => ({
  apps: [] as Partial<InstalledApp>[],
  imports: [] as string[],
  cleanups: [] as string[],
  safe: false,
  embedded: false,
}))

vi.mock('../hooks/panelTabRegistry', () => ({
  useInstalledApps: () => ({ apps: state.apps }),
}))
vi.mock('../lib/safeReload', () => ({ isSafeReload: () => state.safe }))
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: () => state.embedded }))
vi.mock('../components/appBundleImport', () => ({
  importAppBundle: (path: string) => {
    state.imports.push(path)
    const name = path.split('/')[2]
    function Startup() {
      useEffect(() => () => { state.cleanups.push(name) }, [])
      return <span data-testid={`startup-${name}`} />
    }
    return Promise.resolve({ default: Startup })
  },
}))

import StartupModules from '../components/StartupModules'

type Startup = NonNullable<InstalledApp['startup']>

function app(name: string, startup: Partial<Startup> | undefined, extra: Partial<InstalledApp> = {}) {
  return {
    name,
    version: '1.0.0',
    enabled: true,
    manifest: { ui: { entry: 'page.mjs' } },
    startup: startup && { entry: 'startup.mjs', state: 'admitted', reason: '', ...startup },
    ...extra,
  } as Partial<InstalledApp>
}

async function settle() {
  await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve() })
}

beforeEach(() => {
  state.apps = []
  state.imports = []
  state.cleanups = []
  state.safe = false
  state.embedded = false
})

describe('StartupModules', () => {
  it('mounts only admitted, enabled, available apps, from their startup entry', async () => {
    state.apps = [
      app('ghost', {}),
      app('pending', { state: 'needs-approval' }),
      app('refused', { state: 'denied' }),
      app('off', {}, { enabled: false }),
      app('broken', {}, { unavailable: { reason: 'ImportError: x' } }),
      app('plain', undefined),
    ]
    renderWithProviders(<StartupModules />)
    await settle()
    expect(state.imports).toEqual(['/apps/ghost/ui/startup.mjs'])
    expect(screen.getByTestId('startup-ghost')).toBeTruthy()
  })

  it('unmounts a component, running its cleanup, when admission is withdrawn', async () => {
    state.apps = [app('ghost', {})]
    const view = renderWithProviders(<StartupModules />)
    await settle()
    expect(screen.getByTestId('startup-ghost')).toBeTruthy()

    state.apps = [app('ghost', { state: 'denied', reason: 'the app is disabled' })]
    view.rerender(<StartupModules />)
    await settle()
    expect(screen.queryByTestId('startup-ghost')).toBeNull()
    expect(state.cleanups).toEqual(['ghost'])
  })

  it.each([
    ['a safe reload', () => { state.safe = true }],
    ['an embedded instance pane', () => { state.embedded = true }],
  ])('mounts nothing on %s', async (_label, arrange) => {
    arrange()
    state.apps = [app('ghost', {})]
    renderWithProviders(<StartupModules />)
    await settle()
    expect(state.imports).toEqual([])
  })
})
