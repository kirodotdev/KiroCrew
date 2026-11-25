/**
 * New memory (a global fact: key and value) and New lesson (a rule and its
 * category), the two forms the memory page's header opens. Both write the global
 * store only, as their routes do: `PUT /api/memory/semantic` and `POST /api/lessons`.
 */
import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../../api/client'
import { Btn, Input } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import Modal from '../../../components/Modal'
import SegmentedControl from '../../../components/SegmentedControl'
import { semanticWriteErrorMessage } from '../embeddingStatusText'
import { errorText } from '../memoryRecordParts'
import { facetLabel } from './memoryListView'

/** Report an unsaved draft to the page's leave guard while the dialog is open; cleared when it closes. */
function useDraftDirty(dirty: boolean, onDirtyChange?: (dirty: boolean) => void) {
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false) }, [dirty, onDirtyChange])
}

export function NewMemoryDialog({ onClose, onSaved, onDirtyChange }: { onClose: () => void; onSaved: (key: string) => void; onDirtyChange?: (dirty: boolean) => void }) {
  const { t } = useTranslation()
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const save = useMutation({ mutationFn: () => api.vectorSemanticWrite(key.trim(), value), onSuccess: () => onSaved(key.trim()) })
  const ready = !!key.trim() && !!value.trim() && !save.isPending
  useDraftDirty(!!key || !!value, onDirtyChange)
  return <Modal open title={t('memoryPage.new_memory')} maxWidth={480} guardAccidentalDismiss={!!key || !!value} dismissDisabled={save.isPending} onClose={onClose} footer={<div className="flex justify-end gap-2">
    <Btn className="min-h-11" disabled={save.isPending} onClick={onClose}>{t('pages.overview.memoryTab.cancel')}</Btn>
    <Btn primary type="submit" form="memory-new-fact" className="min-h-11" disabled={!ready}>{t('pages.overview.memoryTab.save')}</Btn>
  </div>}>
    <form id="memory-new-fact" className="space-y-3" onSubmit={event => { event.preventDefault(); if (ready) save.mutate() }}>
      <label className="block space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('memoryV2.memory_key')}</span>
        <Input className="min-h-11 w-full font-mono" value={key} placeholder={t('pages.overview.vectorMemoryCard.key_e_g_pref_backend_framework')} disabled={save.isPending} onChange={event => { setKey(event.target.value); save.reset() }} /></label>
      <label className="block space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('pages.overview.vectorMemoryCard.value')}</span>
        <Input className="min-h-11 w-full" value={value} disabled={save.isPending} onChange={event => { setValue(event.target.value); save.reset() }} /></label>
      <p className="text-[12px] text-muted">{t('memoryPage.new_memory_hint')}</p>
      {/* No hand-off: retain the fact key and value when a write fails. */}
      <ErrorNotice message={semanticWriteErrorMessage(save.error) || errorText(save.error)} />
    </form>
  </Modal>
}

const CATEGORIES = ['knowledge', 'tool', 'preference'] as const
type LessonAnswer = { tone: 'info' | 'warning' | 'error'; text: string }

export function NewLessonDialog({ onClose, onSaved, onDirtyChange }: { onClose: () => void; onSaved: () => void; onDirtyChange?: (dirty: boolean) => void }) {
  const { t } = useTranslation()
  const [rule, setRule] = useState('')
  const [category, setCategory] = useState<(typeof CATEGORIES)[number]>('knowledge')
  const [answer, setAnswer] = useState<LessonAnswer | null>(null)
  const save = useMutation({
    mutationFn: () => api.createLesson(rule.trim(), category),
    onSuccess: result => {
      if (result.outcome === 'inserted' || result.outcome === 'enriched') { onSaved(); return }
      if (result.outcome === 'unchanged') setAnswer({ tone: 'info', text: t('pages.overview.memoryTab.lesson_already_stored') })
      else if (result.outcome === 'deduped') setAnswer({ tone: 'warning', text: t('pages.overview.memoryTab.lesson_already_covered', { reason: result.reason }) })
      else setAnswer({ tone: 'error', text: t('pages.overview.memoryTab.lesson_not_saved', { reason: result.reason }) })
    },
  })
  const ready = !!rule.trim() && !save.isPending
  useDraftDirty(!!rule, onDirtyChange)
  return <Modal open title={t('memoryPage.new_lesson')} maxWidth={480} guardAccidentalDismiss={!!rule} dismissDisabled={save.isPending} onClose={onClose} footer={<div className="flex justify-end gap-2">
    <Btn className="min-h-11" disabled={save.isPending} onClick={onClose}>{t('pages.overview.memoryTab.cancel')}</Btn>
    <Btn primary type="submit" form="memory-new-lesson" className="min-h-11" disabled={!ready}>{t('pages.overview.memoryFailuresCard.save_lesson')}</Btn>
  </div>}>
    <form id="memory-new-lesson" className="space-y-3" onSubmit={event => { event.preventDefault(); if (ready) { setAnswer(null); save.mutate() } }}>
      <label className="block space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('pages.overview.memoryTab.rule')}</span>
        <textarea rows={3} aria-label={t('pages.overview.memoryTab.rule')} className="w-full rounded-lg border border-border bg-bg-elevated p-3 text-[14px] leading-5 text-text focus-ring" value={rule} placeholder={t('pages.overview.memoryTab.rule_e_g_always_use_tabs_not_spaces')} disabled={save.isPending} onChange={event => { setRule(event.target.value); setAnswer(null) }} /></label>
      <div className="space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('pages.overview.memoryTab.category')}</span>
        <SegmentedControl collapse={false} layoutId="memory-new-lesson-category" value={category} onChange={setCategory} segments={CATEGORIES.map(key => ({ key, label: facetLabel('category', key) }))} /></div>
      {answer && answer.tone !== 'error' && <p role="status" className={`rounded-lg px-3 py-2 text-[13px] leading-5 ${answer.tone === 'warning' ? 'bg-warn-subtle text-warn-text' : 'bg-bg-hover text-text'}`}>{answer.text}</p>}
      {/* No hand-off: the rule being typed is an unsaved draft. */}
      <ErrorNotice message={answer?.tone === 'error' ? answer.text : null} />
      {/* No hand-off: the rule being typed is an unsaved draft. */}
      <ErrorNotice title={t('pages.overview.memoryTab.lesson_request_failed')} message={errorText(save.error)} />
    </form>
  </Modal>
}
