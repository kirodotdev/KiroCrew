/**
 * The session an app-drawn main pane is bound to (`contributes.sessionKinds`).
 *
 * `AppHost` publishes it when ChatPage mounts a kind's `entry` in place of the
 * transcript and composer; every other host publishes `null`, so an app bundle
 * that serves both a routed page and a session pane can tell which one it is.
 *
 * Kept off the barrel except for the hook: the provider is the host's to set, and
 * an app that could publish its own binding could claim another session's.
 */
import { createContext, createElement, useContext, useMemo, type ReactNode } from 'react'

/** What a session pane is drawing: the chat slot, its session identity, and the
 *  `{ app, kind, object }` binding the server minted at create. */
export interface SessionPane {
  /** The dashboard slot key (`chat-<n>-<ts>`). */
  slotKey: string
  /** The session identity scoped requests carry (`dashboard:<slotKey>`). */
  sessionKey: string
  /** The app that declares the kind. */
  app: string
  /** The kind's `id` in that app's manifest. */
  kind: string
  /** Opaque id of the object this session is about, minted by the server. */
  object: string
}

const SessionPaneContext = createContext<SessionPane | null>(null)

/** Host-side provider. A nested host with no binding passes `null`, which resets
 *  the value, so a pane's binding never leaks into an unrelated app surface. */
export function SessionPaneProvider({ value, children }: { value: SessionPane | null | undefined; children: ReactNode }) {
  const slotKey = value?.slotKey
  const sessionKey = value?.sessionKey
  const app = value?.app
  const kind = value?.kind
  const object = value?.object
  // Field-wise memo: a parent that rebuilds the object each render must not
  // re-render every consumer of the hook.
  const stable = useMemo<SessionPane | null>(
    () => (slotKey && sessionKey && app && kind && object ? { slotKey, sessionKey, app, kind, object } : null),
    [slotKey, sessionKey, app, kind, object],
  )
  return createElement(SessionPaneContext.Provider, { value: stable }, children)
}

/**
 * The session this app surface is drawing as its main pane, or `null` when it is
 * mounted anywhere else (a routed page, a side-panel tab, outside any host).
 */
export function useSessionPane(): SessionPane | null {
  return useContext(SessionPaneContext)
}
