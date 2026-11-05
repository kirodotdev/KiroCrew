/**
 * The main pane of an app-drawn session (`contributes.sessionKinds`).
 *
 * A slot carrying `session_kind: { app, kind, object }` is drawn by that app: the
 * kind's `entry` mounts through the in-process `AppHost` in place of the transcript
 * and composer, and reads its binding with the App SDK's `useSessionPane()`. The
 * title row is kept; the pane starts below it. The slot's tool, spawn and worker
 * approvals stay host controls, drawn over the pane by `SessionPaneApprovals`.
 *
 * When the app cannot draw it (uninstalled, disabled, skipped at boot, or no longer
 * declaring the kind) the session falls back to its transcript, read-only, with a
 * notice. Nothing is deleted, so re-enabling the app brings the pane back.
 */
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Package, RefreshCw } from 'lucide-react'
import AppHost from '../../../components/AppHost'
import ErrorNotice from '../../../components/ErrorNotice'
import { Btn } from '../../../components/ui'
import { appDisplayName } from '../../../components/appstore/appManifest'
import type { InstalledApp } from '../../../components/appstore/types'
import { useInstalledApps } from '../../../hooks/panelTabRegistry'
import {
  sessionKindDescriptor,
  type SessionKindAppRecord,
  type SessionKindDescriptor,
} from '../../../hooks/sessionKindRegistry'
import type { SessionPane } from '../../../app-sdk/sessionPane'
import type { ChatSlot, ChatSlotSessionKind } from '../../../types'
import { i18nT } from '../../../i18n/t'
import { errMessage } from '../../../utils/thunkError'
import { TOP_OVERLAY_ATTR } from '../browserLiveClearance'
import SessionPaneApprovals from './SessionPaneApprovals'

/** What the chat pane draws for the active slot. */
export type SessionKindView =
  /** An ordinary chat: transcript and composer. */
  | { state: 'none' }
  /** Bound, and the app list has not answered yet: draw neither the chat nor the pane. */
  | { state: 'pending' }
  /** Bound, and the app draws it. */
  | { state: 'pane'; slotKey: string; app: InstalledApp; descriptor: SessionKindDescriptor; binding: ChatSlotSessionKind }
  /** Bound, and the app cannot draw it: the transcript, read-only. */
  | { state: 'unavailable'; appLabel: string }
  /** Bound, and the app list failed to load, so whether the app draws it is unknown. */
  | { state: 'load-failed'; error: unknown; retry: () => void }

/** A binding as the slot payload carries it, or null when it is not a usable one.
 *  The payload crosses the wire, so its shape is checked rather than trusted. */
function bindingOf(slot: ChatSlot | undefined): ChatSlotSessionKind | null {
  const b = slot?.session_kind
  if (!b || typeof b !== 'object') return null
  const { app, kind, object } = b as Partial<ChatSlotSessionKind>
  if (typeof app !== 'string' || !app || typeof kind !== 'string' || !kind || typeof object !== 'string' || !object) return null
  return { app, kind, object }
}

/** Decide how the active slot's pane renders. One `['apps']` observer, shared. */
export function useSessionKindView(slot: ChatSlot | undefined): SessionKindView {
  const { apps, isPending, isLoadingError, error, refetch } = useInstalledApps()
  const binding = bindingOf(slot)
  const slotKey = slot?.key
  const app = binding?.app
  const kind = binding?.kind
  const object = binding?.object
  return useMemo<SessionKindView>(() => {
    if (!slotKey || !app || !kind || !object) return { state: 'none' }
    if (isLoadingError) return { state: 'load-failed', error, retry: refetch }
    if (isPending) return { state: 'pending' }
    const record = apps.find(a => a.name === app)
    const descriptor = sessionKindDescriptor(apps as unknown as SessionKindAppRecord[], app, kind)
    if (!record || !descriptor) {
      return { state: 'unavailable', appLabel: record ? appDisplayName(record) : app }
    }
    return { state: 'pane', slotKey, app: record, descriptor, binding: { app, kind, object } }
  }, [slotKey, app, kind, object, apps, isPending, isLoadingError, error, refetch])
}

