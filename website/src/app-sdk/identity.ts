/**
 * App identity — "which app owns this page", published by the host.
 *
 * This is the identity LAYER of the app SDK, separate from the scoped-API layer
 * in `./index`. The split exists because the two are needed by different
 * callers: an external app needs both (it has identity AND must be sandboxed),
 * while a builtin page needs only the first. A builtin is "has identity, needs
 * no sandbox", and before this layer existed it had neither — six builtin app
 * files carry comments explaining that they cannot use the SDK because
 * `AppApiProvider` is mounted only around installed apps.
 *
 * Identity is published as a plain context value from a render body, never from
 * an effect. The ordering is load-bearing: it must land before the app's first
 * child query mounts, which is exactly why issue-radar puts its own
 * `setQueryDefaults` call at module scope. React renders a parent before its
 * children, so a provider in the render body gives that guarantee; an effect
 * runs AFTER the child has already mounted and queried, so anything keyed off
 * identity would miss its first read and silently fall back to un-namespaced
 * behaviour.
 *
 * Imported by PATH (`app-sdk/identity`) and deliberately NOT re-exported from
 * `./index`, following the same reasoning `useComposerDraft` records there. The
 * barrel is the surface third-party apps resolve — `chatProtocolBoundary.test.ts`
 * holds it in exact agreement with `public/vendor/kirocrew-app-sdk.mjs` — so a
 * name added to it is published, and publishing later is additive while
 * un-publishing is a break. There is nothing to publish yet: the consumers are
 * host code and builtin pages, which import this module directly, and
 * `useTrustedAppId` is builtin-only BY CONSTRUCTION, so in a third-party app it
 * would be a hook that always returns null.
 */
import { createContext, useContext, type ReactNode } from 'react'
import React from 'react'

/**
 * Provenance of an identity claim, as the gateway reports it.
 *
 * Only `'builtin'` may use a host-owned namespace. `origin: 'builtin'` is
 * assigned solely by `register_builtin_apps()` and is refused on the
 * self-registration path, which is what makes it the one claim an external app
 * cannot forge.
 */
export type AppOrigin = 'builtin' | 'external'

export interface AppIdentity {
  /**
   * Host-minted app id — the app's `/api/apps` name. Validated where ids are
   * registered (`apps/appIdentity.ts`), because it is used as a key segment.
   */
  readonly appId: string
  /** Provenance. Only `'builtin'` may use a host-owned namespace. */
  readonly origin: AppOrigin
}

const AppIdentityContext = createContext<AppIdentity | null>(null)

/**
 * Identity of the app that owns the current page, or `null` on a host page.
 *
 * Returns `null` rather than throwing the way `useAppApi()` does. A host page
 * legitimately has no app identity — `/chat`, settings and the app store all
 * render outside any app — so the platform features built on identity have to
 * be able to ASK. Throwing would make a shared hook unusable anywhere it might
 * render outside an app page.
 */
export function useAppIdentity(): AppIdentity | null {
  return useContext(AppIdentityContext)
}

/** appIds already reported as refused, so the console stays useful across remounts. */
const refusedNamespaceLogged = new Set<string>()

/**
 * The appId when the host may namespace persisted state and cached data to it,
 * otherwise `null`.
 *
 * This is the SINGLE builtin gate, and callers must read it through this hook
 * rather than testing `useAppIdentity().origin` themselves — one gate to audit
 * and one place to change, instead of the same condition hand-rolled at every
 * consumer.
 *
 * Why the gate is on the namespace and not on identity: a namespace is granted
 * by ID, and an app id is not unique across origins. An external app can
 * self-register under the name `aws-control`; if the namespace followed the name
 * alone it would land in the builtin's `kc:app:aws-control:*` keys and in its
 * query-key prefix. So identity is still published for an external app
 * (`useAppIdentity`, `useAppInfo`) — what is refused here is the host namespace.
 */
export function useTrustedAppId(): string | null {
  const identity = useAppIdentity()
  if (!identity) return null
  if (identity.origin !== 'builtin') {
    // Warn once per app for the process lifetime: a refusal is otherwise
    // invisible (the feature just does not happen), and repeating it on every
    // render of a legitimately-external app would drown out real signal.
    if (!refusedNamespaceLogged.has(identity.appId)) {
      refusedNamespaceLogged.add(identity.appId)
      // eslint-disable-next-line no-console -- a refused capability is invisible otherwise
      console.warn(
        `[app-sdk] App "${identity.appId}" (origin ${identity.origin}) may not use a ` +
          `host-owned state namespace; that is builtin-only.`,
      )
    }
    return null
  }
  return identity.appId
}

/**
 * Publish an app's identity to everything rendered below.
 *
 * Mount this from a render body, not an effect (see the module header). The
 * host has two callers: `BuiltinAppRoute` for a builtin page, and `AppHost` for
 * an installed app — the latter through `AppApiProvider`, which composes this
 * layer with the scoped-API layer.
 *
 * `appId` is NOT re-validated here. An external app's id reaches this provider
 * from its manifest, and refusing it would break `useAppInfo()` for an app whose
 * name is merely outside the host-key charset — while granting it nothing,
 * because `useTrustedAppId()` refuses a non-builtin origin regardless. The
 * charset gate belongs where an id becomes a host key, which is registration.
 */
export function AppIdentityProvider({
  appId,
  origin,
  children,
}: {
  appId: string
  origin: AppOrigin
  children: ReactNode
}) {
  // Memoized so the context value's identity is stable across the host's
  // re-renders: a consumer that keys a query or an effect off this object must
  // not see a new object every time an ancestor repaints.
  const value = React.useMemo<AppIdentity>(() => ({ appId, origin }), [appId, origin])
  return React.createElement(AppIdentityContext.Provider, { value }, children)
}
