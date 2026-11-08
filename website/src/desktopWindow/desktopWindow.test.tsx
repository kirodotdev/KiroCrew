/**
 * The document a desktop-hosted app window loads.
 *
 * It mounts exactly the module the app's manifest declares for the window the
 * shell opened, and only with the shell's bridge present. Every other case
 * renders nothing and asks the shell to close the window, and an app list or a
 * bundle that fails closes it too: the shell reveals a window only on `ready()`,
 * so no fallback card is ever painted over the user's desktop. An app's SDK
 * navigation never moves the document URL the shell checks every message by.
 * A mounted window runs the dashboard's live socket, so an SDK chat panel in it
 * can send.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { renderWithProviders } from '../test/helpers'
import {
  declaredDesktopWindow,
  desktopWindowBridge,
  desktopWindowTarget,
  type DesktopWindowBridge,
} from './desktopWindowTarget'

const hosted = vi.hoisted(() => ({ props: [] as Record<string, unknown>[] }))
const apps = vi.hoisted(() => ({ rows: [] as unknown[], fail: null as Error | null }))
const socket = vi.hoisted(() => ({ mounts: 0 }))

/** Stands in for the app: renders its entry, and a button that runs its SDK `navigate('/chat')`. */
function HostedApp({ entry }: { entry: ReactNode }) {
  const navigate = useNavigate()
  return (
    <div data-testid="hosted-app">
      {entry}
      <button type="button" onClick={() => navigate('/chat')}>go</button>
    </div>
  )
}

vi.mock('../components/AppHost', () => ({
  default: (props: Record<string, unknown>) => {
    hosted.props.push(props)
    return <HostedApp entry={props.entry as ReactNode} />
  },
}))

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: () => {
    socket.mounts += 1
    return {}
  },
}))

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    api: { ...actual.api, listApps: () => (apps.fail ? Promise.reject(apps.fail) : Promise.resolve(apps.rows)) },
  }
})

import DesktopWindowRoot, { DesktopWindowPage } from './DesktopWindowRoot'

/** Chain: the page's `useQuery` -> mocked `api.listApps()` settles -> the effect or the mount it drives. */
const APP_LIST_TIMEOUT_MS = 5_000

function bridge(over: Partial<DesktopWindowBridge> = {}): DesktopWindowBridge {
  return {
    level: 1,
    app: 'pets',
    window: 'pet',
    kind: 'overlay',
    ready: vi.fn(),
    close: vi.fn(),
    open: vi.fn(),
    setHitRegions: vi.fn(),
    setFocusable: vi.fn(),
    onShortcut: vi.fn(() => () => {}),
    ...over,
  }
}

const DESKTOP = { windows: [{ id: 'pet', entry: 'desktop/pet.mjs', kind: 'overlay' }] }

function row(over: Record<string, unknown> = {}) {
  return { name: 'pets', version: '1.0.0', enabled: true, manifest: { name: 'pets', desktop: DESKTOP }, ...over }
}

describe('desktopWindowTarget', () => {
  it('reads the app and window from the hosted document path only', () => {
    expect(desktopWindowTarget('/popout/desktop/pets/pet')).toEqual({ app: 'pets', window: 'pet' })
    for (const path of [
      '/popout/desktop/pets',
      '/popout/desktop/pets/pet/extra',
      '/popout/desktop/Pets/pet',
      '/popout/desktop/pets/../x',
      '/popout/desktop/pets/pet.html',
      '/popout/app/pets',
      '/chat',
    ]) {
      expect(desktopWindowTarget(path)).toBeNull()
    }
  })
})

describe('desktopWindowBridge', () => {
  const target = { app: 'pets', window: 'pet' }

  it('accepts the shell bridge describing this window', () => {
    const b = bridge()
    expect(desktopWindowBridge(target, { kirocrewDesktopWindow: b })).toBe(b)
  })

  it('refuses a missing, foreign, partial or level-0 bridge', () => {
    expect(desktopWindowBridge(target, {})).toBeNull()
    expect(desktopWindowBridge(target, { kirocrewDesktopWindow: bridge({ app: 'other' }) })).toBeNull()
    expect(desktopWindowBridge(target, { kirocrewDesktopWindow: bridge({ window: 'panel' }) })).toBeNull()
    expect(desktopWindowBridge(target, { kirocrewDesktopWindow: bridge({ level: 0 }) })).toBeNull()
    const partial = { ...bridge(), ready: undefined }
    expect(desktopWindowBridge(target, { kirocrewDesktopWindow: partial })).toBeNull()
  })
})

