/**
 * The scoped-API layer of the app SDK: a permission-fenced HTTP client, the host
 * bridges, and the context every SDK hook reads.
 *
 * Deliberately NOT re-exported from `./index`. That barrel is the surface
 * third-party apps resolve -- `chatProtocolBoundary.test.ts` holds every value
 * export of it in exact agreement with `public/vendor/kirocrew-app-sdk.mjs` -- so
 * a name placed there is PUBLISHED, and publishing later is additive while
 * un-publishing is a break. `AppScopedApiProvider`'s consumers are two
 * host-internal builtin pages that can import this path directly, so there is
 * nothing an external app needs here yet. Same reasoning as `./identity`.
 *
 * `AppApiProvider` stays on the barrel: it is already published, and `AppHost`
 * mounts it for installed apps.
 */
import { createContext, useContext, type ReactNode } from 'react'
import React from 'react'
import { noteStaleOwnerResponse } from '../api/staleOwnerSignal'
import { useAppIdentity } from './identity'

export interface AppApi {
  /** GET request scoped to declared permissions. */
  get<T = unknown>(path: string, init?: RequestInit): Promise<T>
  /** POST request scoped to declared permissions. */
  post<T = unknown>(path: string, body?: unknown): Promise<T>
  /** PUT request scoped to declared permissions. */
  put<T = unknown>(path: string, body?: unknown): Promise<T>
  /** PATCH request scoped to declared permissions. */
  patch<T = unknown>(path: string, body?: unknown): Promise<T>
  /** DELETE request scoped to declared permissions. */
  del<T = unknown>(path: string): Promise<T>
}

export interface AppPermissions {
  api: string[]
  events: string[]
}

export interface AppInfo {
  name: string
  version: string
  permissions: AppPermissions
}

export interface AppSdkContextValue {
  api: AppApi
  info: AppInfo
  subscribe: (event: string, cb: (data: unknown) => void) => () => void
  navigate: (path: string) => void
  notify: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

export const AppSdkContext = createContext<AppSdkContextValue | null>(null)

export function useCtx(): AppSdkContextValue {
  const ctx = useContext(AppSdkContext)
  if (!ctx) throw new Error('useAppApi() must be used inside <AppApiProvider>')
  return ctx
}


function createScopedApi(allowedPaths: string[], appName: string): AppApi {
  const check = (path: string): string => {
    // Reject absolute and protocol-relative URLs to prevent SSRF. Backslashes
    // are rejected too: the URL parser treats `\` like `/`, so `/\evil.com` or
    // `\\evil.com` would otherwise be parsed as a protocol-relative authority.
    if (/^(?:https?:)?[/\\]{2}/i.test(path) || path.includes('\\')) {
      throw new Error(`[app-sdk] Absolute URLs are not allowed: ${path}`)
    }
    // Normalize BEFORE the allowlist check so `..` traversal cannot escape the
    // declared scope (e.g. `/api/apps/x/../../secret` → `/api/secret`).
    const parsed = new URL(path, 'http://localhost')
    const normalized = parsed.pathname
    const allowed = allowedPaths.some(p => normalized === p || normalized.startsWith(p.endsWith('/') ? p : p + '/'))
    if (!allowed) {
      throw new Error(`[app-sdk] App "${appName}" not permitted to access ${normalized}. Declared: [${allowedPaths.join(', ')}]`)
    }
    return normalized + parsed.search
  }

  const jsonFetch = async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const safePath = check(path)
    const res = await fetch(safePath, init)
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText)
      // A stale pre-owner session denial raises the dashboard's re-auth prompt
      // (installed by api/client); in a document without it — the vendored
      // iframe copy of this SDK — detection is a no-op and the throw below is
      // unchanged either way.
      noteStaleOwnerResponse(res.status, text)
      throw new Error(`API ${res.status}: ${text}`)
    }
    // An empty-body response is not JSON — res.json() would throw a SyntaxError
    // (e.g. a 204 No Content on DELETE, or a 200 with an empty body and no
    // Content-Length header). Read the body as text and only parse when it is
    // non-empty, so any empty body returns undefined regardless of status or
    // whether a Content-Length: 0 header was sent.
    if (res.status === 204 || res.status === 205) {
      return undefined as T
    }
    const text = await res.text()
    if (text.trim() === '') {
      return undefined as T
    }
    return JSON.parse(text) as T
  }

  return {
    get: (path, init) => jsonFetch(path, { ...init, method: 'GET' }),
    post: (path, body) => jsonFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body != null ? JSON.stringify(body) : undefined,
    }),
    put: (path, body) => jsonFetch(path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: body != null ? JSON.stringify(body) : undefined,
    }),
    patch: (path, body) => jsonFetch(path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: body != null ? JSON.stringify(body) : undefined,
    }),
    del: (path) => jsonFetch(path, { method: 'DELETE' }),
  }
}

