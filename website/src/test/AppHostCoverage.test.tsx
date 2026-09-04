/**
 * AppHost — the whole file, which no other suite touches.
 *
 * Four surfaces sit in front of the real host and each one is a different
 * answer to "why is there no app here": no record at all, a record that is
 * switched off, a record that ships agents but no UI, and the host itself.
 * Behind them are the pieces a hosted app actually talks to — the permission
 * lists handed to `AppApiProvider`, the CustomEvent bridge that stands in for a
 * WebSocket subscription, the notify/navigate escapes, the dev-mode full-page
 * reload, the Suspense skeleton, the bundle-load failure card and the error
 * boundary with its retry.
 *
 * HARNESS. `../app-sdk` is replaced by a provider that records the props it was
 * handed and renders its children. That is the only seam through which the four
 * callbacks AppHost builds (`subscribeFn` / `navigateFn` / `notifyFn`) can be
 * invoked at all: they are passed down, never called by AppHost itself, and the
 * component that would call them is a real ESM bundle fetched over HTTP. The
 * same stand-in doubles as the crash source for the error boundary — throwing
 * from it is indistinguishable, from the boundary's point of view, from an app
 * that throws on its first render.
 *
 * The dynamic `import('/apps/<name>/ui/<entry>')` is left REAL and left to
 * fail: there is no `apps/` directory under the Vite root, so the import
 * rejects, AppHost's own `.catch` turns it into the "Failed to load" card, and
 * the console.error it logs on the way is what pins the bundle path.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { renderWithProviders } from './helpers'
import type { AppHostProps } from '../components/AppHost'

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

/** Recorded `AppApiProvider` props + a one-shot render-throw switch. */
const sdk = vi.hoisted(() => ({
  props: null as null | Record<string, unknown>,
  throwNext: false,
}))

vi.mock('../app-sdk', () => ({
  AppApiProvider: (props: Record<string, unknown>) => {
    sdk.props = props
    if (sdk.throwNext) throw new Error('the app blew up during render')
    return <div data-testid="sdk-provider">{props.children as ReactNode}</div>
  },
}))

import AppHost from '../components/AppHost'

type AppRecord = AppHostProps['app']

interface HostedProps {
  appName: string
  appVersion?: string
  allowedApiPaths: string[]
  allowedEvents: string[]
  subscribeFn: (event: string, cb: (data: unknown) => void) => () => void
  navigateFn: (path: string) => void
  notifyFn: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

/** The props AppHost handed the SDK provider on its most recent render. */
function hosted(): HostedProps {
  if (!sdk.props) throw new Error('AppApiProvider was never rendered')
  return sdk.props as unknown as HostedProps
}

/**
 * A third-party app id on purpose: `ledger-lens` is absent from
 * `APP_MANIFEST_KEY`, so `appDisplayName` / `appDescription` fall through to
 * the fixture's own strings instead of a localised first-party catalog entry.
 */
const NAME = 'ledger-lens'

function appRecord(over: Partial<AppRecord> = {}): AppRecord {
  return {
    name: NAME,
    displayName: 'Ledger Lens',
    version: '0.9.0',
    enabled: true,
    manifest: {
      version: '1.2.3',
      description: 'Reads your books and explains them.',
      ui: { entry: 'index.js' },
      permissions: { api: ['/api/apps'], events: ['ledger:updated'] },
    },
    ...over,
  }
}

function renderHost(app: AppRecord) {
  return renderWithProviders(<AppHost app={app} />)
}

/** Let React.lazy settle: the import rejects, then the catch's module renders. */
async function settleBundle() {
  await waitFor(() => expect(screen.getByRole('heading', { level: 3 })).toBeTruthy())
}

let reloadSpy: ReturnType<typeof vi.fn>
let errorSpy: ReturnType<typeof vi.spyOn>
const origReload = window.location.reload

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  mockNavigate.mockClear()
  sdk.props = null
  sdk.throwNext = false
  reloadSpy = vi.fn()
  Object.defineProperty(window.location, 'reload', { configurable: true, value: reloadSpy })
  // AppHost logs bundle-load failures and the boundary logs app crashes; both
  // are intentional diagnostics, and one of them is an assertion target below.
  errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  errorSpy.mockRestore()
  Object.defineProperty(window.location, 'reload', { configurable: true, value: origReload })
})

