/**
 * @kirocrew/app-sdk — lightweight SDK for KiroCrew apps.
 *
 * Provides React hooks backed by a context that AppHost sets up.
 * Apps import these hooks to access the KiroCrew API, real-time events,
 * theme, and navigation — all permission-scoped.
 *
 * This module lives inside the KiroCrew frontend for now. When we publish
 * it as a standalone package, apps will `import { useAppApi } from '@kirocrew/app-sdk'`
 * and the import map will resolve it to the host's vendored copy.
 */
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useCallback,
  type ReactNode,
} from 'react'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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

export interface AppTheme {
  mode: 'dark' | 'light'
  accent: string
  colorTheme: string
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

interface AppSdkContextValue {
  api: AppApi
  info: AppInfo
  subscribe: (event: string, cb: (data: unknown) => void) => () => void
  navigate: (path: string) => void
  notify: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

const AppSdkContext = createContext<AppSdkContextValue | null>(null)

function useCtx(): AppSdkContextValue {
  const ctx = useContext(AppSdkContext)
  if (!ctx) throw new Error('useAppApi() must be used inside <AppApiProvider>')
  return ctx
}

// ---------------------------------------------------------------------------
// Hooks (public API for app authors)
// ---------------------------------------------------------------------------

/** Permission-scoped API client. */
export function useAppApi(): AppApi {
  return useCtx().api
}

/** Subscribe to a real-time WebSocket event. Unsubscribes on unmount. */
export function useAppEvents(event: string, callback: (data: unknown) => void): void {
  const { subscribe, info } = useCtx()
  const cbRef = useRef(callback)
  cbRef.current = callback

  useEffect(() => {
    if (!info.permissions.events.includes(event) && !info.permissions.events.includes('*')) {
      // eslint-disable-next-line no-console -- intentional permission diagnostic
      console.warn(`[app-sdk] App "${info.name}" not permitted to subscribe to event "${event}"`)
      return
    }
    return subscribe(event, (data: unknown) => cbRef.current(data))
  }, [event, subscribe, info])
}

/** Reactive theme from the host. */
export function useTheme(): AppTheme {
  // Read directly from DOM — apps are in the same tree so CSS vars are live.
  // This avoids needing to thread theme through context.
  const getTheme = useCallback((): AppTheme => {
    const root = document.documentElement
    return {
      mode: (root.dataset.theme as 'dark' | 'light') || 'dark',
      accent: getComputedStyle(root).getPropertyValue('--accent').trim(),
      colorTheme: root.dataset.colorTheme || 'default',
    }
  }, [])

  // Re-render on theme change via MutationObserver
  const [theme, setTheme] = React.useState(getTheme)
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(getTheme()))
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme', 'data-color-theme', 'style'],
    })
    return () => observer.disconnect()
  }, [getTheme])

  return theme
}

/** App metadata. */
export function useAppInfo(): AppInfo {
  return useCtx().info
}

/** Navigate to a KiroCrew route (host-controlled). */
export function useNavigate(): (path: string) => void {
  return useCtx().navigate
}

/** Show a toast notification in the host. */
export function useNotify(): (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void {
  return useCtx().notify
}

/**
 * Set a badge count on this app's sidebar nav item.
 *
 * The host listens for `mc:app:badge` CustomEvents and updates the
 * nav item accordingly.  Pass 0 or undefined to clear the badge.
 */
export function useNavBadge(): (count: number) => void {
  const { info } = useCtx()
  return useCallback((count: number) => {
    window.dispatchEvent(new CustomEvent('mc:app:badge', {
      detail: { appName: info.name, count },
    }))
  }, [info.name])
}

// ---------------------------------------------------------------------------
// Chat Launcher
// ---------------------------------------------------------------------------

export interface ChatLaunchOptions {
  /** Agent name to use for the session. */
  agent?: string
  /** Initial message to send to the agent. */
  message?: string
}

/** Launch intent written to a global slot for ChatPage to consume on mount. */
interface ChatLaunchIntent extends ChatLaunchOptions {
  ts: number
}

/**
 * Launch a chat session in the host dashboard.
 *
 * Writes launch intent to a lightweight global slot, then navigates to
 * /chat. ChatPage consumes the intent on mount — no timing issues,
 * no Redux coupling, no API calls. Same decoupled pattern as useNavBadge.
 */
export function useChatLauncher(): {
  openChat: (opts?: ChatLaunchOptions) => void
} {
  const { navigate } = useCtx()

  const openChat = useCallback((opts: ChatLaunchOptions = {}) => {
    ;(window as Window & { __mc_chat_launch?: ChatLaunchIntent }).__mc_chat_launch = {
      agent: opts.agent,
      message: opts.message,
      ts: Date.now(),
    }
    navigate('/chat')
  }, [navigate])

  return { openChat }
}

// ---------------------------------------------------------------------------
// Provider (used by AppHost, not by apps directly)
// ---------------------------------------------------------------------------

// Need React for useState in useTheme — import it here to avoid
// adding it to the top-level imports (apps get React from import map)
import React from 'react'

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

export function AppApiProvider({
  appName,
  appVersion = '0.0.0',
  allowedApiPaths,
  allowedEvents,
  subscribeFn,
  navigateFn,
  notifyFn,
  children,
}: {
  appName: string
  appVersion?: string
  allowedApiPaths: string[]
  allowedEvents: string[]
  subscribeFn: (event: string, cb: (data: unknown) => void) => () => void
  navigateFn: (path: string) => void
  notifyFn: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
  children: ReactNode
}) {
  const apiKey = JSON.stringify(allowedApiPaths)
  const eventsKey = JSON.stringify(allowedEvents)
  const value = React.useMemo<AppSdkContextValue>(() => ({
    api: createScopedApi(allowedApiPaths, appName),
    info: {
      name: appName,
      version: appVersion,
      permissions: { api: allowedApiPaths, events: allowedEvents },
    },
    subscribe: subscribeFn,
    navigate: navigateFn,
    notify: notifyFn,
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [appName, appVersion, apiKey, eventsKey, subscribeFn, navigateFn, notifyFn])

  return React.createElement(AppSdkContext.Provider, { value }, children)
}

export { useChatSession } from './useChatSession'
export { default as ChatPanel } from './ChatPanel'
export { default as ChatEmbed } from './ChatEmbed'
export { default as ChatMessageList } from './ChatMessageList'
