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
  /** Request with a JSON response, scoped to declared permissions. Body is passed through unchanged. */
  request<T = unknown>(path: string, init?: RequestInit): Promise<T>
  /** GET request scoped to declared permissions. */
  get<T = unknown>(path: string, init?: RequestInit): Promise<T>
  /** POST JSON request scoped to declared permissions. */
  post<T = unknown>(path: string, body?: unknown, init?: RequestInit): Promise<T>
  /** PUT JSON request scoped to declared permissions. */
  put<T = unknown>(path: string, body?: unknown, init?: RequestInit): Promise<T>
  /** PATCH JSON request scoped to declared permissions. */
  patch<T = unknown>(path: string, body?: unknown, init?: RequestInit): Promise<T>
  /** DELETE request scoped to declared permissions. */
  del<T = unknown>(path: string, init?: RequestInit): Promise<T>
}

/** HTTP failures from AppApi. Network, abort and JSON parsing errors retain their original types. */
export interface AppApiError extends Error {
  readonly status: number
  readonly body: string
}

class ScopedApiError extends Error implements AppApiError {
  constructor(readonly status: number, readonly body: string) {
    super(`API ${status}: ${body}`)
    this.name = 'AppApiError'
  }
}

export interface AppPermissions {
  api: string[]
  events: string[]
}

export interface AppInfo {
  name: string
  version: string
  permissions: AppPermissions
  /** Whether the app's host surface is currently visible. A routed page is
   *  always the visible surface (`true`); a body-owning side-panel tab stays
   *  mounted while hidden and reads this to pause polling or release global
   *  keys. Absent on hosts that predate the flag — treat `undefined` as `true`. */
  active?: boolean
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


function createScopedApi(allowedPaths: string[], appName: string, sessionKey?: string): AppApi {
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
    // Restricted-session checks read this header. Only the host may select it:
    // accepting an app's override could attribute a restricted write to another
    // session. A host without a binding cannot validate a caller-selected key.
    const headers = new Headers(init?.headers)
    if (sessionKey) {
      headers.set('X-Session-Key', sessionKey)
    } else if (headers.has('X-Session-Key')) {
      throw new Error('[app-sdk] X-Session-Key requires a host session binding')
    }
    const res = await fetch(safePath, { ...init, headers })
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText)
      // A stale pre-owner session denial raises the dashboard's re-auth prompt
      // (installed by api/client); in a document without it — the vendored
      // iframe copy of this SDK — detection is a no-op and the throw below is
      // unchanged either way.
      noteStaleOwnerResponse(res.status, text)
      throw new ScopedApiError(res.status, text)
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

  const jsonRequest = <T,>(path: string, method: string, body: unknown, init?: RequestInit): Promise<T> => {
    const headers = new Headers(init?.headers)
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
    return jsonFetch<T>(path, {
      ...init, method, headers,
      body: body != null ? JSON.stringify(body) : undefined,
    })
  }

  return {
    request: (path, init) => jsonFetch(path, init),
    get: (path, init) => jsonFetch(path, { ...init, method: 'GET' }),
    post: (path, body, init) => jsonRequest(path, 'POST', body, init),
    put: (path, body, init) => jsonRequest(path, 'PUT', body, init),
    patch: (path, body, init) => jsonRequest(path, 'PATCH', body, init),
    del: (path, init) => jsonFetch(path, { ...init, method: 'DELETE' }),
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
  active = true,
  subscribeFn = noopSubscribe,
  notifyFn = hostNotify,
  sessionKey,
  children,
}: {
  allowedApiPaths: string[]
  navigateFn: (path: string) => void
  appName?: string
  appVersion?: string
  allowedEvents?: string[]
  /** Whether the app's host surface is currently visible; published as
   *  `info.active`. A routed page is always visible, so `true` is the default. */
  active?: boolean
  subscribeFn?: (event: string, cb: (data: unknown) => void) => () => void
  notifyFn?: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
  /**
   * Session the hosted surface is scoped to, sent as `X-Session-Key` on every
   * scoped request. Omitted by a full-page surface, which is not session-scoped;
   * where the host knows the session, passing it is what lets the backend's
   * restricted-session guard fire, since that guard fails open on absence.
   */
  sessionKey?: string
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
    api: createScopedApi(allowedApiPaths, resolvedName, sessionKey),
    info: {
      name: resolvedName,
      version: appVersion,
      permissions: { api: allowedApiPaths, events: allowedEvents },
      active,
    },
    subscribe: subscribeFn,
    navigate: navigateFn,
    notify: notifyFn,
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [resolvedName, appVersion, apiKey, eventsKey, sessionKey, active, subscribeFn, navigateFn, notifyFn])

  return React.createElement(AppSdkContext.Provider, { value }, children)
}

