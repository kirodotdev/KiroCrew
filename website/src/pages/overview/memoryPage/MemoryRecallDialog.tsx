/**
 * Test recall: what a bot gets from this store. "At session start" is the
 * context every session starts with (`GET /api/memory/context-preview`, the
 * prompt path); "With this message" is what the recall for a typed task returns
 * (`GET /api/memory/recall`). An empty task still shows the session start. The
 * exact text of both is one disclosure away.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../../api/client'
import { Btn, Input } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import Modal from '../../../components/Modal'
import { fmtNumber } from '../../../i18n/format'
import { memoryQueryRetry } from '../MemoryStoreCard'
import { MEMORY_RECORD_LABELS, errorText, type RecallResult } from '../memoryRecordParts'
import { facetLabel, sourceGroup } from './memoryListView'

type Preview = { semantic_context?: string; episodic_context?: string }
type Line = { key?: string; text: string; lesson: boolean }

/** The session-start block as rows: a `[heading]`, which may run over several lines, is dropped; a `key: value` line keeps its key. */
export function contextLines(text: string | undefined): Line[] {
  let heading = false
  return (text || '').split('\n').map(line => line.trim()).filter(line => {
    if (!line) return false
    if (heading || line.startsWith('[')) { heading = !line.endsWith(']'); return false }
    return true
  }).map(line => {
    const match = /^([\w.-]+):\s(.*)$/.exec(line)
    return match ? { key: match[1], text: match[2], lesson: match[1].startsWith('lesson.') } : { text: line.replace(/^[-*]\s+/, ''), lesson: false }
  })
}

export function MemoryRecallDialog({ store, initialTask, onClose }: { store: string; initialTask: string; onClose: () => void }) {
  const { t } = useTranslation()
  const [task, setTask] = useState(initialTask)
  const [asked, setAsked] = useState(initialTask.trim())
  const start = useQuery({ queryKey: ['member-memory', store, 'context-preview'], queryFn: () => api.vectorContextPreview(undefined, store) as Promise<Preview>, retry: memoryQueryRetry })
  const recall = useQuery({ queryKey: ['member-memory', store, 'recall', asked], queryFn: () => api.memoryRecall(asked, store) as Promise<RecallResult>, enabled: !!asked, retry: memoryQueryRetry })
  const run = () => { const next = task.trim(); if (next === asked) { void start.refetch(); if (next) void recall.refetch() } else setAsked(next) }
  const lines = contextLines(start.data?.semantic_context)
  const lessons = lines.filter(line => line.lesson).length
  const found = [
    ...(recall.data?.retrieval?.facts || []).map(item => ({ kind: 'fact' as const, item })),
    ...(recall.data?.retrieval?.episodes || []).map(item => ({ kind: 'episode' as const, item })),
  ]
  const exact = [start.data?.semantic_context, recall.data?.lessons_context, recall.data?.semantic_context, recall.data?.episodic_context]
  return <Modal open title={t('memoryPage.test_recall')} maxWidth={880} onClose={onClose}>
    <div className="space-y-4">
      <form className="flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={event => { event.preventDefault(); run() }}>
        <label className="block min-w-0 flex-1 space-y-1.5 text-[13px]"><span className="block text-[12px] font-medium text-muted">{t('memoryRecall.task_optional')}</span>
          <Input className="min-h-11 w-full" value={task} placeholder={t('memoryPage.recall_task_placeholder')} onChange={event => setTask(event.target.value)} /></label>
        <Btn type="submit" primary busy={start.isFetching || recall.isFetching} className="min-h-11 justify-center">{t('memoryPage.test_recall')}</Btn>
      </form>
      {/* No hand-off: the typed task is the only draft here and stays in place for a retry. */}
      <ErrorNotice message={errorText(start.error) || errorText(recall.error)} askAgent={false} />
      <div className="grid min-w-0 gap-3 md:grid-cols-2">
        <Pane title={t('memoryRecall.at_start')} count={start.data ? t('memoryRecall.start_count', { facts: fmtNumber(lines.length - lessons), lessons: fmtNumber(lessons) }) : undefined} testId="recall-at-start" loading={start.isPending}>
          {lines.map((line, index) => <li key={index} className="space-y-1 px-4 py-3">
            <p className="break-words text-[14px] leading-5 text-text [overflow-wrap:anywhere]">{line.text}</p>
            <p className="flex min-w-0 flex-wrap items-center gap-2 text-[12px] text-muted"><span>{t(MEMORY_RECORD_LABELS[line.lesson ? 'directive' : 'fact'])}</span>{line.key && <code className="truncate">{line.key}</code>}</p>
          </li>)}
        </Pane>
        <Pane title={t('memoryRecall.with_message')} count={asked && recall.data ? t('memoryRecall.found_count', { count: fmtNumber(found.length) }) : undefined} testId="recall-with-message" loading={!!asked && recall.isPending} idle={!asked ? t('memoryRecall.type_task') : undefined}>
          {found.map(({ kind, item }, index) => <li key={`${kind}:${item.id || item.key || index}`} className="space-y-1 px-4 py-3">
            <p className="break-words text-[14px] leading-5 text-text [overflow-wrap:anywhere]">{item.snippet || item.text}</p>
            <p className="flex min-w-0 flex-wrap items-center gap-2 text-[12px] text-muted"><span>{t(MEMORY_RECORD_LABELS[kind])}</span>{item.key && <code className="truncate">{item.key}</code>}{item.source && <span>{facetLabel('source', sourceGroup(item.source))}</span>}</p>
          </li>)}
        </Pane>
      </div>
      <details className="text-[13px]" data-testid="recall-exact-text">
        <summary className="min-h-11 cursor-pointer py-3 text-muted">{t('memoryRecall.exact_text')}</summary>
        <div className="grid min-w-0 gap-3 md:grid-cols-2">
          <pre translate="no" className="m-0 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-bg-elevated p-3 font-mono text-[12px] leading-5">{exact[0] || t('memoryRecall.nothing')}</pre>
          <pre translate="no" className="m-0 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-bg-elevated p-3 font-mono text-[12px] leading-5">{exact.slice(1).filter(Boolean).join('\n\n') || t('memoryRecall.nothing')}</pre>
        </div>
      </details>
    </div>
  </Modal>
}

function Pane({ title, count, testId, loading, idle, children }: { title: string; count?: string; testId: string; loading: boolean; idle?: string; children: React.ReactNode[] }) {
  const { t } = useTranslation()
  return <section aria-label={title} data-testid={testId} className="min-w-0 overflow-hidden rounded-2xl border border-border bg-card">
    <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-3"><h3 className="m-0 text-[14px] font-semibold text-text">{title}</h3>{count && <span className="text-[12px] tabular-nums text-muted">{count}</span>}</header>
    {idle ? <p className="px-4 py-4 text-[13px] text-muted">{idle}</p>
      : loading ? <p role="status" className="px-4 py-4 text-[13px] text-muted">{t('pages.overview.memoryTab.loading')}</p>
        : children.length ? <ul className="m-0 max-h-80 list-none divide-y divide-border overflow-y-auto p-0">{children}</ul>
          : <p className="px-4 py-4 text-[13px] text-muted">{t('memoryRecall.nothing')}</p>}
  </section>
}