/**
 * The app's pane, filling the chat pane below the title row. Keyed by the caller
 * on the slot, so moving between two sessions of one kind remounts the app rather
 * than handing one session's state to the next.
 */
export function AppSessionPane({ slotKey, app, descriptor, binding }: {
  slotKey: string
  app: InstalledApp
  descriptor: SessionKindDescriptor
  binding: ChatSlotSessionKind
}) {
  const sessionKey = `dashboard:${slotKey}`
  const sessionPane = useMemo<SessionPane>(
    () => ({ slotKey, sessionKey, app: binding.app, kind: binding.kind, object: binding.object }),
    [slotKey, sessionKey, binding.app, binding.kind, binding.object],
  )
  // The title row is an absolute overlay over the top of the pane (the transcript
  // scrolls under it). An app surface does not, so it starts below the row's
  // measured height, which changes with the viewport and the title's own wrap.
  const rootRef = useRef<HTMLDivElement>(null)
  const [topInset, setTopInset] = useState(0)
  useLayoutEffect(() => {
    const overlay = rootRef.current?.parentElement?.querySelector<HTMLElement>(`[${TOP_OVERLAY_ATTR}]`)
    if (!overlay) return
    const measure = () => setTopInset(overlay.offsetHeight)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(overlay)
    return () => ro.disconnect()
  }, [])
  return (
    <div
      ref={rootRef}
      className="relative flex flex-col flex-1 min-h-0 overflow-hidden"
      style={{ paddingTop: topInset }}
      data-testid="app-session-pane"
      data-session-kind={`${binding.app}:${binding.kind}`}
    >
      {/* Its own stacking context, so no z-index the app sets can lift it over
          the host's approval strip below. */}
      <div className="relative z-0 flex flex-col flex-1 min-h-0">
        <AppHost
          app={app as Parameters<typeof AppHost>[0]['app']}
          entry={descriptor.entry}
          active
          sessionKey={sessionKey}
          sessionPane={sessionPane}
        />
      </div>
      <SessionPaneApprovals slotKey={slotKey} />
    </div>
  )
}

/**
 * The read-only fallback's notice, drawn where the composer would be: the
 * transcript above it is the session as it stands, and nothing here writes to it.
 */
export function AppSessionFallbackNotice({ view }: { view: Extract<SessionKindView, { state: 'unavailable' | 'load-failed' }> }) {
  const navigate = useNavigate()
  if (view.state === 'load-failed') {
    // A failed read with nothing typed here to lose, so the hand-off is on.
    return (
      <div className="px-4 pb-3 pt-2 shrink-0" data-testid="app-session-fallback">
        <ErrorNotice
          askAgent
          testId="app-session-apps-load-error"
          title={i18nT('pages.chat.appSessionPane.apps_load_failed')}
          message={errMessage(view.error) || String(view.error)}
          footer={(
            <div className="flex gap-2 mt-2">
              <Btn onClick={() => view.retry()}><RefreshCw size={14} /> {i18nT('components.appHost.retry')}</Btn>
            </div>
          )}
        />
      </div>
    )
  }
  return (
    <div className="px-4 pb-3 pt-2 shrink-0" data-testid="app-session-fallback">
      <div
        role="status"
        data-testid="app-session-unavailable"
        className="bg-bg-elevated border rounded-lg p-3 flex items-center gap-3"
        style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}
      >
        <span className="flex-1 min-w-0 text-sm text-text">
          {i18nT('pages.chat.appSessionPane.unavailable_read_only', { app: view.appLabel })}
        </span>
        <Btn onClick={() => navigate('/apps')}><Package size={14} /> {i18nT('components.appHost.apps')}</Btn>
      </div>
    </div>
  )
}
