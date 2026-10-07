/** The per-folder closed-sessions row: a folder's CLOSED sessions, listed in
 *  place under the folder they were filed in.
 *
 *  Closing a session frees its runtime and keeps its `folder_id`; this row
 *  shows that set without leaving the tree for the flat Older Sessions pane,
 *  and nothing more: collapsed by default (so the active tree reads as before),
 *  fetched only when opened, and a click resumes the session exactly as an
 *  Older Sessions row does. The Older Sessions pane still lists everything
 *  closed, filed or not.
 *
 *  `count` is the folder's `closed_count`, computed server-side with the same
 *  predicates as the list this row opens, so the label and the list agree.
 *
 *  Holds NO state of its own. The folder tree re-creates its body nodes on
 *  every sidebar render, so local state here would reset on each slot frame:
 *  `open` is owned by the sidebar (like the dormant expander's set) and the
 *  list lives in the query cache, keyed by folder and count. */
import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { offlineProps } from '../../utils/offline'
import { i18nT } from '../../i18n/t'
import type { SessionInfo } from '../../types'
import { fmtRelativeTime } from '../chat/sessionOrder'

/** One page is the whole in-place view; past it the row offers Older Sessions. */
export const FOLDER_CLOSED_LIMIT = 50
/** Past this many rows the toggle has likely scrolled away, so the list ends
 *  with a second one. */
export const FOLDER_CLOSED_FOOT_TOGGLE_AT = 10

export interface FolderClosedSectionProps {
  folderId: string
  folderName: string
  /** Closed sessions filed here (the folder's `closed_count`). */
  count: number
  open: boolean
  onToggle: () => void
  connected: boolean
  onResume: (session: { key: string; title: string }) => void
  onOpenOlderSessions: () => void
  renderChevron: (open: boolean) => ReactNode
}

interface ClosedPage { sessions: SessionInfo[]; hasMore: boolean }

const NOTE_CLS = 'pl-2.5 pr-3 py-1 text-[11px]'

export function FolderClosedSection({ folderId, folderName, count, open, onToggle, connected, onResume, onOpenOlderSessions, renderChevron }: FolderClosedSectionProps) {
  const query = useQuery<ClosedPage>({
    // The count is in the key so a session closing or reopening here refetches.
    queryKey: ['folder-closed', folderId, count],
    enabled: open,
    // Same two narrowings as the Older Sessions pane (closed sessions only, no
    // machine transcripts), scoped server-side to this folder.
    queryFn: async () => {
      const d = await api.sessions(FOLDER_CLOSED_LIMIT, 0, false, true, true, folderId) as { sessions?: SessionInfo[]; has_more?: boolean }
      return { sessions: d.sessions ?? [], hasMore: !!d.has_more }
    },
    // A count change is a new key; keep the previous list on screen meanwhile.
    placeholderData: prev => prev,
    // Never served from cache: the count can return to an earlier value (close
    // one, reopen another) while the set it names has changed.
    staleTime: 0,
    retry: false,
  })

  const regionId = `closed-rows-${folderId}`
  const rows = query.data?.sessions ?? []
  const label = open
    ? i18nT('pages.chatSidebar.folder_closed_hide', { count })
    : i18nT('pages.chatSidebar.folder_closed_show', { count })

  return (
    <>
      <button type="button"
        aria-expanded={open}
        aria-controls={regionId}
        title={i18nT('pages.chatSidebar.folder_closed_hint', { name: folderName })}
        data-testid={`folder-closed-toggle-${folderId}`}
        onClick={onToggle}
        className="w-full flex items-center gap-1.5 pl-2.5 pr-3 py-0.5 rounded-md text-[11px] leading-4 text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
        {renderChevron(open)}
        <span className="tabular-nums">{label}</span>
      </button>
      <div id={regionId} data-testid={`folder-closed-region-${folderId}`} hidden={!open}>
        {open && query.isPending && (
          <div className={`${NOTE_CLS} text-muted`}>{i18nT('pages.chatSidebar.folder_closed_loading')}</div>
        )}
        {open && query.isError && (
          // Block with the hand-off below the text: the sidebar is too narrow for
          // a side-by-side hand-off (see ErrorNotice's `actionPlacement`).
          <ErrorNotice message={i18nT('pages.chatSidebar.folder_closed_load_failed')} askAgent actionPlacement="below"
            className="my-1" testId={`folder-closed-error-${folderId}`} />
        )}
        {open && query.isSuccess && rows.length === 0 && (
          <div className={`${NOTE_CLS} text-muted`}>{i18nT('pages.chatSidebar.folder_closed_none')}</div>
        )}
        {open && rows.map(s => {
          const title = s.title || s.key
          const when = fmtRelativeTime(s.modified ?? s.created)
          return (
            <button key={s.key} type="button"
              data-testid={`folder-closed-row-${s.key}`}
              disabled={!connected}
              title={i18nT('pages.chatSidebar.folder_closed_reopen', { title })}
              {...offlineProps(connected, 'resume sessions')}
              onClick={() => onResume({ key: s.key, title })}
              className="w-full flex items-center gap-2 pl-2.5 pr-3 py-1 rounded-md text-[12px] text-muted opacity-75 hover:opacity-100 hover:text-text hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left disabled:cursor-not-allowed disabled:opacity-50">
              <span className="truncate">{title}</span>
              {when && <span className="ml-auto text-[11px] shrink-0">{when}</span>}
            </button>
          )
        })}
        {open && rows.length > FOLDER_CLOSED_FOOT_TOGGLE_AT && (
          <button type="button" data-testid={`folder-closed-foot-toggle-${folderId}`} aria-controls={regionId} aria-expanded onClick={onToggle}
            className="w-full flex items-center gap-1.5 pl-2.5 pr-3 py-0.5 rounded-md text-[11px] leading-4 text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
            {renderChevron(true)}
            <span className="tabular-nums">{label}</span>
          </button>
        )}
        {open && query.data?.hasMore && (
          <button type="button" data-testid={`folder-closed-more-${folderId}`} onClick={onOpenOlderSessions}
            className={`${NOTE_CLS} w-full text-left text-accent hover:underline bg-transparent border-none cursor-pointer`}>
            {i18nT('pages.chatSidebar.folder_closed_more')}
          </button>
        )}
      </div>
    </>
  )
}
