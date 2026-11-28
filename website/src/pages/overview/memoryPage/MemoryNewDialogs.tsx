/**
 * New memory: a global fact (key and value) or a lesson (a rule and its category),
 * picked at the top of the one form the memory page's header opens. Both write the
 * global store only, as their routes do: `PUT /api/memory/semantic` and `POST /api/lessons`.
 */
import { useEffect, useId, useState } from 'react'
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

// Keys a fact can take (`vector_memory._BUILTIN_PREFIXES` is pref.*, project.*, user.*),
// offered as suggestions on the key field; the store refuses any other prefix.
const KEY_HINTS = [
  'pref.frontend.', 'pref.backend.', 'pref.streaming.', 'pref.editor.', 'pref.os', 'pref.shell',
  'pref.style.', 'pref.communication.', 'pref.testing.', 'pref.deployment.',
  'project.name', 'project.repo', 'project.stack', 'project.storage', 'project.description',
  'user.name', 'user.timezone', 'user.team', 'user.role',
]

/** Report an unsaved draft to the page's leave guard while the dialog is open; cleared when it closes. */
function useDraftDirty(dirty: boolean, onDirtyChange?: (dirty: boolean) => void) {
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false) }, [dirty, onDirtyChange])
}

const CATEGORIES = ['knowledge', 'tool', 'preference'] as const
type LessonAnswer = { tone: 'info' | 'warning' | 'error'; text: string }
type NewKind = 'fact' | 'directive'
const KINDS: NewKind[] = ['fact', 'directive']

export function NewMemoryDialog({ initialRule = '', onClose, onSaved, onLessonSaved, onDirtyChange }: {
  /** Open on a lesson with this rule typed in (an experience saved so it stops fading). */
  initialRule?: string
  onClose: () => void; onSaved: (key: string) => void; onLessonSaved: () => void; onDirtyChange?: (dirty: boolean) => void }) {
  const { t } = useTranslation()
  const [kind, setKind] = useState<NewKind>(initialRule ? 'directive' : 'fact')
  const [key, setKey] = useState('')
  // A selector-safe id: `list` is resolved by id, and the generated one carries colons.
  const hintsId = `memory-key-hints${useId().replace(/:/g, '')}`
  const [value, setValue] = useState('')
  const [rule, setRule] = useState(initialRule)
  const [category, setCategory] = useState<(typeof CATEGORIES)[number]>('knowledge')
  const [answer, setAnswer] = useState<LessonAnswer | null>(null)
  const saveFact = useMutation({ mutationFn: () => api.vectorSemanticWrite(key.trim(), value), onSuccess: () => onSaved(key.trim()) })
  const saveLesson = useMutation({
    mutationFn: () => api.createLesson(rule.trim(), category),
    onSuccess: result => {
      if (result.outcome === 'inserted' || result.outcome === 'enriched') { onLessonSaved(); return }
      if (result.outcome === 'unchanged') setAnswer({ tone: 'info', text: t('pages.overview.memoryTab.lesson_already_stored') })
      else if (result.outcome === 'deduped') setAnswer({ tone: 'warning', text: t('pages.overview.memoryTab.lesson_already_covered', { reason: result.reason }) })
      else setAnswer({ tone: 'error', text: t('pages.overview.memoryTab.lesson_not_saved', { reason: result.reason }) })
    },
  })
  const pending = saveFact.isPending || saveLesson.isPending
  const ready = !pending && (kind === 'fact' ? !!key.trim() && !!value.trim() : !!rule.trim())
  // Both drafts stay while the kind switches, so either one keeps the dialog guarded.
  // A rule brought in from an experience is still stored there, so only a change
  // to it, or a picked category, is a draft.
  const dirty = !!key || !!value || (!!rule && rule !== initialRule) || category !== 'knowledge'
  useDraftDirty(dirty, onDirtyChange)
  const submit = () => { if (!ready) return; if (kind === 'fact') saveFact.mutate(); else { setAnswer(null); saveLesson.mutate() } }
  return <Modal open title={t('memoryPage.new_memory')} maxWidth={480} guardAccidentalDismiss={dirty} dismissDisabled={pending} onClose={onClose} footer={<div className="flex justify-end gap-2">
    <Btn className="min-h-11" disabled={pending} onClick={onClose}>{t('pages.overview.memoryTab.cancel')}</Btn>
    <Btn primary type="submit" form="memory-new" className="min-h-11" disabled={!ready}>{kind === 'fact' ? t('pages.overview.memoryTab.save') : t('pages.overview.memoryFailuresCard.save_lesson')}</Btn>
  </div>}>
    <form id="memory-new" className="space-y-3" onSubmit={event => { event.preventDefault(); submit() }}>
      <div className="space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('memoryV2.group_kind')}</span>
        <SegmentedControl collapse={false} layoutId="memory-new-kind" value={kind} onChange={next => { if (!pending) setKind(next) }} segments={KINDS.map(k => ({ key: k, label: t(k === 'fact' ? 'memoryV2.fact' : 'memoryV2.directive') }))} /></div>
      {kind === 'fact' ? <>
        <label className="block space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('memoryV2.memory_key')}</span>
          <Input className="min-h-11 w-full font-mono" list={hintsId} value={key} placeholder={t('pages.overview.vectorMemoryCard.key_e_g_pref_backend_framework')} disabled={pending} onChange={event => { setKey(event.target.value); saveFact.reset() }} /></label>
        <datalist id={hintsId}>{KEY_HINTS.map(hint => <option key={hint} value={hint}>{hint}</option>)}</datalist>
        <label className="block space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('pages.overview.vectorMemoryCard.value')}</span>
          <Input className="min-h-11 w-full" value={value} disabled={pending} onChange={event => { setValue(event.target.value); saveFact.reset() }} /></label>
        <p className="text-[12px] text-muted">{t('memoryPage.new_memory_hint')}</p>
        {/* No hand-off: retain the fact key and value when a write fails. */}
        <ErrorNotice message={semanticWriteErrorMessage(saveFact.error) || errorText(saveFact.error)} />
      </> : <>
        <label className="block space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('pages.overview.memoryTab.rule')}</span>
          <textarea rows={3} aria-label={t('pages.overview.memoryTab.rule')} className="w-full rounded-lg border border-border bg-bg-elevated p-3 text-[14px] leading-5 text-text focus-ring" value={rule} placeholder={t('pages.overview.memoryTab.rule_e_g_always_use_tabs_not_spaces')} disabled={pending} onChange={event => { setRule(event.target.value); setAnswer(null) }} /></label>
        <div className="space-y-1.5"><span className="block text-[12px] font-medium text-muted">{t('pages.overview.memoryTab.category')}</span>
          <SegmentedControl collapse={false} layoutId="memory-new-lesson-category" value={category} onChange={setCategory} segments={CATEGORIES.map(k => ({ key: k, label: facetLabel('category', k) }))} /></div>
        {answer && answer.tone !== 'error' && <p role="status" className={`rounded-lg px-3 py-2 text-[13px] leading-5 ${answer.tone === 'warning' ? 'bg-warn-subtle text-warn-text' : 'bg-bg-hover text-text'}`}>{answer.text}</p>}
        {/* No hand-off: the rule being typed is an unsaved draft. */}
        <ErrorNotice message={answer?.tone === 'error' ? answer.text : null} />
        {/* No hand-off: the rule being typed is an unsaved draft. */}
        <ErrorNotice title={t('pages.overview.memoryTab.lesson_request_failed')} message={errorText(saveLesson.error)} />
      </>}
    </form>
  </Modal>
}
