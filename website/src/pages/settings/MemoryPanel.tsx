import { lazy, Suspense } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useAppSelector } from '../../store'
import { ContentSkeleton } from '../../components/ui'

/** Lazy: the memory page and its cards are only needed when this tab opens. */
const MemoryTab = lazy(() => import('../overview/MemoryTab'))

/** Settings > Memory: Global memory and each bot's memory, picked with `?store=`. */
export function MemoryPanel() {
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  // Moves on every turn end: the page re-reads its store list on it, and the
  // rest of the page when that list changed.
  const liveRefreshTrigger = useAppSelector(s => s.dashboard.liveRefreshTrigger)
  const [params, setParams] = useSearchParams()
  return <Suspense fallback={<ContentSkeleton rows={6} />}>
    <MemoryTab refreshTrigger={refreshTrigger} liveRefreshTrigger={liveRefreshTrigger} selectedStore={params.get('store') || ''} focus={params.get('focus')} focusLessonKey={params.get('lesson')} onStoreNavigate={store => setParams(previous => {
      const next = new URLSearchParams(previous)
      if (store) next.set('store', store)
      else next.delete('store')
      next.delete('lesson')
      return next
    }, { replace: true })} />
  </Suspense>
}
