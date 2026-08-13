import { useEffect, useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { AlertTriangle, Download, Loader2, X } from 'lucide-react'

import { i18nT } from '../i18n/t'
import type { UpdateState } from '../hooks/useUpdateSubscription'
/**
 * In-app "update ready" modal for the packaged desktop app.
 *
 * Consumes the shared ['update-state'] query cache populated by the
 * useUpdateSubscription hook (mounted once in App.tsx). When an update has
 * finished downloading we surface this modal so the user can restart-and-
 * install on their terms. "Restart & Update" calls back into Electron, which
 * stops the bundled gateway gracefully before ShipIt swaps the .app bundle,
 * then relaunches.
 *
 * Replayed states (payload.replayed — seeded from getInfo() after a renderer
 * reload) never open the modal: the user already saw and possibly dismissed
 * this prompt before the reload, and a staged build re-offers itself through
 * the About card and nav dot. Only a LIVE 'downloaded' event interrupts.
 *
 * No-ops entirely in the browser (query cache never gets populated without
 * the Electron preload), so it's safe to mount unconditionally in App.
 */

type UpdateAPI = {
  install: () => Promise<unknown>
}

function getUpdateApi(): UpdateAPI | undefined {
  return (window as unknown as { updateAPI?: UpdateAPI }).updateAPI
}

export default function UpdateModal() {
  const { data: update } = useQuery<UpdateState | null>({
    queryKey: ['update-state'],
    queryFn: () => null,
    enabled: false, // populated by useUpdateSubscription (App.tsx)
    staleTime: Infinity,
  })

  const [dismissed, setDismissed] = useState(false)
  // Re-open on each fresh "downloaded" event (version change resets dismiss).
  // Replayed payloads are excluded from both the reset and the open: a reload
  // must not undo the user's dismissal of this same staged version.
  const [lastVersion, setLastVersion] = useState<string | undefined>(undefined)
  if (update?.state === 'downloaded' && !update.replayed && update.version !== lastVersion) {
    setLastVersion(update.version)
    setDismissed(false)
  }

  const installMutation = useMutation({ mutationFn: () => getUpdateApi()!.install() })
  // install() resolves as soon as the install is DISPATCHED — on macOS the
  // platform installer then works for several seconds before the app quits.
  // Keying `disabled` on `isPending` alone lets the button re-arm during that
  // window, so the user sees a clickable "Restart & Update" followed by an
  // unexplained quit — which reads as a crash.
  const installing = installMutation.isPending || installMutation.isSuccess

  // A dispatched install normally ends in the app quitting, so isSuccess is a
  // fine proxy for "about to restart" -- EXCEPT when the main process aborts
  // the handoff (stage invalidated mid-dispatch) and the app keeps running.
  // Without a reset, the stale isSuccess keeps `installing` true forever: the
  // next downloaded version reopens this modal with every button disabled and
  // no way out short of a reload. The dispatch is only "still live" while the
  // state is 'installing' or 'downloaded' FOR THE VERSION the user clicked
  // (tracked below) -- keying on the version matters because the IPC
  // resolution can land after the abort/supersede states have already been
  // pushed, at which point a bare state check reads the NEW version's
  // 'downloaded' as the old dispatch still running.
  const [installFor, setInstallFor] = useState<string | undefined>(undefined)
  const state = update?.state
  const stateVersion = update?.version
  useEffect(() => {
    if (!installMutation.isSuccess) return
    const dispatchStillLive = state === 'installing'
      || (state === 'downloaded' && stateVersion === installFor)
    if (!dispatchStillLive) installMutation.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset identity is stable; keying on state transition
  }, [state, stateVersion, installFor, installMutation.isSuccess])

  const open = !!update && update.state === 'downloaded' && !update.replayed && !dismissed

  // Escape dismisses the modal (unless an install is in flight), matching the
  // backdrop-click affordance and keeping the overlay keyboard-accessible.
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !installing) setDismissed(true)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, installing])

  // A live 'installing' push takes over the whole screen: the gateway is
  // being stopped ON PURPOSE, so every surface underneath is about to look
  // dead (offline pill, failed requests) — hiding them is the point. There is
  // deliberately no dismiss affordance and no Escape handling: the app
  // relaunches itself on success.
  //
  // A FAILED install must not simply unmount this surface: the user is
  // staring at a full-screen "Installing update…" and nothing else, so a
  // silent vanish leaves them unable to tell failed from succeeded (the
  // detailed failure card lives in Settings > About, which nothing points
  // them at). Render the failure in place instead, on the same surface, with
  // a dismiss affordance — the gateway is being restored by the main process
  // (onInstallFailed), so dismissing returns to a working dashboard.
  const installFailed = update?.state === 'error' && update.phase === 'install'
  if ((update?.state === 'installing' || installFailed) && !dismissed) {
    return (
      <div
        className="fixed inset-0 z-[100] bg-bg/95 backdrop-blur-md flex flex-col items-center justify-center gap-3 animate-rise"
        role="alert"
        aria-busy={!installFailed}
        aria-label={i18nT(installFailed
          ? 'components.updateModal.install_failed'
          : 'components.updateModal.installing_update')}
      >
        {installFailed ? (
          <>
            <span className="text-3xl text-danger"><AlertTriangle className="lucide-inline" /></span>
            <div className="text-lg font-bold text-text-strong">{i18nT('components.updateModal.install_failed')}</div>
            <p className="text-sm text-muted max-w-sm text-center px-6">
              {i18nT('components.updateModal.install_failed_body')}
            </p>
            <button
              type="button"
              className="mt-1 px-3 py-1.5 text-sm rounded-md bg-accent text-accent-fg hover:opacity-90 cursor-pointer"
              onClick={() => setDismissed(true)}
            >
              {i18nT('components.updateModal.back_to_dashboard')}
            </button>
          </>
        ) : (
          <>
            <span className="text-3xl text-accent"><Loader2 className="lucide-inline animate-spin" /></span>
            <div className="text-lg font-bold text-text-strong">{i18nT('components.updateModal.installing_update')}</div>
            <p className="text-sm text-muted max-w-sm text-center px-6">
              {i18nT('components.updateModal.installing_update_body')}
            </p>
          </>
        )}
      </div>
    )
  }

  if (!open) return null

  const version = update!.version || ''
  const notes = (update!.notes || '').trim()
  const dismiss = () => { if (!installing) setDismissed(true) }

  return (
    <div
      className="fixed inset-0 z-50 bg-bg/80 backdrop-blur-sm flex items-center justify-center animate-rise"
      role="button"
      tabIndex={-1}
      aria-label={i18nT('components.updateModal.dismiss_update_dialog')}
      // Only dismiss when the click lands on the backdrop itself, not when it
      // bubbles up from the dialog — avoids needing a stopPropagation handler
      // (and its a11y warning) on the non-interactive dialog element.
      onClick={e => { if (e.target === e.currentTarget) dismiss() }}
      onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); dismiss() } }}
    >
      <div
        className="bg-card border border-border rounded-xl shadow-xl w-[460px] max-w-[90vw] flex flex-col overflow-hidden"
        role="dialog"
        aria-modal="true"
        aria-label={i18nT('components.updateModal.update_ready')}
      >
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-bg-elevated">
          <div className="flex items-center gap-2">
            <Download className="lucide-inline text-accent" size={16} />
            <span className="text-sm font-semibold text-text">{i18nT('components.updateModal.update_ready')}</span>
          </div>
          <button
            type="button"
            className="text-muted hover:text-text cursor-pointer bg-transparent border-none disabled:opacity-50"
            onClick={dismiss}
            disabled={installing}
            aria-label={i18nT('components.updateModal.dismiss')}
          >
            <X size={16} />
          </button>
        </div>

        <div className="px-4 py-3 text-sm text-text">
          <p>{i18nT('components.updateModal.kirocrew')} {version && <span className="font-semibold">{version}</span>} {i18nT('components.updateModal.is_downloaded_and_ready_to_install')}</p>
          {notes && (
            <p className="mt-2 text-[13px] text-muted whitespace-pre-wrap max-h-40 overflow-auto">{notes}</p>
          )}
          <p className="mt-2 text-[12px] text-muted">
            {i18nT('components.updateModal.the_app_will_stop_the_local_gateway_install_the')}
          </p>
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-2.5 border-t border-border bg-bg-elevated">
          <button
            type="button"
            className="px-3 py-1.5 text-sm text-muted hover:text-text bg-transparent border-none cursor-pointer disabled:opacity-50"
            onClick={dismiss}
            disabled={installing}
          >
            {i18nT('components.updateModal.later')}
          </button>
          <button
            type="button"
            className="px-3 py-1.5 text-sm rounded-md bg-accent text-accent-fg hover:opacity-90 cursor-pointer disabled:opacity-50"
            onClick={() => { setInstallFor(update!.version); installMutation.mutate() }}
            disabled={installing}
          >
            {installing ? i18nT('components.updateModal.restarting') : i18nT('components.updateModal.restart_update')}
          </button>
        </div>
      </div>
    </div>
  )
}
