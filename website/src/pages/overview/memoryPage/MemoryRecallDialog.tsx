/**
 * Test recall: which memories a bot would get for a task, as
 * `GET /api/memory/recall` selects them. Opens with the search box's words as
 * the task, or an empty task when the search box is empty.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../../api/client'
import { Btn, Input } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import Modal from '../../../components/Modal'
import { memoryQueryRetry } from '../MemoryStoreCard'
import { RecallEvidence, errorText, type RecallResult } from '../memoryRecordParts'

export function MemoryRecallDialog({ store, initialTask, onClose }: { store: string; initialTask: string; onClose: () => void }) {
  const { t } = useTranslation()
  const [task, setTask] = useState(initialTask)
  const [asked, setAsked] = useState(initialTask.trim())
  const recall = useQuery({ queryKey: ['member-memory', store, 'recall', asked], queryFn: () => api.memoryRecall(asked, store) as Promise<RecallResult>, enabled: !!asked, retry: memoryQueryRetry })
  const run = () => { const next = task.trim(); if (next === asked) void recall.refetch(); else setAsked(next) }
  return <Modal open title={t('memoryPage.test_recall')} onClose={onClose} footer={<Btn className="min-h-11" onClick={onClose}>{t('components.modal.close')}</Btn>}>
    <div className="space-y-4">
      <form className="flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={event => { event.preventDefault(); if (task.trim()) run() }}>
        <label className="block min-w-0 flex-1 space-y-1.5 text-[13px]"><span className="block text-[12px] font-medium text-muted">{t('memoryPage.recall_task')}</span>
          <Input className="min-h-11 w-full" value={task} placeholder={t('memoryPage.recall_task_placeholder')} onChange={event => setTask(event.target.value)} /></label>
        <Btn type="submit" primary busy={recall.isFetching} className="min-h-11 justify-center" disabled={!task.trim()}>{t('memoryPage.test_recall')}</Btn>
      </form>
      {/* No hand-off: the typed task is the only draft here and stays in place for a retry. */}
      <ErrorNotice message={errorText(recall.error)} askAgent={false} />
      {asked && recall.data && <RecallEvidence data={recall.data} />}
    </div>
  </Modal>
}
