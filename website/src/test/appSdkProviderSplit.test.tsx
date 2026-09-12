/**
 * The provider split: identity layer vs scoped-API layer.
 *
 * `AppApiProvider` used to be the only way to get either, which is why a builtin
 * page — needing identity and no sandbox — could have neither. The split gives
 * each surface what it actually needs, and four properties keep that from
 * regressing:
 *
 * - the scoped layer takes its app name from identity, so a builtin page does not
 *   restate an id the host already minted
 * - a scoped layer with no name available fails LOUDLY, because `info.name`
 *   labels every permission refusal the client throws
 * - `AppApiProvider` never re-publishes identity over an existing one: shadowing a
 *   host-minted `builtin` with its own `external` default would silently revoke
 *   that page's state namespace
 * - the three props every caller hand-wrote identically now have defaults, and the
 *   subscribe default returns a real unsubscribe (returning `undefined` throws on
 *   unmount)
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AppApiProvider, useAppInfo, useAppEvents, useNotify } from '../app-sdk/index'
import { AppScopedApiProvider } from '../app-sdk/scopedApi'
import { AppIdentityProvider, useAppIdentity, useTrustedAppId } from '../app-sdk/identity'

function InfoProbe() {
  const info = useAppInfo()
  const identity = useAppIdentity()
  return (
    <div>
      <span data-testid="name">{info.name}</span>
      <span data-testid="events">{info.permissions.events.join(',') || 'none'}</span>
      <span data-testid="trusted">{useTrustedAppId() ?? 'refused'}</span>
      <span data-testid="origin">{identity?.origin ?? 'no-identity'}</span>
    </div>
  )
}

describe('AppScopedApiProvider', () => {
  it('takes the app name from the identity the host published', () => {
    render(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <AppScopedApiProvider allowedApiPaths={['/api/aws']} navigateFn={() => {}}>
          <InfoProbe />
        </AppScopedApiProvider>
      </AppIdentityProvider>,
    )
    expect(screen.getByTestId('name').textContent).toBe('aws-control')
    // The scoped layer publishes no identity of its own, so the builtin claim
    // above it is intact and the page keeps its host namespace.
    expect(screen.getByTestId('origin').textContent).toBe('builtin')
    expect(screen.getByTestId('trusted').textContent).toBe('aws-control')
  })

  it('lets an explicit appName win, for a component with no route above it', () => {
    render(
      <AppScopedApiProvider
        appName="ops-mission-control"
        allowedApiPaths={['/api/chat']}
        navigateFn={() => {}}
      >
        <InfoProbe />
      </AppScopedApiProvider>,
    )
    expect(screen.getByTestId('name').textContent).toBe('ops-mission-control')
    expect(screen.getByTestId('origin').textContent).toBe('no-identity')
  })

  it('refuses to mount with no name available at all', () => {
    // Loud, not an empty name: an unattributable permission refusal is
    // undebuggable, and the message has to name both ways out.
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() =>
      render(
        <AppScopedApiProvider allowedApiPaths={['/api/chat']} navigateFn={() => {}}>
          <InfoProbe />
        </AppScopedApiProvider>,
      ),
    ).toThrow(/could not resolve an app name/)
    err.mockRestore()
  })

  it('defaults allowedEvents to none declared', () => {
    render(
      <AppScopedApiProvider appName="zzq-app" allowedApiPaths={[]} navigateFn={() => {}}>
        <InfoProbe />
      </AppScopedApiProvider>,
    )
    expect(screen.getByTestId('events').textContent).toBe('none')
  })

  it('defaults subscribe to a real function, so useAppEvents can call it', () => {
    // The risk is the default being ABSENT, not what it returns: `useAppEvents`
    // invokes `subscribe(...)` during its effect, so an undefined one is a
    // TypeError on mount. (An effect returning `undefined` is legal React and
    // throws nothing — verified by mutation — so "returns a real unsubscribe" is
    // not the property worth asserting here.)
    function Subscriber() {
      useAppEvents('slots', () => {})
      return <span data-testid="subscribed">ok</span>
    }
    const { unmount } = render(
      <AppScopedApiProvider appName="zzq-app" allowedApiPaths={[]} navigateFn={() => {}}>
        <Subscriber />
      </AppScopedApiProvider>,
    )
    expect(screen.getByTestId('subscribed')).toBeInTheDocument()
    expect(() => unmount()).not.toThrow()
  })

  it('defaults notify to the host toast bus', () => {
    const seen: string[] = []
    const listener = (e: Event) => seen.push((e as CustomEvent).detail?.message)
    window.addEventListener('mc:notify', listener)
    function Notifier() {
      const notify = useNotify()
      return (
        <button type="button" data-testid="go" onClick={() => notify('zzq hello')}>
          go
        </button>
      )
    }
    render(
      <AppScopedApiProvider appName="zzq-app" allowedApiPaths={[]} navigateFn={() => {}}>
        <Notifier />
      </AppScopedApiProvider>,
    )
    screen.getByTestId('go').click()
    window.removeEventListener('mc:notify', listener)
    expect(seen).toEqual(['zzq hello'])
  })
})

describe('AppApiProvider composition', () => {
  const baseProps = {
    allowedApiPaths: ['/api/apps/zzq'],
    allowedEvents: [],
    subscribeFn: () => () => {},
    navigateFn: () => {},
    notifyFn: () => {},
  }

  it('publishes an external identity for an installed app', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    render(
      <AppApiProvider appName="zzq-installed" {...baseProps}>
        <InfoProbe />
      </AppApiProvider>,
    )
    expect(screen.getByTestId('origin').textContent).toBe('external')
    expect(screen.getByTestId('trusted').textContent).toBe('refused')
    warn.mockRestore()
  })

  it('carries a builtin origin through when the caller supplies one', () => {
    render(
      <AppApiProvider appName="zzq-builtin" origin="builtin" {...baseProps}>
        <InfoProbe />
      </AppApiProvider>,
    )
    expect(screen.getByTestId('origin').textContent).toBe('builtin')
    expect(screen.getByTestId('trusted').textContent).toBe('zzq-builtin')
  })

  it('does not shadow an identity the host already published', () => {
    // The regression this guards: a builtin page has `origin: 'builtin'` from
    // BuiltinAppRoute. Mounting this provider inside it would otherwise republish
    // the page as `external` — the default — and the page's state namespace would
    // vanish with no error at all.
    render(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <AppApiProvider appName="aws-control" {...baseProps}>
          <InfoProbe />
        </AppApiProvider>
      </AppIdentityProvider>,
    )
    expect(screen.getByTestId('origin').textContent).toBe('builtin')
    expect(screen.getByTestId('trusted').textContent).toBe('aws-control')
  })
})
