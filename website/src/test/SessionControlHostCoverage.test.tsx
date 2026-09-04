/**
 * Tests for SessionControlHost's app-facing half — the pieces a hosted control
 * actually talks to, which the main suite cannot reach.
 *
 * HARNESS. `../app-sdk` is replaced by a provider that records the props it was
 * handed and renders its children, exactly as `AppHostCoverage.test.tsx` does
 * for the sibling host. That is the only seam through which the three callbacks
 * this host builds (`subscribeFn` / `navigateFn` / `notifyFn`) can be invoked at
 * all: they are passed down, never called by the host itself, and the component
 * that would call them is a real ESM bundle fetched over HTTP. The same stand-in
 * doubles as the crash source for the error boundary — throwing from it is
 * indistinguishable, from the boundary's point of view, from a control that
 * throws on its first render.
 *
 * The dynamic `import('/apps/<app>/ui/<entry>')` is left REAL and left to fail;
 * the host's own `.catch` turns it into the inline "could not load" notice.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, cleanup } from '@testing-library/react'
import type { ReactNode } from 'react'
import { renderWithProviders } from './helpers'

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

/** Recorded `AppApiProvider` props + a render-throw switch for the boundary. */
const sdk = vi.hoisted(() => ({
  props: null as null | Record<string, unknown>,
  throwNext: false,
}))

vi.mock('../app-sdk', () => ({
  AppApiProvider: (props: Record<string, unknown>) => {
    sdk.props = props
    if (sdk.throwNext) throw new Error('the control blew up during render')
    return <div data-testid="sdk-provider">{props.children as ReactNode}</div>
  },
}))

import SessionControlHost from '../components/SessionControlHost'
import type { SessionControlContext } from '../components/SessionControlHost'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'

interface HostedProps {
  subscribeFn: (event: string, cb: (data: unknown) => void) => () => void
  navigateFn: (path: string) => void
  notifyFn: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

/** The props the host handed the SDK provider on its most recent render. */
function hosted(): HostedProps {
  if (!sdk.props) throw new Error('AppApiProvider was never rendered')
  return sdk.props as unknown as HostedProps
}

const control: ResolvedSessionControl = {
  key: 'test-app:scope',
  appName: 'test-app',
  appDisplayName: 'Test App',
  appVersion: '0.1.0',
  id: 'scope',
  entryPoint: 'dist/session-control.mjs',
  label: 'Scope',
  icon: 'Tag',
  allowedApi: [],
  allowedEvents: [],
  statusPath: 'session-status',
}

const session: SessionControlContext = {
  sessionKey: 'dashboard:chat-2-1787502679',
  cwd: '/home/me/repo',
}

/** A DOMRect is not constructible in jsdom; only these fields are read. */
const rect = (): DOMRect =>
  ({ left: 100, top: 400, width: 40, height: 28, right: 140, bottom: 428, x: 100, y: 400,
     toJSON: () => ({}) } as DOMRect)

function renderHost() {
  const onClose = vi.fn()
  const utils = renderWithProviders(
    <SessionControlHost control={control} session={session} anchorRect={rect()} onClose={onClose} />,
  )
  return { onClose, ...utils }
}

let errorSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  mockNavigate.mockClear()
  sdk.props = null
  sdk.throwNext = false
  // The host logs bundle-load failures and the boundary logs control crashes;
  // both are deliberate, so keep the run readable rather than asserting on them.
  errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  errorSpy.mockRestore()
  cleanup()
})

describe('SessionControlHost — the bridge handed to the control', () => {
  it('bridges host CustomEvents into a control subscription and unsubscribes on demand', () => {
    renderHost()

    const cb = vi.fn()
    const off = hosted().subscribeFn('scope:changed', cb)

    act(() => {
      window.dispatchEvent(new CustomEvent('mc:app:scope:changed', { detail: { scope: 'S-1' } }))
    })
    expect(cb).toHaveBeenCalledWith({ scope: 'S-1' })

    off()
    act(() => {
      window.dispatchEvent(new CustomEvent('mc:app:scope:changed', { detail: { scope: 'S-2' } }))
    })
    expect(cb).toHaveBeenCalledTimes(1)
  })

  it('closes the popover before navigating, so it cannot linger over the destination', () => {
    const { onClose } = renderHost()

    const order: string[] = []
    onClose.mockImplementation(() => order.push('close'))
    mockNavigate.mockImplementation(() => order.push('navigate'))

    hosted().navigateFn('/apps/test-app')

    expect(mockNavigate).toHaveBeenCalledWith('/apps/test-app')
    // Ordering is the point: dismissing after the route change would leave the
    // popover painted over the page it navigated to.
    expect(order).toEqual(['close', 'navigate'])
  })

  it('turns a control notification into an mc:notify event, with and without a type', () => {
    renderHost()

    const seen: unknown[] = []
    const listener = (e: Event) => seen.push((e as CustomEvent).detail)
    window.addEventListener('mc:notify', listener)
    try {
      hosted().notifyFn('scope bound', { type: 'success' })
      hosted().notifyFn('just so you know')
    } finally {
      window.removeEventListener('mc:notify', listener)
    }

    expect(seen).toEqual([
      { message: 'scope bound', type: 'success' },
      { message: 'just so you know' },
    ])
  })
})

describe('SessionControlHost — a control that crashes', () => {
  it('contains the crash and retries it without reloading the chat', () => {
    sdk.throwNext = true
    renderHost()

    // The boundary replaced the control's subtree, and nothing above it.
    expect(screen.queryByTestId('sdk-provider')).toBeNull()
    const retry = screen.getByRole('button')

    // Retry clears the boundary AND bumps the host's reset key, which is what
    // re-imports the bundle rather than re-rendering the same failed module.
    sdk.throwNext = false
    act(() => {
      fireEvent.click(retry)
    })

    expect(screen.getByTestId('sdk-provider')).toBeTruthy()
  })
})
