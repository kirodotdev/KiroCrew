import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { FileText, FilePen, Diff, FolderTree } from 'lucide-react'
import { api } from '../../api/client'
import type { SessionFile } from '../../api/client/files'
import ErrorNotice from '../../components/ErrorNotice'
import { EmptyState } from '../../components/ui'
import Clickable from '../../components/Clickable'
import { fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { reportForError } from '../../utils/errorReport'
import { pathBasename, relativeToRoot, stripTrailingSeparators } from '../../utils/pathBasename'

/** How often the open list re-reads. The server answers from a per-transcript
 *  cache of paths only, so a poll is a stat per listed file, not a transcript read. */
const SESSION_FILES_POLL_MS = 5_000

/** The query both the list and the tab header read; one request serves both. */
export function useSessionFiles(slot: string | null | undefined, active: boolean) {
  return useQuery({
    queryKey: ['session-files', slot ?? ''],
    queryFn: () => api.sessionFiles(slot ?? ''),
    enabled: !!slot && active,
    refetchInterval: q => (q.state.error ? false : SESSION_FILES_POLL_MS),
    refetchOnWindowFocus: true,
    retry: false,
    staleTime: 2_000,
  })
}

/** The folder a row names under its file name: relative to the project when the
 *  file sits inside it, else the absolute folder. Empty at the project root. */
function folderLabel(path: string, projectDir: string): string {
  const dir = stripTrailingSeparators(path.slice(0, path.length - pathBasename(path).length))
  if (!dir) return ''
  const root = stripTrailingSeparators(projectDir)
  if (root && dir === root) return ''
  const rel = relativeToRoot(dir, projectDir)
  return rel === dir ? dir : rel.replace(/^[\\/]+/, '')
}

/**
 * The Files tab's "Edited" view: every file this chat session created or
 * changed, newest first, one row each. The rows come from the session's own
 * transcript on the server (`GET /api/project/session-files`), so they are the
 * same after a reload and from any browser. A row opens the file; its second
 * control opens the change as a diff. A file deleted since is shown struck
 * through, with no diff control.
 */
export default function SessionFilesList({ slot, projectDir, active, onFileOpen, onOpenDiff, onBrowse }: {
  slot: string
  projectDir: string
  active: boolean
  onFileOpen: (absPath: string, diff: boolean) => void
  /** Open a recorded before/after pair as a diff tab. Without it, Show changes
   *  opens the working-tree diff. */
  onOpenDiff?: (absPath: string, modified: string, original: string) => void
  /** Switch the tab to its Browse view; offered from the empty state. */
  onBrowse?: () => void
}) {
  const { t } = useTranslation()
  const { data, error, isPending, refetch } = useSessionFiles(slot, active)
  const files: SessionFile[] = Array.isArray(data?.files) ? data.files : []

  // A recorded change that could not be read, said as itself: opening the
  // working-tree diff in its place would show a different change.
  // Held with its slot, so a notice never outlives the chat it was about.
  const [diffError, setDiffError] = useState<{ slot: string; path: string; error: unknown } | null>(null)
  // Only the newest click may report: an older request settling late is stale.
  const diffRequestRef = useRef(0)

  // Show changes is THIS chat's edit, from its record: a working-tree diff
  // compares with git's HEAD, which has no baseline outside a repository and
  // nothing to show once the edit is committed. When the record holds no
  // whole pair, the working-tree diff is the best the tab can show.
  const showChanges = async (path: string) => {
    setDiffError(null)
    const request = ++diffRequestRef.current
    if (onOpenDiff) {
      let diff
      try {
        diff = await api.sessionFileDiff(slot, path)
      } catch (error) {
        if (request === diffRequestRef.current) setDiffError({ slot, path, error })
        return
      }
      if (request !== diffRequestRef.current) return
      if (diff.available) {
        // As the transcript card does: a new file reads better as itself,
        // and an unchanged pair would be two identical panes.
        if (!diff.before.trim() || diff.before === diff.after) onFileOpen(path, false)
        else onOpenDiff(path, diff.after, diff.before)
        return
      }
    }
    onFileOpen(path, true)
  }

  if (error) {
    return (
      <div className="flex flex-col items-start gap-1.5 px-3 py-3">
        <ErrorNotice
          variant="inline"
          className="whitespace-normal max-w-full"
          message={t('pages.chat.filesHome.edited_failed')}
          report={reportForError(error)}
          askAgent
          testId="session-files-error"
        />
        <button
          onClick={() => { void refetch() }}
          className="text-[12px] px-2.5 h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border border-border"
        >{t('pages.chat.filesHome.retry')}</button>
      </div>
    )
  }
  if (isPending) {
    return <div role="status" className="px-3 py-3 text-[12px] text-muted">{t('pages.chat.activityViewer.loading_workspace')}</div>
  }
  // Every edit was withheld (a path the server will not name, more edits
  // than one turn records): that is not "nothing edited", so it says so.
  const browse = onBrowse && projectDir ? (
    <button
      onClick={onBrowse}
      className="flex items-center gap-1.5 text-[12px] px-2.5 h-[28px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border border-border"
    >
      <FolderTree size={13} />
      {t('pages.chat.filesHome.edited_browse')}
    </button>
  ) : undefined
  if (files.length === 0 && data?.withheld) {
    return (
      <EmptyState
        icon={<FilePen size={22} />}
        title={t('pages.chat.filesHome.edited_withheld_title')}
        testId="session-files-withheld"
        action={browse}
      />
    )
  }
  if (files.length === 0) {
    return (
      <EmptyState
        icon={<FilePen size={22} />}
        title={t('pages.chat.filesHome.edited_empty_title')}
        subtitle={t('pages.chat.filesHome.edited_empty_hint')}
        testId="session-files-empty"
        action={browse}
      />
    )
  }
  const now = Date.now()
  return (
    <div className="flex-1 min-h-0 overflow-y-auto py-1" data-testid="session-files-list">
      {diffError && diffError.slot === slot && (
        <div className="px-3 py-2">
          <ErrorNotice
            variant="inline"
            className="whitespace-normal max-w-full"
            // The file name is stored text: i18nT keeps it out of i18next's parser.
            message={i18nT('pages.chat.filesHome.edited_diff_failed', { name: pathBasename(diffError.path) || diffError.path })}
            report={reportForError(diffError.error)}
            askAgent
            testId="session-file-diff-error"
          />
        </div>
      )}
      <ul className="flex flex-col">
        {files.map(file => {
          const name = pathBasename(file.path) || file.path
          const folder = folderLabel(file.path, projectDir)
          const when = file.ts ? fmtRelative(file.ts, { now }) : ''
          return (
            <li key={file.path} className="group flex items-center gap-1 px-1.5" data-testid="session-file-row">
              <Clickable
                className="flex-1 min-w-0 flex items-center gap-2 px-1.5 py-1.5 rounded-md hover:bg-bg-hover cursor-pointer text-left"
                title={file.path}
                onClick={() => onFileOpen(file.path, false)}
              >
                <FileText size={14} className="shrink-0 opacity-60" />
                <span className="flex-1 min-w-0 flex flex-col">
                  <span className={file.exists ? 'text-[12.5px] text-text truncate' : 'text-[12.5px] text-muted line-through truncate'}>
                    {name}
                  </span>
                  {folder && <span className="text-[11px] text-muted truncate">{folder}</span>}
                </span>
                <span className="shrink-0 text-[11px] text-muted tabular-nums">
                  {file.exists ? when : t('pages.chat.filesHome.edited_deleted')}
                </span>
              </Clickable>
              {file.exists && (
                // Icon AND word: the diff glyph alone reads as an action that
                // could change the file, and this is the list's main path.
                <button
                  onClick={() => { void showChanges(file.path) }}
                  className="shrink-0 flex items-center gap-1 h-[26px] px-2 rounded-md cursor-pointer transition-colors text-[11.5px] text-muted hover:text-text hover:bg-bg-hover bg-transparent border border-border"
                  title={t('pages.chat.filesHome.edited_open_diff')}
                  // The file name is stored text: i18nT keeps it out of i18next's parser.
                  aria-label={i18nT('pages.chat.filesHome.edited_open_diff_for', { name })}
                  data-testid="session-file-diff"
                >
                  <Diff size={13} />
                  {t('pages.chat.filesHome.edited_open_diff')}
                </button>
              )}
            </li>
          )
        })}
      </ul>
      {/* Two reasons a list is short, each said as itself: the cap left the
          oldest out, or some edits cannot be named here at all. */}
      {data?.truncated && (
        <div className="px-3 py-2 text-[11px] text-muted" data-testid="session-files-truncated">
          {t('pages.chat.filesHome.edited_truncated')}
        </div>
      )}
      {data?.withheld && (
        <div className="px-3 py-2 text-[11px] text-muted" data-testid="session-files-withheld-note">
          {t('pages.chat.filesHome.edited_withheld_title')}
        </div>
      )}
    </div>
  )
}
