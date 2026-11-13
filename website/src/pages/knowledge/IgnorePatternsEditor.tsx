import { useId, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { knowledgeApi } from './api'
import { parseSourceProps } from './knowledgeUtils'
import type { Source } from './types'

/** A folder source's ignore patterns, editable after the source is added. */
export default function IgnorePatternsEditor({ source }: { source: Source }) {
  const queryClient = useQueryClient()
  const saved = (parseSourceProps(source).ignorePatterns ?? []).join('\n')
  const [draft, setDraft] = useState(saved)
  const boxId = useId()
  const patterns = draft.split('\n').map(s => s.trim()).filter(Boolean)
  const saveMutation = useMutation({
    mutationFn: (ignore_patterns: string[]) => knowledgeApi(`/sources/${source.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ignore_patterns }),
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] }),
  })

  return (
    <div className="border-t border-border mt-2 pt-2 space-y-1.5" data-testid="source-ignore-patterns">
      <label id={`${boxId}-label`} htmlFor={boxId} className="block text-[11px] font-medium text-muted">
        {i18nT('pages.knowledge.sourcesList.ignore_patterns')}
      </label>
      {/* Visible, not only the placeholder: a source with saved patterns never shows it. */}
      <p id={`${boxId}-help`} className="text-[11px] text-muted">
        {i18nT('pages.knowledge.sourcesList.ignore_patterns_help')}
      </p>
      <textarea id={boxId} aria-labelledby={`${boxId}-label`} aria-describedby={`${boxId}-help`}
        value={draft} onChange={e => setDraft(e.target.value)}
        placeholder={i18nT('pages.knowledge.sourcesList.ignore_patterns_one_per_line_e_g_trash_templates')}
        rows={3} className="w-full px-3 py-1.5 text-sm bg-bg rounded border border-border text-text resize-none" />
      <div className="flex items-center gap-2">
        <span className="flex-1 text-[11px] text-muted">{i18nT('pages.knowledge.sourcesList.ignore_patterns_keep_indexed_files')}</span>
        {/* Busy uses `aria-disabled` plus the guard, NOT `disabled`, which leaves
            the tab order and drops a keyboard user's focus while the save runs.
            The unchanged state stays native-disabled: it is inert, not in flight. */}
        <Btn onClick={() => { if (!saveMutation.isPending) saveMutation.mutate(patterns) }}
          aria-disabled={saveMutation.isPending || undefined} aria-busy={saveMutation.isPending || undefined}
          disabled={!saveMutation.isPending && patterns.join('\n') === saved}
          className="aria-disabled:opacity-30 aria-disabled:cursor-not-allowed">
          {i18nT('pages.knowledge.sourcesList.save_ignore_patterns')}
        </Btn>
      </div>
      {/* No hand-off: the box above holds the unsaved pattern draft. */}
      <ErrorNotice variant="inline" testId="source-ignore-patterns-error"
        message={saveMutation.isError ? saveMutation.error?.message : ''} />
    </div>
  )
}
