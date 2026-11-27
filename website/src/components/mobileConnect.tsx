import { useEffect, useRef, useState, type ComponentProps, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Smartphone, X } from 'lucide-react'
import { api } from '../api/client'
import { readAfterRenewal } from '../api/readAfterRenewal'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { useViewerIsNotOwner } from '../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../i18n/t'
import { lazyRetry } from './lazyRetry'
import { canRenderMobileConnectKind } from './mobileConnectRenderers'
import type { MobileConnectBody as MobileConnectBodyType } from './MobileConnectModal'

// The dialog's body is lazy; the renderer registry it consults is NOT (imported
// at the top of this file). The nav rail decides whether to show the "Connect
// your phone" row before this chunk is ever fetched, so a predicate hiding inside
// it would answer "cannot draw" for every method until the user had already
// opened a dialog the row never offered.
const MobileConnectBody = lazyRetry<ComponentProps<typeof MobileConnectBodyType>>(() => import('./MobileConnectModal').then(m => ({ default: m.MobileConnectBody })), {
  layout: 'block',
  title: () => i18nT('components.mobileConnect.dialog_load_failed'),
  scope: 'mobile-connect-dialog',
})

/** The rail's "Connect your phone" row: its methods, and the dialog it opens. */
export function useMobileConnect(locationKey: string) {
  // "Connect your phone" rail entry. The methods come from the CPP
  // mobile_connect seam filtered by governance; an empty list (edition
  // returned none, policy denied all, seam degraded) hides the row entirely —
  // the endpoint is the authority, the frontend never guesses.
  const [mobileConnectOpen, setMobileConnectOpen] = useState(false)
  // The dialog is a transient overlay opened from a rail row that does not
  // navigate (path="#"), so a navigation — clicking another nav tab or
  // switching chat sessions — must dismiss it, the same as Escape or a
  // backdrop click. Its open flag lives here at the owner rather than in the
  // modal, so nothing inside the modal sees navigation. Key this off
  // location.key, not location.pathname: switching between untitled /chat
  // sessions changes only the key/query, so a pathname dep would leave the
  // dialog stranded over the newly selected session. location.key changes on
  // every history entry, so this closes it on ANY navigation at once.
  useEffect(() => { setMobileConnectOpen(false) }, [locationKey])
  const mobileConnectQuery = useQuery({
    queryKey: ['mobile-connect-methods'],
    // No retry, so a read refused at a lapsed sign-in would hide the row until
    // the window next gets focus: it is read once more after the renewal.
    queryFn: () => readAfterRenewal(() => api.mobileConnectMethods()),
    staleTime: 5 * 60_000,
    retry: false,
  })
  // Only kinds this frontend can draw — a built-in section or an edition's
  // registered renderer (`components/mobileConnectRenderers.tsx`). A kind
  // nothing can draw would otherwise show the rail row and then open an empty
  // dialog, so the predicate, not a literal list, is what gates the row.
  // Pairing a phone mints a sign-in for this machine, which only its owner may
  // hand out, so a teammate gets no row and no dialog. Until the owner check
  // answers the methods decide, so the owner's row never waits on it.
  const { notOwner } = useViewerIsNotOwner()
  const mobileConnectKinds = notOwner ? [] : (mobileConnectQuery.data?.methods ?? [])
    .map(m => m.kind)
    .filter(canRenderMobileConnectKind)
  const hasRenderableMobileConnect = mobileConnectKinds.length > 0
  // A methods refresh can revoke or replace every previously renderable kind
  // while the overlay is open. Close it rather than preserving state that would
  // remount the dialog if a future refresh happens to add a method back.
  useEffect(() => {
    if (!hasRenderableMobileConnect) setMobileConnectOpen(false)
  }, [hasRenderableMobileConnect])
  return {
    mobileConnectOpen, setMobileConnectOpen, mobileConnectKinds, hasRenderableMobileConnect,
    mobileConnectQuery,
  }
}

