/** Clean up sessions: the archive window, its dry-run preview and the archive
 *  write (`useSessionCleanup`), and the header menu's panel that drives them. */
import { useState } from 'react'
import { BrushCleaning } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import { useQuery, useMutation } from '@tanstack/react-query'
import { deleteSlot, fetchHistory } from '../../store/chatSlice'
import { api } from '../../api/client'
import { Btn } from '../../components/ui'
import { fmtRelativeTime, slotActivityTs } from '../chat/sessionOrder'
import { i18nT } from '../../i18n/t'
import type { Slot } from './types'
import type { QueryClient } from '@tanstack/react-query'
import type { AppDispatch } from '../../store'
import type { SidebarScope } from './scope'

/** The cleanup panel's state, preview read and archive write. */
export function useSessionCleanup({ cleanupOpen, setCleanupOpen, activeSlot, dispatch, queryClient }: {
  cleanupOpen: boolean
  setCleanupOpen: (open: boolean) => void
  activeSlot: string | null
  dispatch: AppDispatch
  queryClient: QueryClient
}) {
  const [cleanupDays, setCleanupDays] = useState(3)
  const [cleanupExpanded, setCleanupExpanded] = useState(false)
  const [cleanupError, setCleanupError] = useState('')
  const { data: cleanupPreviewData, isLoading: cleanupPreviewLoading, isError: cleanupPreviewError } = useQuery({
    queryKey: ['cleanup-preview', cleanupDays, activeSlot],
    queryFn: () => api.cleanupSessions(cleanupDays, activeSlot || '', true),
    enabled: cleanupOpen,
    gcTime: 0,
  })
  const cleanupPreview = cleanupPreviewData?.keys ?? null
  const activeIsStale = cleanupPreviewData?.active_is_stale ?? false
  const cleanupMutation = useMutation({
    mutationFn: () => api.cleanupSessions(cleanupDays, activeSlot || ''),
    onSuccess: (res) => {
      if (res.keys?.length) {
        for (const key of res.keys) dispatch(deleteSlot(key))
        dispatch(fetchHistory(false))
      }
      if (res.failed?.length) {
        setCleanupError(`${res.failed.length} session(s) failed to archive`)
      } else {
        setCleanupOpen(false)
      }
      queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })
    },
    onError: (e) => setCleanupError(e instanceof Error ? e.message : i18nT('pages.chatSidebar.archive_failed')),
  })
  return { cleanupDays, setCleanupDays, cleanupExpanded, setCleanupExpanded, cleanupError, setCleanupError, cleanupPreview, cleanupPreviewLoading, cleanupPreviewError, activeIsStale, cleanupMutation }
}

/** The cleanup panel. */
export function renderSessionCleanupPanel(shell: SidebarScope): React.ReactNode {
  const { localSlots, queryClient, setCleanupOpen } = shell
  const { activeIsStale, cleanupDays, cleanupError, cleanupExpanded, cleanupMutation, cleanupPreview,
    cleanupPreviewError, cleanupPreviewLoading, setCleanupDays, setCleanupError, setCleanupExpanded } = shell.sessionCleanup
        const archivable = cleanupPreview ? cleanupPreview.map(k => localSlots.find(s => s.key === k)).filter(Boolean) as Slot[] : []
        const noStale = cleanupPreview != null && cleanupPreview.length === 0 && !activeIsStale
        return (
          <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
            <div className="font-medium text-text-strong mb-2"><BrushCleaning size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.clean_up_sessions_2')}</div>
            <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.archive_sessions_with_no_activity_in_the_last')}</div>
            <div className="flex items-center gap-2 mb-3">
              {[1, 3, 7].map(d => (
                <button key={d} className={`px-2.5 py-1 rounded-md text-[12px] border transition-all cursor-pointer ${
                  cleanupDays === d ? 'bg-accent text-accent-fg border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'
                }`} onClick={() => setCleanupDays(d)}>{i18nT('pages.chatSidebar.day', { count: d })}</button>
              ))}
            </div>
            <div className="text-[12px] text-muted mb-3">
              {cleanupPreviewLoading
                ? i18nT('pages.chatSidebar.checking')
                : cleanupPreviewError
                  ? (
                    // Read failure inside a confirm dialog with no draft: nothing to
                    // lose, so the hand-off is on. Retry stays a separate button
                    // rather than being the error surface itself.
                    <span className="inline-flex items-center gap-2 flex-wrap">
                      <ErrorNotice message={i18nT('pages.chatSidebar.failed_to_load_preview')} variant="inline" askAgent testId="cleanup-preview-error" />
                      <Btn className="text-[12px] px-2 py-0.5" onClick={() => queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })}>{i18nT('pages.chatSidebar.retry')}</Btn>
                    </span>
                  )
                  : noStale
                    ? i18nT('pages.chatSidebar.no_inactive_sessions_to_archive')
                    : cleanupPreview != null && <>
                      {i18nT('pages.chatSidebar.session', { count: archivable.length })} {i18nT('pages.chatSidebar.will_be_moved_to_older_sessions')}{activeIsStale ? ` ${i18nT('pages.chatSidebar.1_skipped_currently_selected')}` : ''} {i18nT('pages.chatSidebar.pinned_sessions_are_kept')}
                      {archivable.length > 0 && (
                        <button className="ml-1 text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => setCleanupExpanded(!cleanupExpanded)}>
                          {cleanupExpanded ? i18nT('pages.chatSidebar.hide') : i18nT('pages.chatSidebar.show')} {i18nT('pages.chatSidebar.session', { count: archivable.length })} ▸
                        </button>
                      )}
                      {cleanupExpanded && archivable.length > 0 && (
                        <div className="mt-2 max-h-32 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5">
                          {archivable.map(s => (
                            <div key={s.key} className="text-[12px] text-muted truncate py-0.5 px-1">
                              {s.title && s.title !== s.key ? s.title : s.key}
                              {slotActivityTs(s) && <span className="ml-1 text-[11px] opacity-60">{fmtRelativeTime(slotActivityTs(s))}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                      </>
              }
            </div>
            {/* Archive inputs are server-side (the days window is a persisted
                pick, not a draft), so nothing is lost by handing off. Its own
                line, above the Cancel/Archive pair: a third control in that row
                would break max-two-buttons-per-row. */}
            <ErrorNotice message={cleanupError} askAgent className="mb-2" testId="cleanup-error" />
            <div className="flex items-center gap-2 justify-end">
              <Btn className="text-[12px] px-3 py-1" onClick={() => setCleanupOpen(false)}>{i18nT('pages.chatSidebar.cancel')}</Btn>
              <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={archivable.length === 0 || cleanupMutation.isPending || cleanupPreviewLoading} onClick={() => {
                setCleanupError('')
                cleanupMutation.mutate()
              }}>{cleanupMutation.isPending ? i18nT('pages.chatSidebar.archiving') : i18nT('pages.chatSidebar.archive_session', { count: archivable.length })}</Btn>
            </div>
          </div>
        )
}
