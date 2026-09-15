/**
 * SessionControlHost — renders one app-contributed session control in a popover.
 *
 * Mirrors AppHost's loading contract exactly (lazy ESM import → error boundary
 * → Suspense → AppApiProvider), with three differences that matter because this
 * renders inside the composer rather than on a page of its own:
 *
 * 1. **Failure must be local.** A page that throws costs the user that page. A
 *    control that throws sits on the path of every turn, so the boundary here
 *    renders a compact inline notice and leaves the chat untouched — it never
 *    unmounts anything but itself.
 * 2. **The session identity is the payload.** The control is handed
 *    `{ sessionKey, folderId, folderName, cwd }` — the grain apps mis-bind on
 *    today, and nothing more. That is the whole reason the slot exists: an app
 *    cannot discover the active session key from its own page. Fields no
 *    control has named a use for are deliberately absent, because this is a
 *    third-party contract and adding one later is cheaper than removing it.
 * 3. **It is keyed on the session.** Switching chats remounts the control, so a
 *    control holding per-session state cannot leak it across a switch.
 *
 * @module components/SessionControlHost
 */
import React, { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import ErrorNotice from './ErrorNotice'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { AppApiProvider } from '../app-sdk'
import { i18nT } from '../i18n/t'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'

// ---------------------------------------------------------------------------
// Session identity handed to the control
// ---------------------------------------------------------------------------

export interface SessionControlContext {
  /** Session key, e.g. `dashboard:chat-2-1787502679`. Empty pre-slot. */
  sessionKey: string
  /**
   * Folder the chat is filed in, or '' when it is at the top level.
   * A folder is a dashboard grouping with its own id — not a directory — so an
   * app storing a per-folder setting must key on this and not on `cwd`.
   */
  folderId?: string
  /** Folder's display name, for an app that wants to name it back to the user. */
  folderName?: string
  /** Working directory recorded for the session, when known. */
  cwd: string
}

export interface SessionControlHostProps {
  control: ResolvedSessionControl
  session: SessionControlContext
  anchorRect: DOMRect | null
  onClose: () => void
}

// ---------------------------------------------------------------------------
// Error boundary — deliberately narrow
// ---------------------------------------------------------------------------

interface EBProps {
  label: string
  appName: string
  onReset: () => void
  children: React.ReactNode
}
interface EBState {
  error: Error | null
}

class ControlErrorBoundary extends React.Component<EBProps, EBState> {
  state: EBState = { error: null }

  static getDerivedStateFromError(error: Error): EBState {
    return { error }
  }

  componentDidCatch(error: Error) {
    // Named loudly: a third-party control failing inside the composer is worth
    // a console entry even though it is contained.
    // eslint-disable-next-line no-console -- surface control crashes for debugging
    console.error(`[SessionControl] ${this.props.appName} control crashed:`, error)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="p-3 w-[280px]">
        <ErrorNotice
          title={i18nT('components.sessionControlHost.failed', { label: this.props.label })}
          message={this.state.error.message}
          askAgent
          className="mb-2"
        />
        <p className="text-[10px] text-muted mb-2">
          {i18nT('components.sessionControlHost.chat_unaffected')}
        </p>
        <button
          className="inline-flex items-center gap-1.5 text-[11px] text-muted hover:text-text px-2 py-1 rounded border border-border bg-transparent cursor-pointer"
          onClick={() => {
            this.setState({ error: null })
            this.props.onReset()
          }}
          title={i18nT('components.sessionControlHost.retry_loading', { label: this.props.label })}
          aria-label={i18nT('components.sessionControlHost.retry_loading', { label: this.props.label })}
        >
          <RefreshCw size={11} /> {i18nT('components.sessionControlHost.retry')}
        </button>
      </div>
    )
  }
}

// ---------------------------------------------------------------------------
// Host
// ---------------------------------------------------------------------------

