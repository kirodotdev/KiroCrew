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
  useEffect,
  useRef,
  useCallback,
  type ReactNode,
} from 'react'
// The identity layer this module composes with its own scoped-API layer. Kept in
// a separate file so a surface that needs identity WITHOUT a sandbox — every
// builtin page — can mount it on its own, and so it stays off this barrel (see
// the note at the bottom of this file on what the barrel publishes).
import { AppIdentityProvider, useAppIdentity, type AppOrigin } from './identity'
// The scoped-API layer, kept in its own module so it is NOT published on this
// barrel -- see the header of ./scopedApi for why.
import { useCtx, AppScopedApiProvider, type AppApi, type AppInfo } from './scopedApi'
// Re-exported as TYPES: the barrel<->vendor-stub parity gate counts value
// exports, and a type vanishes at runtime, so an app author keeps these names
// without the stub needing an entry it could not provide.
export type { AppApi, AppInfo, AppPermissions } from './scopedApi'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------




export interface AppTheme {
  mode: 'dark' | 'light'
  accent: string
  colorTheme: string
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Hooks (public API for app authors)
// ---------------------------------------------------------------------------

/** Permission-scoped API client. */
export function useAppApi(): AppApi {
  return useCtx().api
}

/** Subscribe to a real-time WebSocket event. Unsubscribes on unmount. */
// ---------------------------------------------------------------------------
// WebSocket event scope map — MIRRORS kiro_crew/dashboard/ws_event_scope.py.
// The gateway is authoritative; this table exists only so the SDK can tell an
// app author accurately whether a subscription will be delivered. Keep the
// three sets in sync with the Python tables (a completeness test on the Python
// side fails the build when a new event is unclassified there).
// ---------------------------------------------------------------------------

/** Tier 0 — always delivered to every connected client. */
const WS_TIER0_EVENTS = new Set(['dashboard', 'refresh', 'update_progress'])

/**
 * Slot-scoped events — they carry a `slot` field. An app token receives these
 * for slots it OWNS with no declaration at all; broader visibility (user
 * slots, another app's slots, all slots) needs a `slots:*` / `subagent:*`
 * scope.
 */
const WS_SLOT_SCOPED_EVENTS = new Set([
  // Chat content
  'chat_chunk', 'chat_thinking', 'chat_status', 'chat_message', 'chat_done',
  'chat_segment', 'chat_append', 'chat_message_update', 'chat_variant_switch',
  'chat.side_result', 'heartbeat', 'context_usage',
  // Tool / queue
  'tool_call', 'tool_result',
  'queue_push', 'queue_cancel', 'queue_edit', 'queue_pop', 'queue_reorder',
  'steer_push',
  // Slot metadata / lifecycle
  'slot_title', 'slot_clear', 'slot_agent_switch', 'todo_update', 'mcp_report_update',
  'activity_event', 'session_summary',
  // Voice
  'voice_chunk', 'voice_complete', 'voice_error',
  // Approvals and question cards
  'approval', 'approval_resolved', 'question_card',
  // Subagent lifecycle
  'subagent_spawn', 'subagent_done', 'subagent_tool', 'subagent_chunk',
  'subagent_snapshot', 'subagent_status', 'subagent_queued',
  'subagent_stalled', 'subagent_retrying', 'subagent_recovering',
  'subagent_injection_failed',
  // Slack-gateway driven, slot-scoped
  'autonudge_state', 'batch_finished', 'spawn_batch_started',
  // Workflows / misc
  'workflow_result_injected', 'refine',
])

/**
 * Subagent lifecycle events — the subset of slot-scoped events that
 * `subagent:*` scopes widen. Mirrors `_SUBAGENT_EVENTS` in the Python gate.
 */
const WS_SUBAGENT_EVENTS = new Set([
  'subagent_spawn', 'subagent_done', 'subagent_tool', 'subagent_chunk',
  'subagent_snapshot', 'subagent_status', 'subagent_queued',
  'subagent_stalled', 'subagent_retrying', 'subagent_recovering',
  'subagent_injection_failed',
])

/** Global events — no `slot` field; each needs an explicit scope declaration. */
const WS_GLOBAL_EVENT_TO_SCOPE: Record<string, string> = {
  notification: 'notification',
  notification_ack: 'notification',
  notification_unack: 'notification',
  notifications_clear: 'notification',
  notification_channel_settings: 'notification',
  sessions_restarting: 'sessions',
  yolo_expired: 'yolo',
  artifact_update: 'artifacts',
  'skills.pending_changed': 'skills',
  // Declared by its own literal name (already the correct per-event shape).
  workflow_run_event: 'workflow_run_event',
  log: 'log',
  browser_event: 'browser',
  // NOTE: `slots` is deliberately absent — the list re-push is always
  // delivered and filtered per app in the payload on the server
  // (state.py::_serialize_for_client), so it needs no declaration.
}

/** Result of the SDK subscription pre-check. */
type SubscribeCheckResult =
  | { level: 'ok' }
  | { level: 'own-only'; hint: string }
  /** A known event whose required scope is NOT declared — will not arrive. */
  | { level: 'denied'; hint: string }
  /** Not in the gate's tables at all — most likely a typo, possibly custom. */
  | { level: 'unknown'; hint: string }

/**
 * Predict whether the gateway will deliver `event` given the app's declared
 * `permissions.events`. Advisory only — the gateway decides.
 */
export function checkSubscribeAllowed(event: string, allowed: string[]): SubscribeCheckResult {
  if (WS_TIER0_EVENTS.has(event)) return { level: 'ok' }
  if (allowed.includes('*')) return { level: 'ok' }

  // Slot-scoped: own slots are always visible, so only hint when the author
  // likely wants wider visibility.
  if (WS_SLOT_SCOPED_EVENTS.has(event)) {
    // Match the scope FAMILY to the event family. `subagent:*` is an
    // independent dimension in the gate (`_subagent_visible`), so declaring
    // `subagent:user` widens subagent events only — it does not widen chat.
    // Treating them interchangeably would predict `ok` for a chat
    // subscription that the gateway delivers for own slots only, which is the
    // exact silent-loss trap this check exists to prevent.
    const isSubagentEvent = WS_SUBAGENT_EVENTS.has(event)
    const hasWideningScope = allowed.some((a) =>
      isSubagentEvent ? a.startsWith('subagent') || a.startsWith('slots:') : a.startsWith('slots:'),
    )
    if (hasWideningScope) return { level: 'ok' }
    return {
      level: 'own-only',
      hint:
        `Event "${event}" is slot-scoped: you will receive it for slots your app OWNS. ` +
        `To see other slots, add a scope to permissions.events — ` +
        (isSubagentEvent
          ? `e.g. "subagent:user", "subagent:all", or a "slots:*" scope.`
          : `e.g. "slots:user" or "slots:all" ("subagent:*" widens subagent events only).`),
    }
  }

  // The slots list re-push needs no declaration (payload-filtered server-side).
  if (event === 'slots') return { level: 'ok' }

  const requiredScope = WS_GLOBAL_EVENT_TO_SCOPE[event]
  if (requiredScope) {
    // A `<scope>:*` variant also satisfies the base scope. For notifications
    // the gateway picks between `notification` (own-app) and
    // `notification:system` (gateway-internal) from the payload's source_app,
    // which is not knowable at subscribe time — so any variant counts as ok
    // here and the runtime gate makes the per-event decision.
    const satisfied = allowed.some(
      (a) => a === requiredScope || a.startsWith(`${requiredScope}:`),
    )
    if (satisfied) return { level: 'ok' }
    return {
      level: 'denied',
      hint:
        `Event "${event}" requires permissions.events to include "${requiredScope}"` +
        (requiredScope === 'notification'
          ? `, "notification:system" for gateway-internal pushes (cron, send_message), ` +
            `or "notification:all" for every source.`
          : ` (or "${requiredScope}:all" for the broader variant).`),
    }
  }

  return {
    level: 'unknown',
    hint:
      `Event "${event}" is not recognized by the gateway's scope gate. If it is a ` +
      `custom event your app handles, ignore this warning; otherwise check for ` +
      `typos or update the SDK.`,
  }
}

/** app+event pairs already reported as own-slot-only, to keep the console useful. */
const ownOnlyLogged = new Set<string>()

export function useAppEvents(event: string, callback: (data: unknown) => void): void {
  const { subscribe, info } = useCtx()
  const cbRef = useRef(callback)
  cbRef.current = callback
  const appName = info.name
  const allowedEvents = info.permissions.events

  useEffect(() => {
    const check = checkSubscribeAllowed(event, allowedEvents)
    if (check.level === 'own-only') {
      // Own-slot-only is the DEFAULT, correctly-working configuration, so this
      // is informational. Log once per app+event for the process lifetime:
      // remounts would otherwise repeat a call-to-action hint about a setup
      // that is not broken, drowning out real signal.
      const seenKey = `${appName}::${event}`
      if (!ownOnlyLogged.has(seenKey)) {
        ownOnlyLogged.add(seenKey)
        // eslint-disable-next-line no-console -- intentional permission diagnostic
        console.info(
          `[app-sdk] App "${appName}" subscribing to "${event}" (own-slot only). ${check.hint}`,
        )
      }
    } else if (check.level === 'denied') {
      // eslint-disable-next-line no-console -- intentional permission diagnostic
      console.warn(
        `[app-sdk] App "${appName}" subscribed to "${event}" but the manifest denies it. ` +
          check.hint,
      )
    } else if (check.level === 'unknown') {
      // Separate prefix from the denied case: asserting denial for a custom
      // event and then retracting it in the hint leaves the author unable to
      // tell whether anything is actually wrong.
      // eslint-disable-next-line no-console -- intentional permission diagnostic
      console.warn(`[app-sdk] App "${appName}" subscribed to "${event}": ` + check.hint)
    }
    // Always register: the gateway is authoritative and silently drops what
    // this app may not receive. Not returning early means a manifest fix takes
    // effect on the next event without needing a component re-render.
    return subscribe(event, (data: unknown) => cbRef.current(data))
  }, [event, subscribe, appName, allowedEvents])
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

/**
 * Identity + scoped API in one, for a surface that has neither: an installed app
 * mounted by `AppHost`.
 *
 * Prop contract unchanged, so every existing caller keeps working; `origin` is
 * new and defaults to `'external'` because that is what an installed app is.
 *
 * It publishes identity only when there is NONE in context. A builtin page's
 * identity is minted by the host and carries `origin: 'builtin'`; shadowing it
 * with this provider's `'external'` default would silently revoke that page's
 * host namespace — a capability disappearing with no error, which is the exact
 * failure the identity layer exists to prevent.
 */
export function AppApiProvider({
  appName,
  appVersion = '0.0.0',
  allowedApiPaths,
  allowedEvents,
  subscribeFn,
  navigateFn,
  notifyFn,
  origin = 'external',
  children,
}: {
  appName: string
  appVersion?: string
  allowedApiPaths: string[]
  navigateFn: (path: string) => void
  /** Optional since the scoped layer defaults them; see AppScopedApiProvider. */
  allowedEvents?: string[]
  subscribeFn?: (event: string, cb: (data: unknown) => void) => () => void
  notifyFn?: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
  origin?: AppOrigin
  children: ReactNode
}) {
  const existing = useAppIdentity()
  const scoped = React.createElement(AppScopedApiProvider, {
    appName,
    appVersion,
    allowedApiPaths,
    allowedEvents,
    subscribeFn,
    navigateFn,
    notifyFn,
    children,
  })
  if (existing) return scoped
  return React.createElement(AppIdentityProvider, { appId: appName, origin, children: scoped })
}

export { useChatSession } from './useChatSession'
export { default as ChatPanel } from './ChatPanel'
// The marker protocol, so an embedding app can interpret `[OPTIONS:]` /
// `[STEERING …]` without rendering our transcript components.
export * from './protocol'
export { default as ChatEmbed } from './ChatEmbed'
export { default as ChatMessageList } from './ChatMessageList'
// The transcript's row registry, so an app can add a row type or replace one
// instead of forking the message list.
export {
  defaultMessageRenderers,
  GROUPED_ROLES,
  mergeRenderers,
  resolveRenderer,
  ToolCallPill,
} from './messageRenderers'
export type { MessageRenderer, MessageRenderContext } from './messageRenderers'
// `useComposerDraft` is deliberately NOT exported here yet. It is owned by this
// directory and consumed from `./useComposerDraft` by name, which serves every
// in-tree surface; adding it to this barrel would publish it through the vendor
// stub to third-party apps and freeze the contract before its richest consumer —
// the main composer, with configurable send keys and per-slot persisted drafts —
// has exercised it. Publishing later is additive; un-publishing is a break.
