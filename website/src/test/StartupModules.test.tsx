/**
 * StartupModules: which apps' `ui.startup` components mount, and that a dropped
 * admission unmounts one (running its cleanup) without a reload.
 *
 * Seams: the installed-app list (`useInstalledApps`), the bundle import, and the
 * safe-reload / embedded-pane probes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
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
  crashing: '',
  importFailure: '',
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
    if (path === state.importFailure) return Promise.reject(new Error('startup import failed'))
    const name = path.split('/')[2]
    function Startup() {
      if (path === state.crashing) throw new Error('startup module crashed')
      useEffect(() => () => { state.cleanups.push(name) }, [])
      return <span data-testid={`startup-${name}`} />
    }
    return Promise.resolve({ default: Startup })
  },
}))

import StartupModules from '../components/StartupModules'
import {
  NavigationLeaveGuardProvider,
  useRegisterNavigationLeaveGuard,
} from '../components/NavigationLeaveGuard'
import { useLocation } from 'react-router-dom'

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
  state.crashing = ''
  state.importFailure = ''
  state.safe = false
  state.embedded = false
})

afterEach(() => { vi.restoreAllMocks() })

// Wait for AppHost's lazy import and error boundary to report the failure.
const STARTUP_READY = { timeout: 5000 }

describe('StartupModules', () => {
  it.each(['throw', 'import'] as const)('shows an app-named minor notice on %s failure and keeps the console log', async (failure) => {
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    const path = '/apps/ghost/ui/startup.mjs'
    if (failure === 'throw') state.crashing = path
    else state.importFailure = path
    state.apps = [app('ghost', {})]
    renderWithProviders(<StartupModules />)

    const notice = await screen.findByTestId('startup-module-error-ghost', undefined, STARTUP_READY)
    expect(notice).toBeVisible()
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent('ghost failed to start.')
    expect(within(notice).getByRole('link', { name: 'Open ghost' })).toHaveAttribute('href', '/apps/detail/ghost')
    expect(errorLog).toHaveBeenCalledWith(
      '[StartupModules] ghost startup module failed:',
      expect.objectContaining({ message: failure === 'throw' ? 'startup module crashed' : 'startup import failed' }),
    )
    expect(screen.queryByTestId('startup-ghost')).not.toBeInTheDocument()
  })

  it('dismisses a failed app notice without showing it again on a later failure', async () => {
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    state.importFailure = '/apps/ghost/ui/startup.mjs'
    state.apps = [app('ghost', {})]
    const view = renderWithProviders(<StartupModules />)
    const notice = await screen.findByTestId('startup-module-error-ghost', undefined, STARTUP_READY)

    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss startup notice for ghost until reload' }))
    expect(screen.queryByTestId('startup-module-error-ghost')).not.toBeInTheDocument()
    expect(screen.queryByTestId('startup-module-errors')).not.toBeInTheDocument()

    errorLog.mockClear()
    state.importFailure = '/apps/ghost/ui/other.mjs'
    state.apps = [app('ghost', { entry: 'other.mjs' })]
    view.rerender(<StartupModules />)
    await waitFor(() => expect(errorLog).toHaveBeenCalledWith(
      '[StartupModules] ghost startup module failed:', expect.any(Error),
    ), STARTUP_READY)
    expect(screen.queryByTestId('startup-module-error-ghost')).not.toBeInTheDocument()
  })

  it('deduplicates repeated failures by app name without dropping another app notice', async () => {
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {})
    state.crashing = '/apps/ghost/ui/startup.mjs'
    state.apps = [app('ghost', {})]
    const view = renderWithProviders(<StartupModules />)
    await screen.findByTestId('startup-module-error-ghost', undefined, STARTUP_READY)

    errorLog.mockClear()
    state.crashing = '/apps/ghost/ui/other.mjs'
    state.importFailure = '/apps/owl/ui/startup.mjs'
    state.apps = [app('ghost', { entry: 'other.mjs' }, { version: '2.0.0' }), app('owl', {})]
    view.rerender(<StartupModules />)
    await waitFor(() => expect(errorLog).toHaveBeenCalledWith(
      '[StartupModules] ghost startup module failed:', expect.any(Error),
    ), STARTUP_READY)
    const owlNotice = await screen.findByTestId('startup-module-error-owl', undefined, STARTUP_READY)
    expect(screen.getAllByTestId('startup-module-error-ghost')).toHaveLength(1)
    expect(owlNotice).toHaveTextContent('owl failed to start.')
    expect(within(owlNotice).getByRole('link', { name: 'Open owl' })).toHaveAttribute('href', '/apps/detail/owl')
    expect(within(screen.getByTestId('startup-module-errors')).getAllByRole('status')).toHaveLength(2)
  })

  it('renders no notice or notice region when startup modules mount cleanly', async () => {
    state.apps = [app('ghost', {}), app('owl', {})]
    const view = renderWithProviders(<StartupModules />)
    await screen.findByTestId('startup-ghost', undefined, STARTUP_READY)
    await screen.findByTestId('startup-owl', undefined, STARTUP_READY)
    expect(state.imports).toEqual(['/apps/ghost/ui/startup.mjs', '/apps/owl/ui/startup.mjs'])
    expect(screen.queryAllByTestId(/^startup-module-error-/)).toEqual([])
    expect(screen.queryByTestId('startup-module-errors')).not.toBeInTheDocument()
    expect(view.container.textContent).toBe('')
  })

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

  it('imports an admitted graph under its approval digest', async () => {
    const digest = 'a'.repeat(64)
    state.apps = [app('ghost', { digest })]
    renderWithProviders(<StartupModules />)
    await settle()
    expect(state.imports).toEqual([`/apps/ghost/ui/startup:${digest}/startup.mjs`])
    expect(screen.getByTestId('startup-ghost')).toBeInTheDocument()
  })

  it('remounts on a different approval at the same name and version', async () => {
    const oldDigest = 'a'.repeat(64)
    const newDigest = 'b'.repeat(64)
    state.apps = [app('ghost', { digest: oldDigest })]
    const view = renderWithProviders(<StartupModules />)
    await settle()
    expect(screen.getByTestId('startup-ghost')).toBeInTheDocument()

    state.apps = [app('ghost', { digest: newDigest })]
    view.rerender(<StartupModules />)
    await settle()
    expect(state.imports).toEqual([
      `/apps/ghost/ui/startup:${oldDigest}/startup.mjs`,
      `/apps/ghost/ui/startup:${newDigest}/startup.mjs`,
    ])
    expect(state.cleanups).toEqual(['ghost'])
    expect(screen.getByTestId('startup-ghost')).toBeInTheDocument()
  })

  it('mounts a re-approved entry after the earlier entry crashed, same tree digest', async () => {
    // Re-pointing ui.startup at another file in an unchanged ui/ tree re-approves at
    // the same digest. Keying on the digest alone keeps the crashed AppErrorBoundary,
    // so the new entry never mounts until a reload; the entry must be in the key too.
    const digest = 'a'.repeat(64)
    state.crashing = `/apps/ghost/ui/startup:${digest}/startup.mjs`
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      state.apps = [app('ghost', { digest, entry: 'startup.mjs' })]
      const view = renderWithProviders(<StartupModules />)
      await settle()
      expect(screen.queryByTestId('startup-ghost')).toBeNull()

      state.apps = [app('ghost', { digest, entry: 'other.mjs' })]
      view.rerender(<StartupModules />)
      await settle()
      expect(screen.getByTestId('startup-ghost')).toBeInTheDocument()
    } finally {
      quiet.mockRestore()
    }
  })

  it('mounts a re-approved graph after the earlier approval crashed', async () => {
    const oldDigest = 'a'.repeat(64)
    const newDigest = 'b'.repeat(64)
    state.crashing = `/apps/ghost/ui/startup:${oldDigest}/startup.mjs`
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      state.apps = [app('ghost', { digest: oldDigest })]
      const view = renderWithProviders(<StartupModules />)
      await settle()
      expect(screen.queryByTestId('startup-ghost')).toBeNull()

      // Only a host keyed on the digest leaves the crashed host's error boundary behind.
      state.apps = [app('ghost', { digest: newDigest })]
      view.rerender(<StartupModules />)
      await settle()
      expect(screen.getByTestId('startup-ghost')).toBeInTheDocument()
    } finally {
      quiet.mockRestore()
    }
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

  it('asks the leave guard before the failed-app notice link navigates away', async () => {
    // A startup module fails while the page holds an unsaved draft: clicking the
    // notice's "Open ghost" link unmounts that page, so a plain click must ask
    // the leave guard, not navigate unconditionally (would lose the draft).
    vi.spyOn(console, 'error').mockImplementation(() => {})
    state.importFailure = '/apps/ghost/ui/startup.mjs'
    state.apps = [app('ghost', {})]
    const guard = vi.fn(() => false) // the open draft vetoes leaving
    function Guarded({ children }: { children: ReactNode }) {
      useRegisterNavigationLeaveGuard(guard)
      return <>{children}</>
    }
    function Where() {
      return <span data-testid="where">{useLocation().pathname}</span>
    }
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <Guarded>
          <StartupModules />
          <Where />
        </Guarded>
      </NavigationLeaveGuardProvider>,
      { route: '/chat' },
    )
    const notice = await screen.findByTestId('startup-module-error-ghost', undefined, STARTUP_READY)
    fireEvent.click(within(notice).getByRole('link', { name: 'Open ghost' }))
    expect(guard).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('where').textContent).toBe('/chat') // did not navigate
  })

  it('does not reload, and still mounts a re-approved module, after an admitted startup app is replaced', async () => {
    // The startup AppHost is mounted on every route; a full-page reload on an
    // app-replaced broadcast would reload every open tab. The mount suppresses
    // the reload AND must still import a re-approved graph afterwards (its
    // digest-pinned URL is fresh), rather than stranding it behind the
    // replaced-bundle notice with no reload to clear it.
    const reload = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...original, reload },
    })
    try {
      const oldDigest = 'a'.repeat(64)
      const newDigest = 'b'.repeat(64)
      state.apps = [app('ghost', { digest: oldDigest })]
      const view = renderWithProviders(<StartupModules />)
      await screen.findByTestId('startup-ghost', undefined, STARTUP_READY)
      // The gateway replaces the app's files and broadcasts the reload event.
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: { app: 'ghost' } }))
      })
      await settle()
      expect(reload).not.toHaveBeenCalled()
      // The owner re-approves: a new digest -> new key -> fresh host. It must
      // import the new graph, not show ReplacedBundleNotice.
      state.apps = [app('ghost', { digest: newDigest })]
      view.rerender(<StartupModules />)
      await settle()
      expect(reload).not.toHaveBeenCalled()
      expect(state.imports).toContain(`/apps/ghost/ui/startup:${newDigest}/startup.mjs`)
      expect(screen.getByTestId('startup-ghost')).toBeInTheDocument()
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })
})