export default function SessionControlHost({
  control,
  session,
  anchorRect,
  onClose,
}: SessionControlHostProps) {
  const [resetKey, setResetKey] = useState(0)
  const popRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()
  // Tracks IME composition anywhere inside the dialog, so the Escape handler
  // below can tell "dismiss the dialog" from "cancel a composition candidate".
  // Keyed on the popover being mounted, which is exactly when Escape is ours.
  const imeLatch = useDocumentImeLatch(!!anchorRect)

  const bundlePath = `/apps/${control.appName}/ui/${control.entryPoint}`

  const LazyControl = useMemo(
    () =>
      lazy(() =>
        import(/* @vite-ignore */ bundlePath).catch(err => {
          // eslint-disable-next-line no-console -- surface bundle load failures for debugging
          console.error(
            `[SessionControl] failed to load ${control.appName}/${control.id} from ${bundlePath}:`,
            err
          )
          // Resolve with a component rather than rejecting: a rejected lazy
          // throws to the nearest boundary on every render attempt, and the
          // message here is more useful than a bare chunk-load error.
          return {
            default: () => (
              <div className="p-3 w-[280px]">
                <ErrorNotice
                  title={i18nT('components.sessionControlHost.could_not_load', { label: control.label })}
                  message={String(err?.message || err)}
                  askAgent
                  messageClassName="break-all"
                />
              </div>
            ),
          }
        })
      ),
    [bundlePath, resetKey] // eslint-disable-line react-hooks/exhaustive-deps
  )

  // Close on outside click / Escape, matching the sibling pill popovers.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      // The triggering chip is not "outside". mousedown fires before click, so
      // closing here would let the chip's own toggle re-open it — clicking an
      // open chip would flicker rather than dismiss.
      if (target?.closest?.('[data-session-control-chip]')) return
      if (popRef.current && !popRef.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // An IME user presses Escape to cancel a composition candidate, not to
      // dismiss the dialog — and closing here unmounts the control, discarding
      // an unsaved draft with nothing to restore it. The latch owns that
      // distinction (and the stopPropagation/preventDefault split that goes
      // with declining a key), so a declined Escape must simply not close.
      if (!imeLatch.claimKey(e)) return
      onClose()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose, imeLatch])

  const subscribeFn = useCallback((event: string, cb: (data: unknown) => void) => {
    const handler = (e: Event) => cb((e as CustomEvent).detail)
    window.addEventListener(`mc:app:${event}`, handler)
    return () => window.removeEventListener(`mc:app:${event}`, handler)
  }, [])

  const navigateFn = useCallback((path: string) => {
    // Navigating away from the composer closes the popover first, otherwise it
    // would linger over the destination. Uses react-router `navigate`, mirroring
    // AppHost's own navigateFn — the dashboard has no `mc:navigate` listener.
    onClose()
    navigate(path)
  }, [onClose, navigate])

  const notifyFn = useCallback(
    (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => {
      window.dispatchEvent(new CustomEvent('mc:notify', { detail: { message, ...opts } }))
    },
    []
  )

  if (!anchorRect) return null

  // Anchor above the trigger, clamped into the viewport — same approach the
  // approval-mode dropdown uses. The width is clamped too: a fixed 340 overflows
  // any viewport narrower than 356, where `left` has already bottomed out at its
  // 8px minimum and cannot absorb the difference, so the right edge would clip
  // off-screen with no way to scroll to it. Floored at 0 so an absurd viewport
  // (jsdom defaults, a measurement taken mid-teardown) cannot yield a negative
  // width, which is an invalid CSS length rather than a narrow popover.
  const width = Math.max(0, Math.min(340, window.innerWidth - 16))
  const left = Math.max(8, Math.min(anchorRect.left, window.innerWidth - width - 8))
  const bottom = window.innerHeight - anchorRect.top + 6

  return createPortal(
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- stopPropagation only: the dialog is a container that isolates global chords from the control inside it, so there is no gesture here to mirror with a keyboard equivalent. The control's own interactive elements carry their own handlers and are unaffected.
    <div
      ref={popRef}
      role="dialog"
      aria-label={i18nT('components.sessionControlHost.dialog_label', {
        label: control.label,
        appName: control.appDisplayName,
      })}
      className="fixed z-[9999] animate-slide-up rounded-lg bg-bg-elevated border border-border shadow-lg overflow-hidden"
      style={{ bottom, left, width }}
      onKeyDown={e => {
        // Global chat-jump chords are deliberately NOT input-gated for digits
        // (`useKeyboardShortcuts.ts:763`, "Digits keep firing in inputs"), and a
        // slot switch unmounts this host — so Ctrl/Alt+digit while typing in a
        // control would switch chats and discard an unsaved draft with nothing to
        // restore it. Isolate every key here EXCEPT Escape, which must still
        // reach the document listener above that closes the popover.
        //
        // This sits on the dialog rather than on the window handler because the
        // control's own key handlers are React descendants and therefore already
        // ran by the time this fires: isolating here costs the control nothing,
        // while stopping the native event before it reaches `window` is what
        // actually prevents the switch.
        if (e.key !== 'Escape') e.stopPropagation()
      }}
    >
      <ControlErrorBoundary
        label={control.label}
        appName={control.appName}
        onReset={() => setResetKey(k => k + 1)}
      >
        <AppApiProvider
          appName={control.appName}
          appVersion={control.appVersion}
          allowedApiPaths={control.allowedApi}
          allowedEvents={control.allowedEvents}
          subscribeFn={subscribeFn}
          navigateFn={navigateFn}
          notifyFn={notifyFn}
          /* The identity the control is scoped to, sent as `X-Session-Key` on
             every scoped request. Without it the backend's restricted-session
             guard fails open, so a control opened in an incognito chat would be
             allowed the persistent writes incognito exists to deny. The control
             already receives this same key as a prop; this is the half the
             SERVER needs. */
          sessionKey={session.sessionKey}
        >
          <Suspense
            fallback={
              <div className="p-3 text-[11px] text-muted">
                {i18nT('components.sessionControlHost.loading', { label: control.label })}
              </div>
            }
          >
            {/* Keyed on the session: a control holding per-session state must
                not carry it across a chat switch. */}
            <LazyControl key={session.sessionKey} session={session} onClose={onClose} />
          </Suspense>
        </AppApiProvider>
      </ControlErrorBoundary>
    </div>,
    document.body
  )
}
