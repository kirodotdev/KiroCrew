import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Undo2, Archive } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Badge, EmptyState } from '../../components/ui'
import { fmtNumber, fmtDateTimeNumeric } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { MemoryScopeNotice, memoryQueryRetry } from './MemoryStoreCard'

/**
 * Episodes a semantic write superseded, with a way to put one back.
 *
 * The retirement is a similarity JUDGEMENT about what is no longer true, and
 * nothing in the memory engine hard-deletes an episode — so without a list like
 * this the rule is indistinguishable from data loss, because every other reader
 * filters the tombstoned rows out.
 */

/** Rows one page asks for. Past what an operator reads in one screen, and the
 *  route clamps its own ceiling anyway. */
const RETIRED_PAGE = 50

/** Longest retired text rendered inline. A retired episode is a whole
 *  conversation turn, so an uncapped cell turns the table into a wall. */
const TEXT_PREVIEW_CHARS = 240

export default function MemoryRetiredCard({ store }: { store: string; privateMemory?: boolean }) {
  const queryClient = useQueryClient()
  const retired = useInfiniteQuery({
    queryKey: ['memory-retired', store],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => pageParam ? api.memoryRetired(store || undefined, RETIRED_PAGE, pageParam) : api.memoryRetired(store || undefined, RETIRED_PAGE),
    getNextPageParam: (last, pages) => last.retired.length === RETIRED_PAGE ? pages.length * RETIRED_PAGE : undefined,
    retry: memoryQueryRetry,
  })
  const restore = useMutation({
    mutationFn: (id: string) => api.memoryRestoreRetired(id, store || undefined),
    onSuccess: () => {
      // The row leaves this list and rejoins the live episodes, so the store's
      // counts move too.
      queryClient.invalidateQueries({ queryKey: ['memory-retired', store] })
      queryClient.invalidateQueries({ queryKey: ['memory-stores'] })
      queryClient.invalidateQueries({ queryKey: ['member-memory', store || 'default'] })
      queryClient.invalidateQueries({ queryKey: ['memory-records', store || 'default'] })
    },
  })

  const rows = retired.data?.pages.flatMap(page => page.retired) ?? []

  return (
    <Card>
      <CardTitle>{i18nT('memoryV2.replaced_experiences')}</CardTitle>
      <p className="text-[12px] leading-relaxed text-muted mb-2">
        {i18nT('memoryV2.replaced_experiences_hint')}
      </p>
      <MemoryScopeNotice error={retired.error} />
      {!!retired.error && store && <Btn className="mb-3 min-h-11" disabled={retired.isFetching} onClick={() => void retired.refetch()}>{i18nT('memoryV2.retry_read')}</Btn>}
      {restore.error && <MemoryScopeNotice error={restore.error} />}
      {retired.isPending && <p role="status" className="text-[13px] text-muted">{i18nT('pages.overview.memoryTab.loading')}</p>}
      {!retired.error && !retired.isPending && (
        <div className="overflow-x-auto"><table className="w-full border-collapse table-striped">
          <thead>
            <tr>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.memoryRetiredCard.memory')}</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('memoryV2.replaced_by')}</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('memoryV2.times_replaced')}</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('memoryV2.replaced_at')}</th>
              <th aria-label={i18nT('pages.overview.memoryRetiredCard.actions')} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={5}>
                  <EmptyState
                    icon={<Archive className="lucide-inline" />}
                    title={i18nT('memoryV2.no_replaced_experiences')}
                    subtitle={i18nT('memoryV2.replaced_experience_empty_hint')}
                  />
                </td>
              </tr>
            ) : rows.map(row => (
              <tr key={row.id} className="hover:bg-bg-hover transition-colors">
                {/* Rendered as a React text child, never through `esc`: React
                    escapes a text child already, and pre-escaping turns an
                    ampersand in a remembered sentence into `&amp;` on screen. */}
                <td className="px-2.5 py-2 border-b border-border text-sm">
                  {row.text.slice(0, TEXT_PREVIEW_CHARS)}
                  {row.text.length > TEXT_PREVIEW_CHARS && <><span className="text-muted">…</span><details className="mt-2"><summary className="cursor-pointer text-muted">{i18nT('memoryV2.view_details')}</summary><p className="mt-2 whitespace-pre-wrap break-words">{row.text}</p></details></>}
                </td>
                <td className="px-2.5 py-2 border-b border-border text-sm">
                  {row.superseded_by
                    ? row.superseded_by
                    : <span className="text-muted">{i18nT('pages.overview.memoryRetiredCard.not_recorded')}</span>}
                </td>
                {/* Retirements, not rows: the event log is append-only, so an
                    episode retired, restored and retired again counts more than
                    once here while still being one row. */}
                <td className="px-2.5 py-2 border-b border-border text-sm">
                  {(row.retired_times ?? 0) > 1
                    ? <Badge variant="warn">{fmtNumber(row.retired_times ?? 0)}</Badge>
                    : fmtNumber(row.retired_times ?? 1)}
                </td>
                <td className="px-2.5 py-2 border-b border-border text-sm">
                  {row.ts
                    ? fmtDateTimeNumeric(row.ts)
                    : <span className="text-muted">{i18nT('pages.overview.memoryRetiredCard.unknown')}</span>}
                </td>
                <td className="px-2.5 py-2 border-b border-border text-sm">
                  <Btn
                    disabled={restore.isPending}
                    onClick={() => restore.mutate(row.id)}
                  >
                    <Undo2 className="lucide-inline" aria-hidden="true" /> {i18nT('memoryV2.restore_experience')}
                  </Btn>
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      {retired.hasNextPage && <Btn className="mt-3 min-h-11" disabled={retired.isFetchingNextPage} onClick={() => void retired.fetchNextPage()}>{i18nT('memoryV2.show_more')}</Btn>}
    </Card>
  )
}
