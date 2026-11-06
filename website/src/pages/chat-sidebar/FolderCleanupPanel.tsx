import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { shallowEqual } from 'react-redux'
import { Clock, FolderX } from 'lucide-react'
import { api } from '../../api/client'
import { invalidateFoldersWhenIdle } from '../../api/chatFoldersWrite'
import ErrorNotice from '../../components/ErrorNotice'
import { MORE_BELOW_MASK } from '../../components/ModelEffortDropdown'
import { Btn, PanelSectionHeader } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { useScrollEdgesY } from '../../hooks/useScrollEdges'
import { closeBusyReasons } from '../../lib/closeBusyGate'
import { reasonLine } from '../../lib/closeConfirmPrompt'
import { useAppDispatch, useAppSelector } from '../../store'
import { deleteSlot } from '../../store/chatSlice'
import { collectFolderSubtreeIds } from '../../utils/folderTree'
import type { ChatFolder } from '../../types'
import type { Slot } from './types'

/** Why a previewed root stays, per the server's `root_kept`. */
const ROOT_KEPT_LINE = {
  cron: 'pages.chatSidebar.root_kept_cron',
  default: 'pages.chatSidebar.root_kept_default',
  channel: 'pages.chatSidebar.root_kept_channel',
  kept: 'pages.chatSidebar.root_kept_other',
} as const

/** "a / b / c" for a folder, walking its parent chain. Guarded against a cycle. */
export function folderPath(folders: ChatFolder[], id: string): string {
  const byId = new Map(folders.map(f => [f.id, f]))
  const names: string[] = []
  const seen = new Set<string>()
  let cur = byId.get(id)
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    names.unshift(cur.name)
    cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
  }
  return names.join(' / ')
}

/**
 * The header ⋮ → "Clean up empty folders" panel: a dry-run preview of every
 * folder with no live session and no settings, then one Delete for all of
 * them. Delete sends back the previewed ids and the server deletes only those
 * that are still empty, re-checked inside the folder-store lock: a session
 * filed after the preview keeps its folder, and a folder that emptied after
 * the preview (so was never shown) is not deleted.
 *
 * With `root` it is the folder ⋮ → "Close sessions and delete…" panel for that
 * folder and every folder under it: the preview also lists the sessions filed
 * there, each with the live work its row ✕ would ask about (a running turn, an
 * armed loop, ...), and Delete first closes each one as that ✕ does (archived,
 * revivable). The server keeps the folder of a session that stayed open, and
 * every folder above it.
 */
