import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { FileWarning, Trash2 } from 'lucide-react'
import { api, ApiError } from '../../api/client'
import { Btn, PanelSectionHeader } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { parseErrorCode } from '../../utils/errorReport'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'

/** One entry of `GET /api/agents/templates`'s `unlisted`: a spec file the list
 *  does not show, although its stem stays taken, so creating that name is
 *  refused. `deletable` says whether a delete by its stem reaches this file
 *  alone; the server still decides. */
export interface UnlistedSpec {
  filename: string
  stem: string
  reason: 'unreadable' | 'duplicate_name'
  declared_name?: string
  listed_as?: string
  deletable: boolean
}

const reasonText = (e: UnlistedSpec): string => e.reason === 'duplicate_name'
  ? i18nT('pages.overview.agentTemplatesTab.unlisted_reason_duplicate', { name: e.declared_name ?? '', file: e.listed_as ?? '' })
  : i18nT('pages.overview.agentTemplatesTab.unlisted_reason_unreadable')

/** What the delete leaves. A same-name copy goes alone: the listed file that
 *  holds its declared name, and the agent of that name, stay as they are. */
const confirmBody = (e: UnlistedSpec): string => e.reason === 'duplicate_name'
  ? i18nT('pages.overview.agentTemplatesTab.unlisted_delete_confirm_body_duplicate', {
    file: e.listed_as ?? '', name: e.declared_name ?? '', stem: e.stem,
  })
  : i18nT('pages.overview.agentTemplatesTab.unlisted_delete_confirm_body')

/**
 * The files in the agents directory the list cannot show, each with why and
 * how to free its name: Delete file when the server can reach the file by its
 * stem alone, otherwise the instruction to rename or delete it by hand. A delete
 * refused because something still names the file hands the reference list to
 * the tab's dialog, the one a row's delete opens.
 */
export default function UnlistedSpecFiles({ entries, onReferenced, className }: {
  entries: UnlistedSpec[]
  onReferenced: (name: string, body: string) => void
  className?: string
}) {
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [error, setError] = useState('')
  const [deleted, setDeleted] = useState('')
  const remove = useMutation({
    mutationFn: (e: UnlistedSpec) => api.agentTemplateDelete(e.stem),
    onSuccess: (_r, e) => {
      setError('')
      setDeleted(i18nT('pages.overview.agentTemplatesTab.deleted', { name: e.filename }))
      void queryClient.invalidateQueries({ queryKey: ['agent-templates'] })
    },
    onError: (err: unknown, e) => {
      setDeleted('')
      if (err instanceof ApiError && parseErrorCode(err.body) === 'template_referenced') {
        onReferenced(e.stem, err.body)
        return
      }
      setError(errMessage(err) || i18nT('pages.overview.agentTemplatesTab.err_generic'))
    },
  })
  const askThenDelete = async (e: UnlistedSpec) => {
    const yes = await confirm({
      title: i18nT('pages.overview.agentTemplatesTab.unlisted_delete_confirm', { file: e.filename }),
      body: confirmBody(e),
      confirmLabel: i18nT('pages.overview.agentTemplatesTab.unlisted_delete_button'),
    })
    if (yes) remove.mutate(e)
  }
  if (entries.length === 0) return null
  const title = i18nT('pages.overview.agentTemplatesTab.unlisted_title')
  return (
    <section className={className} aria-label={title} data-testid="unlisted-spec-files">
      <PanelSectionHeader label={title} count={entries.length} className="px-2 pt-2 pb-1" />
      <p className="px-2 pb-1 text-[11px] text-muted">{i18nT('pages.overview.agentTemplatesTab.unlisted_hint')}</p>
      {entries.map(e => (
        <div key={e.filename} className="flex items-start gap-2 px-2 py-1.5">
          <FileWarning className="lucide-inline mt-0.5 shrink-0 text-muted" aria-hidden />
          <div className="min-w-0 flex-1">
            <code className="block truncate font-mono text-[12px] text-text" title={e.filename}>{e.filename}</code>
            <span className="block text-[11px] text-muted">{reasonText(e)}</span>
            {e.deletable
              ? (
                <Btn
                  danger
                  className="mt-1 text-[12px]"
                  disabled={remove.isPending}
                  aria-label={i18nT('pages.overview.agentTemplatesTab.unlisted_delete_aria', { file: e.filename })}
                  onClick={() => void askThenDelete(e)}
                >
                  <Trash2 className="lucide-inline" aria-hidden /> {i18nT('pages.overview.agentTemplatesTab.unlisted_delete_button')}
                </Btn>
              )
              : <span className="block text-[11px] text-muted">{i18nT('pages.overview.agentTemplatesTab.unlisted_by_hand')}</span>}
          </div>
        </div>
      ))}
      {deleted && <p className="px-2 pt-1 text-[12px] text-ok" role="status">{deleted}</p>}
      {/* No hand-off: the template editor beside this list keeps its unsaved
          draft (`draft`, `dirty`) in local state, and the chat a hand-off opens
          unmounts the tab and throws that draft away. */}
      <ErrorNotice message={error} variant="inline" className="mx-2 mt-1" onDismiss={error ? () => setError('') : undefined} />
      {confirmDialog}
    </section>
  )
}
