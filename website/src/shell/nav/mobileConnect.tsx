import { lazy, Suspense, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { canRenderMobileConnectKind } from '../../components/mobileConnectRenderers'

// The dialog is lazy; the renderer registry it consults is NOT (imported at the
// top of this file). The nav rail decides whether to show the "Connect your
// phone" row before this chunk is ever fetched, so a predicate hiding inside it
// would answer "cannot draw" for every method until the user had already opened
// a dialog the row never offered.
const MobileConnectModal = lazy(() => import('../../components/MobileConnectModal'))

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
    queryFn: api.mobileConnectMethods,
    staleTime: 5 * 60_000,
    retry: false,
  })
  // Only kinds this frontend can draw — a built-in section or an edition's
  // registered renderer (`components/mobileConnectRenderers.tsx`). A kind
  // nothing can draw would otherwise show the rail row and then open an empty
  // dialog, so the predicate, not a literal list, is what gates the row.
  const mobileConnectKinds = (mobileConnectQuery.data?.methods ?? [])
    .map(m => m.kind)
    .filter(canRenderMobileConnectKind)
  const hasRenderableMobileConnect = mobileConnectKinds.length > 0
  // A methods refresh can revoke or replace every previously renderable kind
  // while the overlay is open. Close it rather than preserving state that would
  // remount the dialog if a future refresh happens to add a method back.
  useEffect(() => {
    if (!hasRenderableMobileConnect) setMobileConnectOpen(false)
  }, [hasRenderableMobileConnect])
  return { mobileConnectOpen, setMobileConnectOpen, mobileConnectKinds, hasRenderableMobileConnect }
}

export function MobileConnectDialog({ mobileConnect }: { mobileConnect: ReturnType<typeof useMobileConnect> }) {
  const { mobileConnectOpen, setMobileConnectOpen, mobileConnectKinds, hasRenderableMobileConnect } = mobileConnect
  if (!(mobileConnectOpen && hasRenderableMobileConnect)) return null
  return (
    <Suspense fallback={null}>
      <MobileConnectModal kinds={mobileConnectKinds} onClose={() => setMobileConnectOpen(false)} />
    </Suspense>
  )
}
