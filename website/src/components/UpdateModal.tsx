import { useEffect, useState } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Download, X } from 'lucide-react'

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
 * No-ops entirely in the browser (query cache never gets populated without
 * the Electron preload), so it's safe to mount unconditionally in App.
 */

type UpdateState = {
  state: 'checking' | 'available' | 'downloading' | 'downloaded' | 'not-available' | 'error'
  version?: string
  notes?: string
  channel?: string
  message?: string
}

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
  // Re-open on each fresh "downloaded" event (version change resets dismiss)
  const [lastVersion, setLastVersion] = useState<string | undefined>(undefined)
  if (update?.state === 'downloaded' && update.version !== lastVersion) {
    setLastVersion(update.version)
    setDismissed(false)
  }

  const installMutation = useMutation({ mutationFn: () => getUpdateApi()!.install() })
  const installing = installMutation.isPending

  const open = !!update && update.state === 'downloaded' && !dismissed

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

  if (!open) return null

  const version = update!.version || ''
  const notes = (update!.notes || '').trim()
  const dismiss = () => { if (!installing) setDismissed(true) }

  return (
    <div
      className="fixed inset-0 z-50 bg-bg/80 backdrop-blur-sm flex items-center justify-center animate-rise"
      role="button"
      tabIndex={-1}
      aria-label="Dismiss update dialog"
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
        aria-label="Update ready"
      >
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-bg-elevated">
          <div className="flex items-center gap-2">
            <Download className="lucide-inline text-accent" size={16} />
            <span className="text-sm font-semibold text-text">Update ready</span>
          </div>
          <button
            type="button"
            className="text-muted hover:text-text cursor-pointer bg-transparent border-none disabled:opacity-50"
            onClick={dismiss}
            disabled={installing}
            aria-label="Dismiss"
          >
            <X size={16} />
          </button>
        </div>

        <div className="px-4 py-3 text-sm text-text">
          <p>KiroCrew {version && <span className="font-semibold">{version}</span>} is downloaded and ready to install.</p>
          {notes && (
            <p className="mt-2 text-[13px] text-muted whitespace-pre-wrap max-h-40 overflow-auto">{notes}</p>
          )}
          <p className="mt-2 text-[12px] text-muted">
            The app will stop the local gateway, install the update, and relaunch.
          </p>
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-2.5 border-t border-border bg-bg-elevated">
          <button
            type="button"
            className="px-3 py-1.5 text-sm text-muted hover:text-text bg-transparent border-none cursor-pointer disabled:opacity-50"
            onClick={dismiss}
            disabled={installing}
          >
            Later
          </button>
          <button
            type="button"
            className="px-3 py-1.5 text-sm rounded-md bg-accent text-accent-fg hover:opacity-90 cursor-pointer disabled:opacity-50"
            onClick={() => installMutation.mutate()}
            disabled={installing}
          >
            {installing ? 'Restarting...' : 'Restart & Update'}
          </button>
        </div>
      </div>
    </div>
  )
}