export default function FolderCleanupPanel({ folders, root, slots = [], onClose }: {
  folders: ChatFolder[]
  root?: ChatFolder
  slots?: Slot[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const [includeTopLevel, setIncludeTopLevel] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // Busy lines make rows tall, so the capped list can hide a session it will
  // close: fade its bottom edge while more rows sit below.
  const [attachSessions, sessionEdges, , attachSessionsContent] = useScrollEdgesY<HTMLDivElement>()
  const preview = useQuery({
    queryKey: ['folder-cleanup-preview', includeTopLevel, root?.id],
    queryFn: () => api.cleanupChatFolders({ dryRun: true, includeTopLevel, root: root?.id }),
    gcTime: 0,
  })
  const archived = preview.data?.archived ?? {}
  // The server keeps this root (a cron job's, a channel's or the default
  // folder, ...): the panel then closes sessions and says why the folder stays.
  const rootKept = root ? preview.data?.root_kept : undefined
  const previewIds = preview.data?.ids
  const rows = useMemo(() => (previewIds ?? []).map(id => ({ id, path: folderPath(folders, id) })), [previewIds, folders])
  const toClose = useMemo(() => {
    if (!root) return []
    const tree = collectFolderSubtreeIds(folders, root.id)
    return slots.filter(s => s.folder_id && tree.has(s.folder_id))
  }, [root, folders, slots])
  const busy = useAppSelector(state => toClose.map(s => closeBusyReasons(state, s.key).map(reasonLine).join(' ')), shallowEqual)
  const cleanup = useMutation({
    mutationFn: async () => {
      // The open chat closes last, so its close lands outside this tree
      // instead of opening a session the next close would take away.
      const ordered = [...toClose.filter(s => s.key !== activeSlot), ...toClose.filter(s => s.key === activeSlot)]
      let stayedOpen = 0
      for (const s of ordered) {
        if (deleteSlot.rejected.match(await dispatch(deleteSlot(s.key)))) stayedOpen++
      }
      const res = await api.cleanupChatFolders({ includeTopLevel, root: root?.id, ids: previewIds ?? [] })
      return { res, stayedOpen }
    },
    onSuccess: ({ res, stayedOpen }) => {
      // Through the idle gate, not a direct invalidate: this refetch of the
      // folder tree would otherwise land inside a still-pending optimistic
      // folder write in the sidebar and snap it back (see CHAT_FOLDERS_WRITE_KEY).
      invalidateFoldersWhenIdle(queryClient)
      queryClient.invalidateQueries({ queryKey: ['folder-cleanup-preview'] })
      // On the panel's own error surface: a later close that succeeds clears
      // the sidebar's refused-close notice, so that one cannot carry it.
      if (stayedOpen > 0) {
        setError(i18nT('pages.chatSidebar.some_sessions_stayed_open'))
        return
      }
      // The server keeps any previewed folder that gained a session or a new
      // subfolder after the preview. Closing then would look as if they went
      // too; stay open on the fresh preview and say why some stayed.
      if ((res.deleted?.length ?? 0) < (previewIds?.length ?? 0)) {
        setNotice(i18nT(root ? 'pages.chatSidebar.some_folders_stayed' : 'pages.chatSidebar.some_folders_kept'))
        return
      }
      onClose()
    },
    onError: (e) => setError(e instanceof Error ? e.message : i18nT('components.errorBoundary.something_went_wrong')),
  })
  return (
    <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise" data-testid="folder-cleanup-panel">
      <div className="font-medium text-text-strong mb-2"><FolderX size={14} className="lucide-inline" /> {root ? i18nT(rootKept ? 'pages.chatSidebar.close_sessions_in_title' : 'pages.chatSidebar.close_sessions_and_delete_title', { name: root.name }) : i18nT('pages.chatSidebar.clean_up_empty_folders')}</div>
      <div className="text-muted text-[12px] mb-2">{i18nT(root ? (rootKept ? 'pages.chatSidebar.close_sessions_explainer' : 'pages.chatSidebar.close_sessions_and_delete_explainer') : 'pages.chatSidebar.empty_folders_explainer')}</div>
      {root && rootKept && (
        <div className="text-[12px] text-text mb-2" data-testid="folder-cleanup-root-kept">
          {i18nT(ROOT_KEPT_LINE[rootKept], { name: root.name })}
        </div>
      )}
      {root ? toClose.length > 0 && (
        <div className="mb-2">
          <PanelSectionHeader className="mb-1" label={i18nT('pages.chatSidebar.sessions_to_close')} count={toClose.length} />
          <div ref={attachSessions} className={`max-h-40 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5 ${sessionEdges.bottom ? MORE_BELOW_MASK : ''}`} data-testid="folder-cleanup-sessions">
            <div ref={attachSessionsContent}>
            {toClose.map((s, i) => {
              const path = `${folderPath(folders, s.folder_id ?? '')} / ${s.title && s.title !== s.key ? s.title : s.key}`
              return (
                <div key={s.key} className="text-[12px] text-muted py-0.5 px-1">
                  <div className="truncate" title={path}>{path}</div>
                  {busy[i] && <div className="text-[11px] text-warn" data-testid="folder-cleanup-busy">{busy[i]}</div>}
                </div>
              )
            })}
            </div>
          </div>
        </div>
      ) : (
        <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
          <input type="checkbox" checked={includeTopLevel} onChange={e => setIncludeTopLevel(e.target.checked)} aria-label={i18nT('pages.chatSidebar.include_top_level_folders')} data-testid="folder-cleanup-top-level" />
          {i18nT('pages.chatSidebar.include_top_level_folders')}
        </label>
      )}
      <div className="text-[12px] text-muted mb-3">
        {preview.isLoading
          ? i18nT('pages.chatSidebar.checking')
          : preview.isError
            ? (
              <span className="inline-flex items-center gap-2 flex-wrap">
                <ErrorNotice message={i18nT('pages.chatSidebar.failed_to_load_preview')} variant="inline" askAgent testId="folder-cleanup-preview-error" />
                <Btn className="text-[12px] px-2 py-0.5" onClick={() => preview.refetch()}>{i18nT('pages.chatSidebar.retry')}</Btn>
              </span>
            )
            : rows.length === 0
              ? (root ? toClose.length === 0 && i18nT('pages.chatSidebar.nothing_to_close_or_delete') : i18nT('pages.chatSidebar.no_empty_folders'))
              : (<>
                {root && <PanelSectionHeader className="mb-1" label={i18nT('pages.chatSidebar.folders_to_delete')} count={rows.length} />}
                <div className="max-h-40 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5" data-testid="folder-cleanup-list">
                  {rows.map(r => (
                    <div key={r.id} className="flex items-center gap-1 text-[12px] text-muted py-0.5 px-1">
                      <span className="truncate" title={r.path}>{r.path}</span>
                      {archived[r.id] ? (
                        <span
                          className="ml-auto shrink-0 inline-flex items-center gap-0.5 text-[11px] opacity-60"
                          title={i18nT('pages.chatSidebar.archived_sessions_kept', { count: archived[r.id] })}
                          aria-label={i18nT('pages.chatSidebar.archived_sessions_kept', { count: archived[r.id] })}
                          data-testid="folder-cleanup-archived"
                        ><Clock size={11} aria-hidden="true" />{i18nT('pages.chatSidebar.archived_count', { count: archived[r.id] })}</span>
                      ) : null}
                    </div>
                  ))}
                </div>
              </>)}
      </div>
      {notice && <div className="text-[12px] text-muted mb-2" role="status" data-testid="folder-cleanup-notice">{notice}</div>}
      <ErrorNotice message={error} askAgent className="mb-2" testId="folder-cleanup-error" />
      <div className="flex flex-wrap items-center gap-2 justify-end">
        {/* Disabled mid-run: closing the panel would not stop the run. */}
        <Btn className="text-[12px] px-3 py-1" disabled={cleanup.isPending} onClick={onClose}>{i18nT('pages.chatSidebar.cancel')}</Btn>
        {preview.isSuccess && (rows.length > 0 || toClose.length > 0) && (
          <Btn
            danger
            className="text-[12px] px-2 py-1 whitespace-nowrap"
            data-testid="folder-cleanup-confirm"
            disabled={cleanup.isPending || preview.isFetching}
            onClick={() => { setError(''); setNotice(''); cleanup.mutate() }}
          >{root
            ? (rootKept
              ? i18nT(cleanup.isPending ? 'pages.chatSidebar.closing_sessions' : 'pages.chatSidebar.close_sessions')
              : i18nT(cleanup.isPending ? 'pages.chatSidebar.closing_and_deleting' : 'pages.chatSidebar.close_and_delete'))
            : (cleanup.isPending ? i18nT('pages.chatSidebar.deleting_folders') : i18nT('pages.chatSidebar.delete_empty_folders', { count: rows.length }))}</Btn>
        )}
      </div>
    </div>
  )
}
