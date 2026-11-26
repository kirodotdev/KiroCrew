import { lazy, memo, Suspense } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useAppSelector } from '../../store'
import { ContentSkeleton } from '../../components/ui'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

/** Lazy: the memory page and its cards are only needed when this tab opens. */
const MemoryTab = lazy(() => import('../overview/MemoryTab'))

/** Settings > Memory: Global memory and each bot's memory, picked with `?store=`.
 *  Memoised: it takes no props, so a Settings page render (the Kiro usage poll)
 *  does not re-render the memory page; its own store reads still do. */
export const MemoryPanel = memo(function MemoryPanel() {
  // A language switch must still reach the page under this memo.
  useLanguageGeneration()
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
})