describe('AppHost — guard surfaces', () => {
  it('offers a way back to Apps when there is no app record at all', () => {
    renderHost(undefined as unknown as AppRecord)

    expect(screen.getByTestId('page-title').textContent).toBe('App Not Found')
    expect(screen.getByTestId('page-subtitle').textContent).toBe('"unknown" is not installed')
    expect(screen.getByText('Install it from Apps or via CLI')).toBeTruthy()
    expect(screen.queryByTestId('sdk-provider')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /apps/i }))
    expect(mockNavigate).toHaveBeenCalledWith('/apps')
  })

  it('explains a disabled app instead of loading its bundle', () => {
    renderHost(appRecord({ enabled: false }))

    expect(screen.getByTestId('page-title').textContent).toBe('Ledger Lens')
    expect(screen.getByTestId('page-subtitle').textContent).toBe('This app is disabled')
    expect(screen.getByText('Enable this app from Apps to use it.')).toBeTruthy()
    expect(screen.queryByTestId('sdk-provider')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /apps/i }))
    expect(mockNavigate).toHaveBeenCalledWith('/apps')
  })

  it('treats a missing ui.entry as an agent-only app, not an error', () => {
    renderHost(appRecord({
      manifest: { version: '1.2.3', description: 'Reads your books and explains them.' },
    }))

    expect(screen.getByTestId('page-title').textContent).toBe('Ledger Lens')
    expect(screen.getByTestId('page-subtitle').textContent).toBe('Reads your books and explains them.')
    expect(screen.getByRole('heading', { level: 3 }).textContent).toBe('Agent-only app')
    expect(screen.queryByTestId('sdk-provider')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /apps/i }))
    expect(mockNavigate).toHaveBeenCalledWith('/apps')
  })

  it('falls through to the agent-only surface when the manifest itself is absent', () => {
    renderHost({ name: NAME, enabled: true })

    // No manifest means no localised copy and no description: the id is all
    // there is, and `appDescription` yields an empty string, which PageHeader
    // then omits rather than rendering a blank subtitle row.
    expect(screen.getByTestId('page-title').textContent).toBe(NAME)
    expect(screen.queryByTestId('page-subtitle')).toBeNull()
  })
})

describe('AppHost — hosting a real bundle', () => {
  it('shows a named loading skeleton while the bundle is in flight', async () => {
    renderHost(appRecord())

    const line = screen.getByText(
      (_content, el) => el?.tagName === 'P' && (el.textContent ?? '').startsWith('Loading'),
    )
    expect(line.textContent).toContain('Ledger Lens')

    await settleBundle()
  })

  it('renders a failure card, naming the bundle path, when the import rejects', async () => {
    renderHost(appRecord())
    await settleBundle()

    expect(screen.getByRole('heading', { level: 3 }).textContent).toBe('Failed to load Ledger Lens')
    // No PageHeader on this card — it replaces the app, not the whole page.
    expect(screen.queryByTestId('page-title')).toBeNull()

    const logged = errorSpy.mock.calls.map(c => String(c[0])).join('\n')
    expect(logged).toContain(`[AppHost] Failed to load ${NAME} from /apps/${NAME}/ui/index.js:`)

    fireEvent.click(screen.getByRole('button', { name: /apps/i }))
    expect(mockNavigate).toHaveBeenCalledWith('/apps')
  })

  it('passes the manifest permission lists through to the SDK provider', async () => {
    renderHost(appRecord())
    await settleBundle()

    expect(hosted().appName).toBe(NAME)
    expect(hosted().allowedApiPaths).toEqual(['/api/apps'])
    expect(hosted().allowedEvents).toEqual(['ledger:updated'])
  })

  it('scopes an app that declares no permissions to nothing at all', async () => {
    renderHost(appRecord({
      manifest: { version: '1.2.3', ui: { entry: 'index.js' } },
    }))
    await settleBundle()

    expect(hosted().allowedApiPaths).toEqual([])
    expect(hosted().allowedEvents).toEqual([])
  })

  it('prefers the manifest version and falls back to the installed record', async () => {
    const { unmount } = renderHost(appRecord())
    await settleBundle()
    expect(hosted().appVersion).toBe('1.2.3')
    unmount()

    renderHost(appRecord({ manifest: { ui: { entry: 'index.js' } } }))
    await settleBundle()
    expect(hosted().appVersion).toBe('0.9.0')
  })
})

