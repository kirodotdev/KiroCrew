/**
 * Which React Query cache an app's own calls land in.
 *
 * An app bundle renders inside the host tree and shares the host's React Query
 * MODULE instance, so `useQueryClient()` inside it resolves whatever client the
 * nearest provider holds. With the dashboard's own client there, an app reaches
 * every key in the product: `setQueryData(['auth-me'], …)` rewrites the signed-in
 * user the dashboard paints, `getQueryData` reads it back, and `clear()` empties
 * the whole cache in one call. `staleTime: Infinity` then keeps whatever it left
 * behind on screen, because freshness comes from server push rather than from
 * age — so an emptied or rewritten key stays that way until something else
 * invalidates it.
 *
 * So the host mounts a second provider around an external app, holding a client
 * of that app's own. Nothing about the shared module changes: one React Query
 * instance, one `QueryClientContext`, one copy of the library, and every hook an
 * app imports keeps working with no change to the app. What changes is only the
 * cache those hooks reach, which is the capability an app should not have.
 *
 * The client, not a guarded wrapper, is the fix because the reachable surface is
 * not a method list. `clear()` takes no key at all; `invalidateQueries`,
 * `removeQueries`, `resetQueries` and `cancelQueries` each treat a missing filter
 * as every key; `setQueryDefaults([], …)` matches by prefix and the empty prefix
 * matches everything. A wrapper has to enumerate all of them and stay correct as
 * the library grows one more. A separate cache answers all of them at once, and
 * answers the next one too.
 *
 * A builtin app keeps the dashboard's client. Builtin pages are host code and
 * share key prefixes with the dashboard on purpose — `artifact`, `awsConsent`,
 * `apps`, `pull-request-source`, `workflow-definitions` — which is why identity
 * GRANTS them a namespace through `useTrustedAppId()` where it refuses an
 * external app one. This module applies the same split to the cache client.
 *
 * The client is held per APP AND per host session binding, not per mount. A
 * per-mount client isolates just as well and costs an app its cached data every
 * time its page unmounts, so returning to an app would repaint from loading
 * placeholders — the opposite of what an app's own retention is for. The binding
 * belongs in that identity because one app id is hosted under several bindings at
 * once, and its keys carry no session of their own. One entry per pairing visited,
 * holding a cache whose queries are collected on the library's own `gcTime` once
 * nothing reads them, so an idle entry settles at an empty client rather than
 * growing.
 */
import React, { type ReactNode } from 'react'
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { newQueryClient, registerRecoverableQueryClient } from '../api/queryClient'
import type { AppOrigin } from './identity'

/**
 * appId -> host session binding -> the cache that pairing owns.
 *
 * The pair is held as nested maps rather than as one composed string key, so no
 * separator character exists for an app id or a session key to contain: a flat
 * `appId + sep + sessionKey` is only unique while neither half can spell `sep`,
 * and nothing about either value promises that.
 */
const appClients = new Map<string, Map<string | undefined, QueryClient>>()

/**
 * The client an external app of this id owns under this host session binding,
 * created on first use. One cache per (app, host session binding).
 *
 * Keyed by app id so two installed apps cannot read each other's cached data.
 * App ids are unique across what the gateway reports as installed, and an app
 * that self-registers under a builtin's name still lands here rather than on the
 * dashboard's client, because the origin decides that and the name does not.
 *
 * The app id ALONE is not the identity, because the same app id is mounted under
 * DIFFERENT bindings at the same time: `AppPage` hosts it as `dashboard:ui` and a
 * chat side panel hosts it as `dashboard:<slot>`, one panel per slot. The binding
 * decides which session the app's own requests answer for -- `scopedApi` sends it
 * as `X-Session-Key` -- and an external app's query key is passed through
 * unprefixed, since `useTrustedAppId()` refuses it a namespace. So two panels of
 * one app reading `['items']` under two sessions write the SAME key, and
 * `staleTime: Infinity` then serves the first session's rows to the second panel
 * with no error to notice. Keying on the pair is what keeps those two apart.
 *
 * A host that passes no binding shares one entry with the other unbound mounts of
 * that app, which is correct: they answer for the same session as each other.
 */
export function appQueryClient(appId: string, sessionKey?: string): QueryClient {
  let bindings = appClients.get(appId)
  if (!bindings) {
    bindings = new Map<string | undefined, QueryClient>()
    appClients.set(appId, bindings)
  }
  // An empty binding and an absent one are one mount population, not two.
  const binding = sessionKey || undefined
  const existing = bindings.get(binding)
  if (existing) return existing
  const client = newQueryClient()
  // A session lapse breaks this client's queries too, and the host's recovery
  // sweep runs over the clients registered here. Registering at creation is what
  // keeps an app's failed panel healing on recovery like a dashboard panel does.
  registerRecoverableQueryClient(client)
  bindings.set(binding, client)
  return client
}

/**
 * Put an app's subtree on the cache it is allowed to reach.
 *
 * Mounted by the host, above the app bundle, so an app cannot render its way out
 * of it: a context value is resolved from the nearest provider ABOVE the consumer,
 * and an app owns nothing above itself. An app is free to mount its own
 * `QueryClientProvider` inside — that only reaches another client of its own,
 * since no host client is importable from an app bundle.
 */
export function AppQueryClientProvider({
  appId,
  origin,
  sessionKey,
  children,
}: {
  appId: string
  origin: AppOrigin
  /** Host session this mount answers for; part of the cache identity. */
  sessionKey?: string
  children: ReactNode
}) {
  if (origin === 'builtin') return React.createElement(React.Fragment, null, children)
  return React.createElement(
    QueryClientProvider,
    { client: appQueryClient(appId, sessionKey) },
    children,
  )
}
