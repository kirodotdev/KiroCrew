/**
 * An external app's query cache is its own.
 *
 * The invariant, as one sentence: an external app's React Query calls resolve to
 * a client whose cache holds only that app's queries, so no host key is
 * readable, writable, invalidatable or removable from inside the app.
 *
 * Every case below is that sentence applied to one door, because the reachable
 * surface is not a single method. `clear()` takes no key at all, the four
 * `*Queries` methods treat a missing filter as "everything", and
 * `get/setQueryData` name a key directly — so a guard on any one of them leaves
 * the others open. A shared client fails all of them at once; an app-owned
 * client closes all of them at once, which is why the fix is the client and not
 * a method list.
 *
 * Two cases exist to keep the fix from over-reaching. A builtin app MUST keep
 * the host client: builtin pages are host code and deliberately share key
 * prefixes with the dashboard (`artifact`, `apps`, `awsConsent`). And an app
 * returned to must still find its data, which is why the client is held per app
 * rather than per mount — a per-mount client would isolate correctly and silently
 * cost every app the cache retention a remount is supposed to keep.
 *
 * One case pins the other half of that identity: the HOST SESSION BINDING. One app
 * id is mounted under several bindings at once, its keys carry no session, and the
 * binding travels only as a request header — so two panels of one app under two
 * sessions would otherwise read one entry.
 *
 * The last case pins the residual: isolation holds only while the host's own
 * singleton stays unreachable from the surfaces an app can resolve.
 *
 * Two further cases guard the boundary from each side. One origin decides both
 * the identity an app is published under and the client it resolves, so those two
 * are asserted together. And the host's post-lapse recovery invalidation names a
 * query STATE rather than a key, so it has to reach an app's client as well —
 * isolation is about what an app can touch, not about withholding a repair the
 * host owes every panel in the page.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import type { ReactNode } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { AppApiProvider } from '../app-sdk/index'
import { useAppIdentity, type AppIdentity, type AppOrigin } from '../app-sdk/identity'
import { appQueryClient } from '../app-sdk/appQueryClient'
import {
  queryClient as hostSingleton,
  invalidateAcrossQueryClients,
  recoverableQueryClients,
} from '../api/queryClient'

/** A host key with real consequences: `api/client.ts` invalidates this one by name. */
const HOST_KEY = ['auth-me'] as const

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

/** A host client already holding the dashboard's own cached identity. */
function hostClient(): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(HOST_KEY, { user: 'raymond' })
  return client
}

/** Publishes the client an app's own `useQueryClient()` resolves to. */
function Probe({ into }: { into: { current: QueryClient | null } }) {
  into.current = useQueryClient()
  return <span data-testid="mounted">app</span>
}

/**
 * Mount `children` the way a host renders an app bundle: inside the dashboard's
 * provider, under the app's identity. `AppHost` passes `origin` explicitly and
 * `SessionControlHost` takes the default; both arrive here.
 */
function mountApp(
  appId: string,
  host: QueryClient,
  children: ReactNode,
  origin: AppOrigin = 'external',
  sessionKey?: string,
) {
  return render(
    <QueryClientProvider client={host}>
      <AppApiProvider
        appName={appId}
        origin={origin}
        sessionKey={sessionKey}
        allowedApiPaths={[]}
        navigateFn={() => {}}
      >
        {children}
      </AppApiProvider>
    </QueryClientProvider>,
  )
}

/** The client an app of this id resolves, mounted and then discarded. */
function clientSeenBy(
  appId: string,
  host: QueryClient,
  origin: AppOrigin = 'external',
  sessionKey?: string,
) {
  const seen: { current: QueryClient | null } = { current: null }
  const view = mountApp(appId, host, <Probe into={seen} />, origin, sessionKey)
  expect(screen.getByTestId('mounted').textContent).toBe('app')
  const client = seen.current
  if (!client) throw new Error('no client resolved')
  return { client, view }
}

