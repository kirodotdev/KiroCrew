/**
 * Test recall: what a bot gets from this store. Both panes read the
 * session-start build itself (`GET /api/memory/context-preview`, its
 * `session_start.parts`): "At session start" is a session that starts with no
 * message; "With this message" is the parts the same start adds or changes when
 * the typed task is its first message. Each part is the exact text the bot gets,
 * labelled with the family the server tagged it with; nothing here parses it.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../../api/client'
import { Btn, Input } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import Modal from '../../../components/Modal'
import { memoryQueryRetry } from '../MemoryStoreCard'
import { errorText } from '../memoryRecordParts'

const FAMILY_LABELS = { memory: 'memoryRecall.family_memory', lessons: 'memoryRecall.family_lessons', provider: 'memoryRecall.family_provider' } as const
type Part = { family: keyof typeof FAMILY_LABELS; text: string }
type Preview = { session_start?: { parts?: Part[] } }

const partsOf = (preview: Preview | undefined) => preview?.session_start?.parts || []
const partId = (part: Part) => JSON.stringify([part.family, part.text])

export function MemoryRecallDialog({ store, onClose }: { store: string; onClose: () => void }) {
  const { t } = useTranslation()
  const [task, setTask] = useState('')
  const [asked, setAsked] = useState('')
  const start = useQuery({ queryKey: ['member-memory', store, 'context-preview', ''], queryFn: () => api.vectorContextPreview(undefined, store) as Promise<Preview>, retry: memoryQueryRetry })
  const withTask = useQuery({ queryKey: ['member-memory', store, 'context-preview', asked], queryFn: () => api.vectorContextPreview(asked, store) as Promise<Preview>, enabled: !!asked, retry: memoryQueryRetry })
  const run = () => { const next = task.trim(); if (next === asked) { void start.refetch(); if (next) void withTask.refetch() } else setAsked(next) }
  const parts = partsOf(start.data)
  // What the message adds: the parts its start has that a start with no message lacks.
  const before = new Set(parts.map(partId))
  const added = partsOf(withTask.data).filter(part => !before.has(partId(part)))
  return <Modal open title={t('memoryPage.test_recall')} maxWidth={880} onClose={onClose}>
    <div className="space-y-4">
      <form className="flex flex-col gap-2 sm:flex-row sm:items-end" onSubmit={event => { event.preventDefault(); run() }}>
        <label className="block min-w-0 flex-1 space-y-1.5 text-[13px]"><span className="block text-[12px] font-medium text-muted">{t('memoryRecall.task_optional')}</span>
          <Input className="min-h-11 w-full" value={task} placeholder={t('memoryPage.recall_task_placeholder')} onChange={event => setTask(event.target.value)} /></label>
        <Btn type="submit" primary busy={start.isFetching || withTask.isFetching} className="min-h-11 justify-center">{t('memoryPage.test_recall')}</Btn>
      </form>
      {/* No hand-off: the typed task is the only draft here and stays in place for a retry. */}
      <ErrorNotice message={errorText(start.error) || errorText(withTask.error)} askAgent={false} />
      <div className="grid min-w-0 gap-3 md:grid-cols-2">
        <Pane title={t('memoryRecall.at_start')} testId="recall-at-start" loading={start.isPending}>
          {parts.map((part, index) => <PartRow key={index} part={part} />)}
        </Pane>
        <Pane title={t('memoryRecall.with_message')} testId="recall-with-message" loading={!!asked && withTask.isPending} idle={!asked ? t('memoryRecall.type_task') : undefined}>
          {added.map((part, index) => <PartRow key={index} part={part} />)}
        </Pane>
      </div>
    </div>
  </Modal>
}

function PartRow({ part }: { part: Part }) {
  const { t } = useTranslation()
  return <li className="space-y-1.5 px-4 py-3">
    <p className="m-0 text-[12px] font-medium text-muted">{t(FAMILY_LABELS[part.family] ?? FAMILY_LABELS.memory)}</p>
    <pre translate="no" className="m-0 whitespace-pre-wrap break-words font-mono text-[12px] leading-5 text-text [overflow-wrap:anywhere]">{part.text}</pre>
  </li>
}

function Pane({ title, testId, loading, idle, children }: { title: string; testId: string; loading: boolean; idle?: string; children: React.ReactNode[] }) {
  const { t } = useTranslation()
  return <section aria-label={title} data-testid={testId} className="min-w-0 overflow-hidden rounded-2xl border border-border bg-card">
    <header className="border-b border-border px-4 py-3"><h3 className="m-0 text-[14px] font-semibold text-text">{title}</h3></header>
    {idle ? <p className="px-4 py-4 text-[13px] text-muted">{idle}</p>
      : loading ? <p role="status" className="px-4 py-4 text-[13px] text-muted">{t('pages.overview.memoryTab.loading')}</p>
        : children.length ? <ul className="m-0 max-h-96 list-none divide-y divide-border overflow-y-auto p-0">{children}</ul>
          : <p className="px-4 py-4 text-[13px] text-muted">{t('memoryRecall.nothing')}</p>}
  </section>
}