describe('declaredDesktopWindow', () => {
  it('finds a declared window by id and nothing else', () => {
    expect(declaredDesktopWindow({ desktop: DESKTOP }, 'pet')).toEqual({ id: 'pet', entry: 'desktop/pet.mjs', kind: 'overlay' })
    expect(declaredDesktopWindow({ desktop: DESKTOP }, 'panel')).toBeNull()
    expect(declaredDesktopWindow({ desktop: { windows: 'x' } }, 'pet')).toBeNull()
    expect(declaredDesktopWindow(null, 'pet')).toBeNull()
  })
})

describe('DesktopWindowPage', () => {
  beforeEach(() => {
    hosted.props = []
    apps.fail = null
    socket.mounts = 0
  })

  it("mounts the declared window's entry and reports a failure as a close", async () => {
    apps.rows = [row()]
    const b = bridge()
    const { findByTestId } = renderWithProviders(<DesktopWindowPage target={{ app: 'pets', window: 'pet' }} bridge={b} />)
    expect((await findByTestId('hosted-app', {}, { timeout: APP_LIST_TIMEOUT_MS })).textContent).toBe('desktop/pet.mjsgo')
    const props = hosted.props.at(-1)!
    expect((props.app as { name: string }).name).toBe('pets')
    expect(b.close).not.toHaveBeenCalled()
    ;(props.onFailure as (e: unknown) => void)(new Error('boom'))
    expect(b.close).toHaveBeenCalledTimes(1)
    expect(document.body.style.background).toBe('transparent')
    expect(socket.mounts).toBeGreaterThan(0)
  })

  it.each([
    ['a window the app does not declare', [row()], 'panel'],
    ['a disabled app', [row({ enabled: false })], 'pet'],
    ['an app skipped at boot', [row({ unavailable: { reason: 'x' } })], 'pet'],
    ['an app that is not installed', [], 'pet'],
  ])('renders nothing and closes for %s', async (_label, rows, windowId) => {
    apps.rows = rows
    const b = bridge({ window: windowId })
    const { queryByTestId } = renderWithProviders(<DesktopWindowPage target={{ app: 'pets', window: windowId }} bridge={b} />)
    await waitFor(() => expect(b.close).toHaveBeenCalled(), { timeout: APP_LIST_TIMEOUT_MS })
    expect(queryByTestId('hosted-app')).toBeNull()
    expect(hosted.props).toEqual([])
    expect(socket.mounts).toBe(0)
  })

  it('closes the window when the app list cannot be read', async () => {
    apps.fail = new Error('HTTP 500')
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const b = bridge()
    const { queryByTestId } = renderWithProviders(<DesktopWindowPage target={{ app: 'pets', window: 'pet' }} bridge={b} />)
    await waitFor(() => expect(b.close).toHaveBeenCalledTimes(1), { timeout: APP_LIST_TIMEOUT_MS })
    expect(queryByTestId('hosted-app')).toBeNull()
    expect(String(errorSpy.mock.calls[0]?.[0])).toContain('[DesktopWindow] pets/pet')
    errorSpy.mockRestore()
  })
})

describe('DesktopWindowRoot', () => {
  const PATH = '/popout/desktop/pets/pet'
  let prevPath: string

  beforeEach(() => {
    hosted.props = []
    apps.fail = null
    apps.rows = [row()]
    prevPath = window.location.pathname
    window.history.replaceState(null, '', PATH)
  })

  afterEach(() => {
    window.history.replaceState(null, '', prevPath)
    delete (window as { kirocrewDesktopWindow?: unknown }).kirocrewDesktopWindow
  })

  it("keeps the document on the window's own path when the app navigates", async () => {
    const b = bridge()
    ;(window as { kirocrewDesktopWindow?: unknown }).kirocrewDesktopWindow = b
    const { findByRole } = render(<DesktopWindowRoot target={{ app: 'pets', window: 'pet' }} />)
    fireEvent.click(await findByRole('button', { name: 'go' }, { timeout: APP_LIST_TIMEOUT_MS }))
    expect(window.location.pathname).toBe(PATH)
    expect(b.close).not.toHaveBeenCalled()
  })
})
