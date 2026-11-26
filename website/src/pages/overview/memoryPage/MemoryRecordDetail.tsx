/**
 * One record in full beside the list (a sheet on a phone): its text, every
 * field the row summarizes, where it came from, its history and its actions.
 */
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { X } from 'lucide-react'
import { Btn, IconButton } from '../../../components/ui'
import Modal from '../../../components/Modal'
import { fmtDateTimeNumeric } from '../../../i18n/format'
import type { MemoryRecord } from '../../../types/memoryEditing'
import { MEMORY_RECORD_LABELS, RecordHistory, RecordProvenance, memoryRecordBody } from '../memoryRecordParts'
import { SCOPE_UNUSABLE, facetLabel, lessonFacts, recordTags, sourceGroup } from './memoryListView'
import { FilterChip, fmtScore, type ChipFilter } from './MemoryListParts'
import { StrengthDetail } from './MemoryStrength'

export function MemoryRecordDetail({ store, row, phone, chip, fadeLine, onSaveAsLesson, onClose, onEdit, onForget, onRestore, onKeepCurrent, onProposal }: {
  store: string; row: MemoryRecord; phone: boolean
  /** The store's fade line (`memory_decay.FADE_LINE`); a store that does not fade has none. */
  fadeLine?: number
  onSaveAsLesson?: () => void
  /** Tags filter the list, as they do in its rows. */
  chip?: ChipFilter
  onClose: () => void; onEdit: () => void; onForget: () => void
  onRestore: (revisionId: number) => void; onKeepCurrent: () => void; onProposal: (proposal: MemoryRecord, operation: 'set' | 'forget') => void
}) {
  const { t } = useTranslation()
  const lesson = row.kind === 'directive' ? lessonFacts(row) : null
  const score = typeof row.importance === 'number' ? row.importance : null
  const title = lesson ? t('memoryPage.lesson_category', { kind: t(MEMORY_RECORD_LABELS.directive), category: facetLabel('category', lesson.category) }) : t(MEMORY_RECORD_LABELS[row.kind])
  const fields: [string, ReactNode][] = []
  if (row.key) fields.push([t('memoryV2.memory_key'), <code className="break-all text-[12px]">{row.key}</code>])
  if (lesson) {
    fields.push([t('memoryPage.facet_applies'), facetLabel('applies', lesson.applies)])
    fields.push([t('pages.overview.memoryTab.scope'), <span className={lesson.scope === SCOPE_UNUSABLE ? 'text-warn-text' : 'break-all'}>{facetLabel('scope', lesson.scope)}</span>])
  }
  if (score !== null) fields.push([t(row.kind === 'episode' ? 'memoryPage.facet_importance' : 'memoryPage.facet_confidence'), <span className={`tabular-nums ${row.kind !== 'episode' && score < 0.5 ? 'text-danger-text' : ''}`}>{fmtScore(score)}</span>])
  if (row.kind === 'episode' && recordTags(row).length) fields.push([t('memoryPage.facet_tags'), <span className="flex flex-wrap gap-1.5">{recordTags(row).map(tag => <FilterChip key={tag} group="tag" value={tag} tone="bg-bg-hover px-2 text-muted" chip={chip}>#{tag}</FilterChip>)}</span>])
  fields.push([t('pages.overview.memoryTab.source'), <span title={row.source || undefined}>{facetLabel('source', sourceGroup(row.source))}</span>])
  if (row.created_at) fields.push([t('memoryPage.field_created'), fmtDateTimeNumeric(row.created_at)])
  if (row.updated_at && row.updated_at !== row.created_at) fields.push([t('memoryPage.field_updated'), fmtDateTimeNumeric(row.updated_at)])
  const body = <div className="space-y-4">
    {lesson?.scope === SCOPE_UNUSABLE && <p className="rounded-lg bg-warn-subtle px-3 py-2.5 text-[13px] leading-5 text-warn-text">{t('pages.overview.memoryTab.scope_unusable_hint')}</p>}
    <p className={`whitespace-pre-wrap break-words leading-6 text-text [overflow-wrap:anywhere] ${row.kind === 'fact' ? 'rounded-lg bg-bg-hover px-3 py-2.5 text-[14px]' : 'text-[15px]'}`}>{memoryRecordBody(row)}</p>
    {lesson?.negative && <p className="text-[13px] leading-5 text-muted">{t('memoryPage.lesson_dont', { text: lesson.negative })}</p>}
    {!!row.metadata?.email_addresses?.length && <p className="break-all text-[13px] text-text">{row.metadata.email_addresses.join(', ')}</p>}
    <dl className="m-0 grid gap-x-3 gap-y-2.5 text-[13px] leading-5" style={{ gridTemplateColumns: '96px minmax(0,1fr)' }}>
      {fields.map(([label, value]) => [<dt key={`${label}:k`} className="text-muted">{label}</dt>, <dd key={label} className="m-0 min-w-0 text-text">{value}</dd>])}
    </dl>
    {row.decay && fadeLine !== undefined && <StrengthDetail decay={row.decay} fadeLine={fadeLine} written={row.created_at} onSaveAsLesson={onSaveAsLesson} />}
    <RecordProvenance row={row} source={false} />
    <RecordHistory store={store} row={row} onRestore={onRestore} onKeepCurrent={onKeepCurrent} onProposal={onProposal} />
  </div>
  const actions = <div className="flex flex-wrap gap-2">
    <Btn className="min-h-11" disabled={!row.revision} onClick={onEdit}>{t('memoryV2.edit_action')}</Btn>
    <Btn danger className="min-h-11" disabled={!row.revision} onClick={onForget}>{t('memoryV2.forget')}</Btn>
  </div>
  if (phone) return <Modal open title={title} onClose={onClose} footer={actions}>{body}</Modal>
  return <aside aria-label={title} className="w-full space-y-4 rounded-2xl border border-border bg-card p-5" data-testid="memory-record-detail">
    <div className="flex items-center justify-between gap-2"><h3 className="m-0 text-[15px] font-semibold text-text">{title}</h3><IconButton aria-label={t('components.modal.close')} className="flex h-8 w-8 items-center justify-center" onClick={onClose}><X className="lucide-inline" aria-hidden="true" /></IconButton></div>
    {body}
    <div className="border-t border-border pt-3">{actions}</div>
  </aside>
}
