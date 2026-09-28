/**
 * `useAppQuery` and the retention seam, mounted.
 *
 * Three properties are worth a tree rather than a pure test: that a namespaced
 * key is byte-identical to the one an app writes by hand, that the builtin gate
 * is read through `useTrustedAppId()` and not re-decided here, and that
 * retention is registered BEFORE the page's first query — including on a repeat
 * visit, which is the case a cold-load test cannot see.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { lazy, Suspense } from 'react'
import { render, screen, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppIdentityProvider, type AppOrigin } from '../app-sdk/identity'
// Two providers, two import paths, and the difference is the point of the cases
// below. `AppApiProvider` is on the barrel and PUBLISHES identity (guarded so it
// never shadows a host-minted one); `AppScopedApiProvider` is path-imported like
// this module, is absent from the vendor stub, and only READS identity. So only
// the first can cost a builtin page its namespace.
import { AppApiProvider } from '../app-sdk/index'
import { AppScopedApiProvider } from '../app-sdk/scopedApi'
import {
  AppCacheRetention,
  useAppQuery,
  useAppQueryKey,
} from '../app-sdk/appQuery'
import { APP_CACHE_RETENTION_MS } from '../apps/appCacheRetention'
import { queryClient as sharedQueryClient } from '../api/queryClient'

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

/** Mount `ui` under a client, optionally inside an app identity. */
function mount(
  ui: React.ReactElement,
  client: QueryClient,
  identity?: { appId: string; origin: AppOrigin },
) {
  const body = identity ? (
    <AppIdentityProvider appId={identity.appId} origin={identity.origin}>
      {ui}
    </AppIdentityProvider>
  ) : (
    ui
  )
  return render(<QueryClientProvider client={client}>{body}</QueryClientProvider>)
}

let warn: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  // The builtin gate warns once per refused app, and `resolveAppQueryKey` warns once
  // per double-prefixed app. Both are asserted where they matter; elsewhere they
  // are noise.
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

/* ── the key an app's query actually gets ─────────────────────────────────── */

function AccountsProbe() {
  const q = useAppQuery(['accounts'], { queryFn: () => Promise.resolve('loaded') })
  return <div data-testid="accounts">{q.data ?? 'pending'}</div>
}

describe('useAppQuery', () => {
  it('lands under the app namespace, byte-identical to a hand-written key', async () => {
    // The literal is what `AwsControlPage.tsx` writes today. If the prefix shape
    // ever became `['app', 'aws-control', …]`, every existing
    // `invalidateQueries(['aws-control', …])` in the app would stop matching its
    // own queries — the failure this assertion exists to prevent.
    const client = newClient()
    mount(<AccountsProbe />, client, { appId: 'aws-control', origin: 'builtin' })
    expect(await screen.findByText('loaded')).toBeTruthy()
    expect(client.getQueryData(['aws-control', 'accounts'])).toBe('loaded')
  })

  it('degrades to a plain query where there is no host namespace', async () => {
    // A host page has no app identity. The key is used unchanged: it still
    // fetches and still shares with any other reader of the same key.
    const client = newClient()
    mount(<AccountsProbe />, client)
    expect(await screen.findByText('loaded')).toBeTruthy()
    expect(client.getQueryData(['accounts'])).toBe('loaded')
  })

  it('refuses the namespace to an external app', async () => {
    // MUTATION: read `useAppIdentity().appId` instead of `useTrustedAppId()` —
    // an external app that self-registers the name `aws-control` would land in
    // the builtin's key namespace, and this assertion is what catches it.
    const client = newClient()
    mount(<AccountsProbe />, client, { appId: 'aws-control-lookalike', origin: 'external' })
    expect(await screen.findByText('loaded')).toBeTruthy()
    expect(client.getQueryData(['accounts'])).toBe('loaded')
    expect(client.getQueryData(['aws-control-lookalike', 'accounts'])).toBeUndefined()
    expect(warn).toHaveBeenCalled()
  })

  it('keeps the namespace when the page mounts the SCOPED api layer inside itself', async () => {
    // The real shape of two builtin pages: `spec-builder/SpecBuilderPage.tsx:131`
    // and `ops-mission-control/IncidentChat.tsx:77` render
    // `AppScopedApiProvider` in their own subtree and let it resolve their name
    // from the host identity above. A query below that provider is still the
    // app's own, so it must still be namespaced.
    const client = newClient()
    mount(
      <AppScopedApiProvider allowedApiPaths={['/api/apps/aws-control']} navigateFn={() => {}}>
        <AccountsProbe />
      </AppScopedApiProvider>,
      client,
      { appId: 'aws-control', origin: 'builtin' },
    )
    expect(await screen.findByText('loaded')).toBeTruthy()
    expect(client.getQueryData(['aws-control', 'accounts'])).toBe('loaded')
  })

  it('keeps the namespace when a provider that CAN publish identity is nested inside', async () => {
    // `AppApiProvider` defaults to `origin: 'external'` and publishes identity —
    // but only when there is none already. Nested under a builtin page it must
    // not shadow, and this is what the shadowing would have cost: the key here
    // would fall back to `['accounts']` while retention stayed registered on
    // `['aws-control']`, so the app's data would land outside the namespace being
    // retained. No error, no skeleton fix, nothing to grep for.
    //
    // MUTATION: delete the `if (existing) return scoped` early return in
    // `app-sdk/index.ts` — this fails and the one above stays green, because only
    // this provider publishes.
    const client = newClient()
    mount(
      <AppApiProvider
        appName="some-installed-app"
        allowedApiPaths={['/api/apps/some-installed-app']}
        navigateFn={() => {}}
      >
        <AccountsProbe />
      </AppApiProvider>,
      client,
      { appId: 'aws-control', origin: 'builtin' },
    )
    expect(await screen.findByText('loaded')).toBeTruthy()
    expect(client.getQueryData(['aws-control', 'accounts'])).toBe('loaded')
    expect(client.getQueryData(['accounts'])).toBeUndefined()
    expect(client.getQueryData(['some-installed-app', 'accounts'])).toBeUndefined()
  })
})

