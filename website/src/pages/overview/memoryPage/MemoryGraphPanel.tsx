import { lazy, Suspense } from 'react'
import { ContentSkeleton } from '../../../components/ui'

/** Lazy: the graph carries the sigma/graphology stack, needed only once its tab opens. */
const MemoryGraphTab = lazy(() => import('../MemoryGraphTab'))

/** The memory page's Graph tab for one store; empty `store` is Global memory. */
export function MemoryGraphPanel({ store }: { store: string }) {
  return <Suspense fallback={<ContentSkeleton rows={6} />}><MemoryGraphTab store={store} /></Suspense>
}