describe('external app query-cache isolation', () => {
  it('hands an external app a client that is not the host client', () => {
    const host = hostClient()
    const { client } = clientSeenBy('todo-ledger', host)
    expect(client).not.toBe(host)
  })

  it('keeps the host cache when the app clears its own', () => {
    // The accidental case, and the reason this is not only a malice story: an app
    // resetting its own state calls `clear()` with no key, and on a shared client
    // that wipes the dashboard.
    const host = hostClient()
    const { client } = clientSeenBy('todo-txt', host)
    client.clear()
    expect(host.getQueryData(HOST_KEY)).toEqual({ user: 'raymond' })
  })

  it('cannot write a host key', () => {
    const host = hostClient()
    const { client } = clientSeenBy('launchdarkly', host)
    client.setQueryData(HOST_KEY, { user: 'attacker' })
    expect(host.getQueryData(HOST_KEY)).toEqual({ user: 'raymond' })
  })

  it('cannot read a host key', () => {
    const host = hostClient()
    const { client } = clientSeenBy('reader-app', host)
    expect(client.getQueryData(HOST_KEY)).toBeUndefined()
  })

  it('cannot invalidate or remove host queries with an empty filter', () => {
    // `staleTime: Infinity` is what makes this stick: an invalidated host query
    // does not heal by aging out, so the dashboard keeps showing the emptied or
    // refetching state until something else touches that key.
    const host = hostClient()
    const { client } = clientSeenBy('sweeper-app', host)
    client.invalidateQueries()
    client.removeQueries()
    expect(host.getQueryState(HOST_KEY)?.isInvalidated).toBe(false)
    expect(host.getQueryData(HOST_KEY)).toEqual({ user: 'raymond' })
  })

  it('cannot retarget host queries through an empty-prefix default', () => {
    const host = hostClient()
    const { client } = clientSeenBy('defaults-app', host)
    client.setQueryDefaults([], { gcTime: 0 })
    expect(host.getQueryDefaults(HOST_KEY)).toEqual({})
  })

  it('still gives a builtin app the host client', () => {
    // Builtin pages are host code. They share key prefixes with the dashboard on
    // purpose, so isolating them would break the sharing the platform documents.
    const host = hostClient()
    const { client } = clientSeenBy('aws-control', host, 'builtin')
    expect(client).toBe(host)
    expect(client.getQueryData(HOST_KEY)).toEqual({ user: 'raymond' })
  })

  it('keeps one app client across remounts, so returning to an app finds its data', () => {
    const host = hostClient()
    const first = clientSeenBy('retained-app', host)
    first.client.setQueryData(['own-data'], 'kept')
    first.view.unmount()
    const second = clientSeenBy('retained-app', host)
    expect(second.client).toBe(first.client)
    expect(second.client.getQueryData(['own-data'])).toBe('kept')
  })

  it('gives two external apps separate clients', () => {
    const host = hostClient()
    const one = clientSeenBy('app-one', host)
    one.view.unmount()
    const two = clientSeenBy('app-two', host)
    expect(two.client).not.toBe(one.client)
    one.client.setQueryData(['secret'], 'app-one only')
    expect(two.client.getQueryData(['secret'])).toBeUndefined()
  })

  it('splits one app id across two host session bindings', () => {
    // The app id is not the whole identity. `AppPage` hosts an app as
    // `dashboard:ui` and each chat side panel hosts the SAME app as
    // `dashboard:<slot>`, so two mounts answer for different sessions at once.
    // The binding rides as `X-Session-Key` and never enters the key, and an
    // external app's key is unprefixed, so both panels writing `['items']` would
    // land on one entry and `staleTime: Infinity` would serve the first session's
    // rows to the second panel with nothing to notice.
    const host = hostClient()
    const ui = clientSeenBy('cross-session-app', host, 'external', 'dashboard:ui')
    ui.client.setQueryData(['items'], 'ui rows')
    ui.view.unmount()

    const slot = clientSeenBy('cross-session-app', host, 'external', 'dashboard:slot-7')
    expect(slot.client).not.toBe(ui.client)
    expect(slot.client.getQueryData(['items'])).toBeUndefined()
    slot.view.unmount()

    // And the same binding still returns the same cache, so a panel returning to
    // its own session finds its own rows.
    const again = clientSeenBy('cross-session-app', host, 'external', 'dashboard:ui')
    expect(again.client).toBe(ui.client)
    expect(again.client.getQueryData(['items'])).toBe('ui rows')
  })

  it('publishes no route to the host singleton, which is what isolation rests on', () => {
    // Isolation is a property of which client the app's context resolves. It would
    // fall the moment the host's own client became importable from an app bundle,
    // and the two surfaces an app can resolve are the import map and the SDK stub.
    const config = read('../../vite.config.ts')
    const importMapKeys = [...config.matchAll(/'([^']+)': '\/vendor\/([^']+)'/g)]
    expect(importMapKeys.length).toBeGreaterThan(5)
    for (const [, , filename] of importMapKeys) {
      expect(read(`../../public/vendor/${filename}`)).not.toContain('queryClient')
    }
    expect(read('../app-sdk/index.ts')).not.toContain("from '../api/queryClient'")
  })

  it('publishes identity and the cache client from the same origin', () => {
    // The two decisions that must not drift apart: an app published as external
    // is refused the host namespace AND the host client, and a builtin is granted
    // both. Asserting them in one case is what catches a later change that moves
    // one of them to a different condition.
    const host = hostClient()
    const seen: { identity: AppIdentity | null; client: QueryClient | null } = {
      identity: null,
      client: null,
    }
    function Pair() {
      seen.identity = useAppIdentity()
      seen.client = useQueryClient()
      return <span data-testid="mounted">app</span>
    }

    const external = mountApp('published-external', host, <Pair />)
    expect(seen.identity?.origin).toBe('external')
    expect(seen.client).not.toBe(host)
    external.unmount()

    mountApp('aws-control', host, <Pair />, 'builtin')
    expect(seen.identity?.origin).toBe('builtin')
    expect(seen.client).toBe(host)
  })

  it('lets host recovery refetch an app query broken by a session lapse', async () => {
    // A lapse errors queries wherever they live, and the recovery invalidation in
    // `api/client.ts` names a STATE rather than a key. Sweeping the dashboard's
    // client alone would heal dashboard panels and leave an app's panel in the
    // error state, with `staleTime: Infinity` keeping it there.
    const host = hostClient()
    let attempts = 0
    function Panel() {
      const q = useQuery({
        queryKey: ['panel'],
        retry: false,
        queryFn: () => {
          attempts += 1
          return attempts === 1
            ? Promise.reject(new Error('lapse'))
            : Promise.resolve('recovered')
        },
      })
      return <span data-testid="panel">{q.data ?? q.status}</span>
    }

    mountApp('lapse-app', host, <Panel />)
    await waitFor(() => expect(screen.getByTestId('panel').textContent).toBe('error'))
    expect(attempts).toBe(1)
    expect(recoverableQueryClients()).toContain(hostSingleton)
    expect(recoverableQueryClients()).toContain(appQueryClient('lapse-app'))

    invalidateAcrossQueryClients({
      predicate: (q) => q.state.status === 'error' && q.state.data === undefined,
    })
    await waitFor(() => expect(screen.getByTestId('panel').textContent).toBe('recovered'))
    expect(attempts).toBe(2)
  })
})