describe('AppHost — the bridges handed to a hosted app', () => {
  it('bridges host CustomEvents into an app subscription and unsubscribes on demand', async () => {
    renderHost(appRecord())
    await settleBundle()

    const cb = vi.fn()
    const off = hosted().subscribeFn('ledger:updated', cb)

    act(() => {
      window.dispatchEvent(new CustomEvent('mc:app:ledger:updated', { detail: { total: 42 } }))
    })
    expect(cb).toHaveBeenCalledWith({ total: 42 })

    off()
    act(() => {
      window.dispatchEvent(new CustomEvent('mc:app:ledger:updated', { detail: { total: 43 } }))
    })
    expect(cb).toHaveBeenCalledTimes(1)
  })

  it('routes an app navigation request through the host router', async () => {
    renderHost(appRecord())
    await settleBundle()

    hosted().navigateFn('/apps/ledger-lens')
    expect(mockNavigate).toHaveBeenCalledWith('/apps/ledger-lens')
  })

  it('turns an app notification into an mc:notify event, with and without a type', async () => {
    renderHost(appRecord())
    await settleBundle()

    const seen: unknown[] = []
    const listener = (e: Event) => seen.push((e as CustomEvent).detail)
    window.addEventListener('mc:notify', listener)
    try {
      hosted().notifyFn('books balanced', { type: 'success' })
      hosted().notifyFn('just so you know')
    } finally {
      window.removeEventListener('mc:notify', listener)
    }

    expect(seen).toEqual([
      { message: 'books balanced', type: 'success' },
      { message: 'just so you know' },
    ])
  })
})

describe('AppHost — dev-mode live reload', () => {
  it('reloads only for its own app, and only while mounted', async () => {
    const { unmount } = renderHost(appRecord())
    await settleBundle()

    act(() => {
      window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: { app: 'other-app' } }))
    })
    expect(reloadSpy).not.toHaveBeenCalled()

    // No detail at all — the broadcast shape a stray dispatcher would send.
    act(() => { window.dispatchEvent(new CustomEvent('mc:app-reload')) })
    expect(reloadSpy).not.toHaveBeenCalled()

    act(() => {
      window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: { app: NAME } }))
    })
    expect(reloadSpy).toHaveBeenCalledTimes(1)

    unmount()
    act(() => {
      window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: { app: NAME } }))
    })
    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })
})

describe('AppHost — error boundary', () => {
  it('catches an app crash, reports it, and remounts the app on retry', async () => {
    sdk.throwNext = true
    renderHost(appRecord())

    expect(screen.getByTestId('page-title').textContent).toBe(NAME)
    expect(screen.getByTestId('page-subtitle').textContent).toBe('App crashed')
    expect(screen.getByRole('heading', { level: 3 }).textContent).toBe(`${NAME} encountered an error`)
    expect(screen.getByText('the app blew up during render')).toBeTruthy()

    const crashLog = errorSpy.mock.calls.map(c => String(c[0])).join('\n')
    expect(crashLog).toContain(`[AppHost] ${NAME} crashed:`)

    // Retry clears the boundary AND bumps the reset key, so the app is given a
    // genuinely fresh lazy import rather than the cached rejected one.
    sdk.throwNext = false
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))

    await settleBundle()
    expect(screen.queryByTestId('page-subtitle')).toBeNull()
    expect(screen.getByRole('heading', { level: 3 }).textContent).toBe('Failed to load Ledger Lens')
  })

  it('offers Apps as the other way out of a crashed app', async () => {
    sdk.throwNext = true
    renderHost(appRecord())

    fireEvent.click(screen.getByRole('button', { name: /apps/i }))
    expect(mockNavigate).toHaveBeenCalledWith('/apps')

    // The crash fallback stays put: navigation is the host's business, and the
    // boundary has nothing to re-render into.
    expect(screen.getByTestId('page-subtitle').textContent).toBe('App crashed')
    await act(async () => { await Promise.resolve() })
  })
})
