import { lazy, Suspense } from 'react'
import { useMergeBackDialog } from '../hooks/useMergeBackDialog'

const MergeBackDialog = lazy(() => import('./MergeBackDialog'))

/**
 * Where ChatPage mounts the merge dialog. The dialog opens only from a session
 * menu, so its code loads on the first open instead of riding in the
 * always-loaded App chunk (the App entry in scripts/check-bundle-size.mjs).
 */
export default function MergeBackDialogHost() {
  if (!useMergeBackDialog()?.forkKey) return null
  return <Suspense fallback={null}><MergeBackDialog /></Suspense>
}
