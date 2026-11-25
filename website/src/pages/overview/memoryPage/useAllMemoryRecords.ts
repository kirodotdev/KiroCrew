/**
 * Loads every record of a store in the background, newest first, so the list can
 * filter, sort and search locally once it has them (`memoryLocalList.ts`).
 *
 * The page shows its first rows from the server path meanwhile; this starts beside
 * it and reads pages of `PAGE` with `PARALLEL` requests at once, reporting how many
 * have arrived. An edit invalidates `['memory-records', store]`, which reloads it,
 * and until that reload ends the list goes back to the server path, so it never
 * shows rows from before the edit.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import type { MemoryRecord } from '../../../types/memoryEditing'
import { memoryQueryRetry } from '../MemoryStoreCard'
import { recordIdentity } from '../memoryRecordParts'

/** The route's largest summary page (`memory_edit.SUMMARY_PAGE_MAX`). */
const PAGE = 1000
const PARALLEL = 4
const ALL = { q: '', kind: 'all' } as const

export type AllRecords = { records: MemoryRecord[] | null; loaded: number; total: number; loading: boolean }

export function useAllMemoryRecords(store: string, enabled = true): AllRecords {
  const [progress, setProgress] = useState({ store, loaded: 0, total: 0 })
  const all = useQuery({
    queryKey: ['memory-records', store, 'all'],
    enabled,
    retry: memoryQueryRetry,
    queryFn: async ({ signal }) => {
      const read = (offset: number) => api.memoryRecords(store, ALL, offset, PAGE, { sort: 'updated', order: 'desc', summary: true })
      const first = await read(0)
      const pages: MemoryRecord[][] = [first.entries]
      let loaded = first.entries.length
      setProgress({ store, loaded, total: first.total })
      const offsets: number[] = []
      for (let offset = PAGE; offset < first.total; offset += PAGE) offsets.push(offset)
      let next = 0
      const worker = async () => {
        while (next < offsets.length) {
          if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
          const index = ++next
          const page = await read(offsets[index - 1])
          pages[index] = page.entries
          loaded += page.entries.length
          setProgress({ store, loaded, total: first.total })
        }
      }
      await Promise.all(Array.from({ length: PARALLEL }, worker))
      // A write between pages can shift a row onto two pages; keep its first copy.
      const seen = new Set<string>()
      return pages.flat().filter(record => { const id = recordIdentity(record); if (seen.has(id)) return false; seen.add(id); return true })
    },
  })
  const loading = all.isFetching || !all.data
  const mine = progress.store === store
  // A server that sends no summaries cannot be listed locally: stay on its own path.
  const usable = !loading && all.data!.every(record => record.summary)
  return { records: usable ? all.data! : null, loaded: mine ? progress.loaded : 0, total: mine ? progress.total : 0, loading }
}