/* ── the key builder every other cache API uses ───────────────────────────── */

describe('useAppQueryKey', () => {
  it('builds the same key useAppQuery used, so an invalidation still matches', async () => {
    // The property that keeps a mutation able to refresh the list it changed.
    // `useAppQuery` and every `invalidateQueries` / `setQueryData` /
    // `useInfiniteQuery` site must agree byte for byte; sharing one resolver is
    // what makes that structural instead of a convention.
    const client = newClient()
    let builtKey: readonly unknown[] = []
    function KeyProbe() {
      builtKey = useAppQueryKey()(['accounts'])
      return <AccountsProbe />
    }
    mount(<KeyProbe />, client, { appId: 'aws-control', origin: 'builtin' })
    expect(await screen.findByText('loaded')).toBeTruthy()
    expect(builtKey).toEqual(['aws-control', 'accounts'])
    // Not just equal by value — it addresses the entry the query created.
    expect(client.getQueryData(builtKey)).toBe('loaded')
    await act(async () => {
      await client.invalidateQueries({ queryKey: builtKey })
    })
    expect(client.getQueryState(builtKey)?.dataUpdateCount).toBeGreaterThan(1)
  })

  it('degrades with the same rule the hook uses', () => {
    const client = newClient()
    let builtKey: readonly unknown[] = []
    function KeyProbe() {
      builtKey = useAppQueryKey()(['accounts'])
      return null
    }
    mount(<KeyProbe />, client)
    expect(builtKey).toEqual(['accounts'])
  })
})

/* ── retention, and WHEN it is registered ─────────────────────────────────── */

/**
 * What the page saw for its own namespace on each of its renders.
 *
 * The FIRST entry is the contract: a page that renders before retention is
 * registered has already created its queries with react-query's 5-minute
 * default, which is the bug, and asserting the settled state would pass either
 * way.
 */
const observed: (number | undefined)[] = []
/**
 * Every time a mount actually SUSPENDED, recorded as it happens.
 *
 * Recorded rather than read off the DOM afterwards, because by the time
 * `findByTestId('page')` resolves the fallback has been replaced either way --
 * so `queryByTestId('fallback')` is null whether the mount suspended or not, and
 * asserting on it proves nothing. Whether the page was cold or warm is the whole
 * difference between these two cases, so it has to be observable.
 */
const suspended: string[] = []
let probeClient: QueryClient
let probeAppId = ''

function RetentionProbe() {
  observed.push(probeClient.getQueryDefaults([probeAppId]).gcTime as number | undefined)
  return <div data-testid="page">page</div>
}

function Fallback() {
  suspended.push(probeAppId)
  return <div data-testid="fallback">loading</div>
}

/**
 * ONE lazy component for the module's lifetime, as the real registry has: the
 * registry builds its `lazy()` calls at module scope, so a second visit within a
 * session finds the module resolved. A fresh `lazy()` per mount would be
 * permanently cold, and it is the WARM case that tells a render-body
 * registration apart from an effect.
 */
const WarmLazy = lazy(() => Promise.resolve({ default: RetentionProbe }))

function mountPage(
  client: QueryClient,
  appId: string,
  Page: React.ComponentType,
  origin: AppOrigin = 'builtin',
) {
  probeClient = client
  probeAppId = appId
  return render(
    <QueryClientProvider client={client}>
      <AppIdentityProvider appId={appId} origin={origin}>
        <AppCacheRetention client={client} />
        <Suspense fallback={<Fallback />}>
          <Page />
        </Suspense>
      </AppIdentityProvider>
    </QueryClientProvider>,
  )
}

