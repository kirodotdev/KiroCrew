/**
 * The body of the SDK's `SessionChanges`, behind its lazy boundary: the chat's
 * "Edited" list beside the diff of the file picked in it. The client both read
 * through is the one `SessionChanges` put in context (see ./SessionChanges).
 */
import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Diff } from 'lucide-react'
import DiffPanel from '../components/DiffPanel'
import ErrorNotice from '../components/ErrorNotice'
import { ContentSkeleton, EmptyState } from '../components/ui'
import SessionFilesList, { useSessionFiles } from '../pages/chat/SessionFilesList'
import { useSessionFilesClient } from '../pages/chat/sessionFilesClient'
import { i18nT } from '../i18n/t'
import { reportForError } from '../utils/errorReport'
import { pathBasename } from '../utils/pathBasename'

function FileDiff({ slot, path, changedAt }: { slot: string; path: string; changedAt: string }) {
  const { t } = useTranslation()
  const client = useSessionFilesClient()
  // The change time is in the key, so a newer edit to the file reads its diff again.
  const { data, error, isPending } = useQuery({
    queryKey: ['session-file-diff', slot, path, changedAt],
    queryFn: () => client.sessionFileDiff(slot, path),
    retry: false,
    staleTime: Infinity,
  })
  if (isPending) return <ContentSkeleton rows={6} />
  if (error) {
    return (
      <div className="px-3 py-3">
        <ErrorNotice
          variant="inline"
          className="whitespace-normal max-w-full"
          // The file name is stored text: i18nT keeps it out of i18next's parser.
          message={i18nT('pages.chat.filesHome.edited_diff_failed', { name: pathBasename(path) || path })}
          report={reportForError(error)}
          askAgent
          testId="app-session-changes-diff-error"
        />
      </div>
    )
  }
  if (!data.available) {
    return (
      <EmptyState
        icon={<Diff size={22} />}
        title={t('appSdk.sessionChanges.diff_unavailable')}
        testId="app-session-changes-diff-unavailable"
      />
    )
  }
  return <DiffPanel filePath={path} original={data.before} modified={data.after} />
}

export default function SessionChangesBody({ slot, projectDir, active, diffRefusal }: {
  slot: string
  projectDir: string
  active: boolean
  /** Shown in the diff's place when the app may not read diffs. */
  diffRefusal: ReactNode
}) {
  const { t } = useTranslation()
  // Held with its slot, so a pick never outlives the chat it was made in.
  const [picked, setPicked] = useState<{ slot: string; path: string } | null>(null)
  // The list's own query: the same key, so this reads its cache and sends nothing more.
  const { data } = useSessionFiles(slot, active)
  const path = picked?.slot === slot ? picked.path : ''
  const files = Array.isArray(data?.files) ? data.files : []
  const changedAt = files.find(f => f.path === path)?.ts ?? ''

  let pane: ReactNode
  if (!path) {
    pane = <EmptyState icon={<Diff size={22} />} title={t('appSdk.sessionChanges.pick_a_file')} testId="app-session-changes-none" />
  } else if (diffRefusal) {
    pane = <div className="px-3 py-3">{diffRefusal}</div>
  } else {
    pane = <FileDiff slot={slot} path={path} changedAt={changedAt} />
  }

  return (
    <div className="w-full h-full min-h-0 flex flex-col md:flex-row" data-testid="app-session-changes">
      {/* Stacked on a narrow page, the list scrolls within its share so a long
          one never pushes the diff out of view. */}
      <div className="min-h-0 max-h-[40%] md:max-h-none flex flex-col border-b md:border-b-0 md:border-r border-border md:w-[300px] md:shrink-0">
        <SessionFilesList
          slot={slot}
          projectDir={projectDir}
          active={active}
          onFileOpen={filePath => setPicked({ slot, path: filePath })}
        />
      </div>
      <div className="flex-1 min-w-0 min-h-0 flex flex-col" data-testid="app-session-changes-diff">{pane}</div>
    </div>
  )
}