export function MobileConnectDialog({ mobileConnect }: { mobileConnect: ReturnType<typeof useMobileConnect> }) {
  const { mobileConnectOpen, setMobileConnectOpen, mobileConnectKinds, hasRenderableMobileConnect } = mobileConnect
  if (!(mobileConnectOpen && hasRenderableMobileConnect)) return null
  const close = () => setMobileConnectOpen(false)
  // The frame is drawn from this bundle, so a body whose chunk fails to load
  // shows its notice, with Try Again, inside a dialog that still closes.
  return (
    <MobileConnectFrame onClose={close}>
      <MobileConnectBody kinds={mobileConnectKinds} onClose={close} onHandoff={close} />
    </MobileConnectFrame>
  )
}

/** The "Connect your phone" dialog's frame: the backdrop, the panel with its
 *  close button and title, and the modal keyboard contract. */
export function MobileConnectFrame({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const { t } = useTranslation()
  const dialogRef = useRef<HTMLDivElement>(null)
  // The repo's modal keyboard contract: focus-in on mount, focus-restore on
  // close, Tab trapped inside the dialog, Escape dismisses.
  useDialogFocusTrap(dialogRef, onClose)
  // Dismiss on any pointer-down OUTSIDE the dialog. The dialog layer is
  // pointer-events-none, so a click anywhere outside the panel falls through to
  // whatever paints underneath — the backdrop, the main content, or the nav
  // rail, which renders ABOVE the backdrop so a click on empty sidebar space
  // never reaches the backdrop (the dead zone this closes). A nav row's own
  // click still fires, so clicking a tab both dismisses and navigates. Attached
  // from an effect (after mount) so the very click that opened the dialog —
  // already dispatched before this runs — cannot immediately close it; capture
  // phase so a child that stops propagation can't hide the outside click from
  // us.
  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      const dialog = dialogRef.current
      if (dialog && !dialog.contains(e.target as Node)) onClose()
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => document.removeEventListener('pointerdown', onPointerDown, true)
  }, [onClose])

  // Two sibling layers, the same split Modal.tsx uses for its backdrop and
  // dialog wrapper. The BACKDROP stays on the chat-pane ceiling (z-50): the
  // desktop nav rail (navRail.tsx, `focus-chrome-rail`) is a DOM-later z-50 sibling
  // in the shell's stacking context, so it wins the tie and stays clickable —
  // a nav-row click both dismisses (the outside-pointerdown handler above) and
  // navigates. The DIALOG LAYER sits at z-[65]: ABOVE every piece of chat
  // chrome the panel must cover — the sessions flyout (z-[59]), its drawer
  // morph (z-[60]), the rail toggle and focus-peek layers (z-[61]), the
  // focus-mode rail (inline zIndex 62) and its mac drag strips (inline
  // zIndex 63) — and BELOW the shell's z-[70] toast/menu band and its z-[100]
  // full-screen takeovers (the "Installing update…" surface, the update-error
  // and changelog dialogs), which must always paint over an open panel.
  // Modal.tsx's z-[100] is NOT the reference here: Modal portals to
  // document.body, a different stacking context, while this component renders
  // inside the App shell's `relative z-[1]` root, where z-[100] only ties
  // with the shell's own DOM-earlier takeovers and wins on document order.
  // The layer is pointer-events-none (only the panel is pointer-events-auto)
  // so every click outside the panel passes through to what is underneath.
  return (
    <>
      <div
        className="fixed inset-0 z-50 bg-bg/80 backdrop-blur-xs animate-rise"
        role="presentation"
      />
      <div className="fixed inset-0 z-[65] flex items-center justify-center p-safe pointer-events-none animate-rise">
        <div
          ref={dialogRef}
          tabIndex={-1}
          role="dialog"
          aria-modal="true"
          aria-label={t('components.mobileConnect.use_kiro_crew_on_your_phone')}
          className="bg-card border border-border rounded-xl shadow-xl w-[440px] max-w-[90vw] max-h-[85vh] overflow-y-auto outline-hidden p-6 relative pointer-events-auto"
        >
          <button
            onClick={onClose}
            aria-label={t('components.mobileConnect.close')}
            className="absolute top-3 right-3 text-muted hover:text-text bg-transparent border-none cursor-pointer p-1"
          >
            <X size={16} />
          </button>
          <div className="flex items-center gap-2 text-[15px] font-semibold text-text-strong mb-1.5">
            <Smartphone size={16} className="shrink-0" />
            {t('components.mobileConnect.use_kiro_crew_on_your_phone')}
          </div>
          {children}
        </div>
      </div>
    </>
  )
}