/**
 * Stable defaults for the three props every caller was hand-writing identically.
 *
 * Module-scope constants, not inline literals: the provider memoizes its context
 * value on these by identity, so a fresh array or arrow per render would rebuild
 * the scoped API client on every repaint.
 */
const NO_EVENTS: string[] = []
/** An app that subscribes to nothing. Returns an unsubscribe because `useAppEvents`
 *  hands its result straight to React as the effect cleanup, and a cleanup that is a
 *  real function keeps that contract honest — React tolerates `undefined` there, so
 *  this is about the contract, not about avoiding a crash. */
const noopSubscribe = () => () => {}
/** The host's own toast bus, which AppHost and spec-builder each hand-wrote. */
const hostNotify = (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => {
  window.dispatchEvent(new CustomEvent('mc:notify', { detail: { message, ...opts } }))
}

/**
 * The scoped-API layer: a permission-fenced API client plus the host bridges,
 * published to every SDK hook.
 *
 * Mount this alone when the page already HAS identity — a builtin page does,
 * from `BuiltinAppRoute` — and mount `AppApiProvider` (which composes identity
 * with this) when it does not, which is the installed-app case.
 *
 * `navigateFn` stays a required injected prop and gets no default on purpose.
 * A default would mean this module importing a router, and the SDK does not own
 * routing: it is resolved to the host's real navigator by whoever mounts the
 * provider. `window.location.assign` would not do as a default either — it
 * reloads the whole dashboard.
 *
 * `appName` is optional and resolves explicit prop → identity context. Pass it
 * for a component that must render in isolation (its own unit test mounts it
 * with no route above); omit it on a page under `BuiltinAppRoute`, which
 * publishes the id the host minted.
 */
export function AppScopedApiProvider({
  allowedApiPaths,
  navigateFn,
  appName,
  appVersion = '0.0.0',
  allowedEvents = NO_EVENTS,
  subscribeFn = noopSubscribe,
  notifyFn = hostNotify,
  children,
}: {
  allowedApiPaths: string[]
  navigateFn: (path: string) => void
  appName?: string
  appVersion?: string
  allowedEvents?: string[]
  subscribeFn?: (event: string, cb: (data: unknown) => void) => () => void
  notifyFn?: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
  children: ReactNode
}) {
  const identity = useAppIdentity()
  const resolvedName = appName ?? identity?.appId
  if (!resolvedName) {
    // Loud rather than an empty name: `info.name` labels every permission
    // refusal the scoped client throws, and a nameless one is unattributable.
    throw new Error(
      '[app-sdk] <AppScopedApiProvider> could not resolve an app name. Render it under ' +
        'an <AppIdentityProvider> (a builtin page gets one from BuiltinAppRoute), or pass appName.',
    )
  }
  const apiKey = JSON.stringify(allowedApiPaths)
  const eventsKey = JSON.stringify(allowedEvents)
  const value = React.useMemo<AppSdkContextValue>(() => ({
    api: createScopedApi(allowedApiPaths, resolvedName),
    info: {
      name: resolvedName,
      version: appVersion,
      permissions: { api: allowedApiPaths, events: allowedEvents },
    },
    subscribe: subscribeFn,
    navigate: navigateFn,
    notify: notifyFn,
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [resolvedName, appVersion, apiKey, eventsKey, subscribeFn, navigateFn, notifyFn])

  return React.createElement(AppSdkContext.Provider, { value }, children)
}