describe('app cache retention', () => {
  beforeEach(() => {
    observed.length = 0
    suspended.length = 0
  })

  it('is in place before the page renders on a COLD load', async () => {
    // Its own lazy, resolved by this test, so the case is cold no matter what
    // ran before it -- sharing the module-scope lazy would make it cold only
    // when it happens to run first.
    //
    // This case passes even against an effect-based registration, and that is
    // not a flaw in the assertion: Suspense holds the child back until after the
    // parent has committed, so the effect has already run by the child's first
    // render. It is kept because it is the ordinary path, and its VALUE is the
    // contrast with the warm case below.
    let release!: (m: { default: React.ComponentType }) => void
    const ColdLazy = lazy(
      () => new Promise<{ default: React.ComponentType }>((resolve) => { release = resolve }),
    )
    const client = newClient()
    mountPage(client, 'cold-app', ColdLazy)

    // It really was cold: the tree suspended before the page existed.
    expect(suspended).toEqual(['cold-app'])
    expect(observed).toHaveLength(0)

    release({ default: RetentionProbe })
    await screen.findByTestId('page')
    expect(observed[0]).toBe(APP_CACHE_RETENTION_MS)
  })

  it('is in place before the page renders on a REPEAT visit', async () => {
    // The case this design exists for, and the only one that reds against an
    // effect. Both mounts happen HERE rather than relying on the cold test above
    // having warmed the module: as an order-dependent test it passed against the
    // effect version whenever it ran alone, which a `-t` filter or a shard split
    // does routinely.
    //
    // MUTATION: move the `applyCacheRetention` call in `useAppCacheRetention`
    // into a `useEffect` -- this reds, alone or in file order, while the cold
    // case above stays green.
    const first = newClient()
    const { unmount } = mountPage(first, 'warm-app', WarmLazy)
    await screen.findByTestId('page')
    // The first visit paid the suspension; that is what leaves the module loaded.
    expect(suspended).toEqual(['warm-app'])
    unmount()

    observed.length = 0
    suspended.length = 0
    // A fresh client, so retention has to be registered again rather than being
    // left over from the first visit.
    const second = newClient()
    mountPage(second, 'warm-app', WarmLazy)

    // Nothing suspended: React rendered the route and the page in ONE pass, so
    // no parent had committed and no effect had run. Asserted BEFORE any await,
    // because the point is what the page saw on its first render.
    expect(suspended).toEqual([])
    expect(observed[0]).toBe(APP_CACHE_RETENTION_MS)
    await screen.findByTestId('page')
  })

  it('registers one default per client and app, not one per render', async () => {
    // MUTATION: drop the WeakMap/Set guard — react-query warns once several
    // query defaults match a key, and the noise arrives long after the cause.
    const client = newClient()
    const spy = vi.spyOn(client, 'setQueryDefaults')
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="once-app" origin="builtin">
          <AppCacheRetention client={client} />
          <AppCacheRetention client={client} />
        </AppIdentityProvider>
      </QueryClientProvider>,
    )
    rerender(
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="once-app" origin="builtin">
          <AppCacheRetention client={client} />
          <AppCacheRetention client={client} />
        </AppIdentityProvider>
      </QueryClientProvider>,
    )
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(['once-app'], { gcTime: APP_CACHE_RETENTION_MS })
  })

  it('registers nothing on a host page', () => {
    const client = newClient()
    const spy = vi.spyOn(client, 'setQueryDefaults')
    render(
      <QueryClientProvider client={client}>
        <AppCacheRetention client={client} />
      </QueryClientProvider>,
    )
    expect(spy).not.toHaveBeenCalled()
  })

  it('registers nothing for an external app', () => {
    // Same single gate as the key namespace: builtin-only, decided in
    // `useTrustedAppId()`, not re-tested here.
    const client = newClient()
    const spy = vi.spyOn(client, 'setQueryDefaults')
    render(
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="third-party" origin="external">
          <AppCacheRetention client={client} />
        </AppIdentityProvider>
      </QueryClientProvider>,
    )
    expect(spy).not.toHaveBeenCalled()
    expect(warn).toHaveBeenCalled()
  })

  it('does not reach a key prefix that is not the app id', () => {
    // Documents an intended NO-OP so it is not later "fixed" into a defect.
    // Several apps' keys are named for the resource, not the app, because the
    // host reads them too: `workflows` uses `workflow-definitions`, shared with
    // the workflow cards in chat. Retention at `[appId]` deliberately does not
    // reach those — the host's cache stays the host's decision — and such an app
    // opts in by adopting `useAppQuery`, not by having its keys renamed.
    const client = newClient()
    client.setQueryDefaults(['workflows'], { gcTime: APP_CACHE_RETENTION_MS })
    expect(client.getQueryDefaults(['workflows', 'list']).gcTime).toBe(APP_CACHE_RETENTION_MS)
    expect(client.getQueryDefaults(['workflow-definitions']).gcTime).toBeUndefined()
  })
})

/* ── the CONSEQUENCE, not just the registration ───────────────────────────── */

/**
 * Everything above proves `setQueryDefaults` was called with the right value at
 * the right moment. None of it proves the thing a user feels: that the data is
 * still there on the way back in.
 *
 * These two cases are that proof, and they are an A/B of the same flow -- load,
 * unmount, wait, look -- differing only in whether the page had an app identity.
 * The clock is faked, so the six minutes the bug needs cost no wall time. The
 * control is what makes it evidence: without identity the entry is gone at six
 * minutes on react-query's 5-minute default, which is the reported symptom
 * reproduced in a test rather than argued about.
 */
describe('retention, observed on the clock', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  /**
   * Load a query under this tree, then hand back its key and an unmount.
   *
   * The tree is IDENTICAL in both arms, seam included -- the only variable is
   * whether an identity is published above it. That is what makes the pair an
   * A/B: with no identity the seam reads `null` from the gate and registers
   * nothing, which is every builtin app page's behaviour today.
   */
  async function loadThenUnmount(client: QueryClient, identity?: { appId: string; origin: AppOrigin }) {
    let key: readonly unknown[] = []
    function Probe() {
      key = useAppQueryKey()(['accounts'])
      return <AccountsProbe />
    }
    const view = mount(
      <>
        <AppCacheRetention client={client} />
        <Probe />
      </>,
      client,
      identity,
    )
    await screen.findByText('loaded')
    return { key, unmount: view.unmount }
  }

  it('keeps an app page\'s data past the 5-minute default, and still bounds it', async () => {
    const client = newClient()
    const { key, unmount } = await loadThenUnmount(client, {
      appId: 'aws-control',
      origin: 'builtin',
    })
    expect(key).toEqual(['aws-control', 'accounts'])
    expect(client.getQueryData(key)).toBe('loaded')

    // Fake the clock only from here: the fetch above needs real promises.
    vi.useFakeTimers()
    unmount()

    // Six minutes away — past react-query's default, inside this app's retention.
    // MUTATION: change APP_CACHE_RETENTION_MS to 5 minutes and this is gone.
    vi.advanceTimersByTime(6 * 60_000)
    expect(client.getQueryData(key)).toBe('loaded')

    // Bounded, not Infinity: a tab left open for an hour does not hold every app's
    // data forever.
    vi.advanceTimersByTime(25 * 60_000)
    expect(client.getQueryData(key)).toBeUndefined()
  })

  it('loses it at six minutes without an app identity — the symptom', async () => {
    // The control, and the reported bug: same flow, no identity, so no namespace
    // and no retention. This is what every builtin app page does today.
    const client = newClient()
    const { key, unmount } = await loadThenUnmount(client)
    expect(key).toEqual(['accounts'])
    expect(client.getQueryData(key)).toBe('loaded')

    vi.useFakeTimers()
    unmount()

    vi.advanceTimersByTime(6 * 60_000)
    expect(client.getQueryData(key)).toBeUndefined()
  })
})

/* ── the seam is actually wired ───────────────────────────────────────────── */

vi.mock('../apps/builtinRegistry', async () => {
  const { lazy: reactLazy } = await import('react')
  const component = reactLazy(() =>
    Promise.resolve({ default: () => <div data-testid="wired-page">page</div> }),
  )
  return {
    getBuiltinApp: (path: string) =>
      path === '/wired-app' ? { component, appId: 'wired-app-id' } : undefined,
    hasBuiltinComponent: (path: string) => path === '/wired-app',
    BUILTIN_COMPONENT_REGISTRY: {},
  }
})

describe('BuiltinAppRoute', () => {
  it('gives a builtin page its app retention', async () => {
    // End to end through the real route component, on the shared client it uses
    // in production. Without this, every assertion above could hold while the
    // seam was mounted nowhere.
    const { default: BuiltinAppRoute } = await import('../apps/BuiltinAppRoute')
    render(
      <MemoryRouter initialEntries={['/wired-app']}>
        <Routes>
          <Route path="/:builtinApp" element={<BuiltinAppRoute />} />
        </Routes>
      </MemoryRouter>,
    )
    await screen.findByTestId('wired-page')
    expect(sharedQueryClient.getQueryDefaults(['wired-app-id']).gcTime).toBe(
      APP_CACHE_RETENTION_MS,
    )
  })
})
