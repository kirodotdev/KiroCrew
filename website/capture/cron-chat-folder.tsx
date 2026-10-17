/**
 * Isolated capture entry for the Schedule form's chat-folder picker (#1620).
 *
 * WHY ISOLATED: the control lives in JobForm, which only renders inside the
 * Schedule page behind a live gateway (a cron store, an agent roster, a model
 * list). This mounts the REAL JobForm against the real stylesheet, theme tokens
 * and live i18n catalog, with `/api/chat/folders` answered by a fixture — so a
 * frame documents the shipped control, not a mock-up of it.
 *
 * Scene comes from the query string:
 *   ?scene=closed       — the picker in its default state on a new job
 *   ?scene=open         — the folder list open, showing nested folders by path
 *   ?scene=filed        — an existing job that is already filed in a folder
 *   ?scene=hidden       — a filed job with "Hide in chat" on: picker disabled, says why
 *   ?scene=stateless    — a job on a fresh session per run: picker disabled, says why
 *   ?scene=unavailable  — the folder list failed to load: notice plus a retry
 *   ?scene=empty        — no folders exist yet: says where one is created
 *   ?scene=deleted      — the job's saved folder was deleted since the save
 *   &theme=dark|light
 *
 * The open scene is driven by the harness clicking the REAL trigger, not by
 * forcing component state. Every other scene is reached the way a reader
 * reaches it: through the job record and the folder endpoint's answer.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import JobForm from '../src/components/JobForm'
import { initI18n } from '../src/i18n/all'
import type { ChatFolder, CronJob } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'closed'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-capture-scene', scene)

const FOLDERS: ChatFolder[] = [
  { id: 'work', name: 'Work', order: 0 },
  { id: 'standups', name: 'Standups', order: 0, parent_id: 'work' },
  { id: 'incidents', name: 'Incidents', order: 1, parent_id: 'work' },
  { id: 'personal', name: 'Personal', order: 1 },
]

const FILED_JOB = {
  id: 'cap1',
  name: 'Standup brief',
  message: 'Summarize what the team shipped since yesterday.',
  schedule: '',
  enabled: true,
  every_secs: 86400,
  chat_folder_id: 'standups',
} as CronJob

/** The job each scene opens the form on. Unset scenes are a NEW job. */
const JOB_FOR_SCENE: Record<string, CronJob | undefined> = {
  filed: FILED_JOB,
  hidden: { ...FILED_JOB, hide_in_chat: true } as CronJob,
  stateless: { ...FILED_JOB, chat_folder_id: '', persistent_session: false } as CronJob,
  // Names a folder the endpoint no longer returns, which is exactly what a
  // deletion leaves behind on the job record.
  deleted: { ...FILED_JOB, chat_folder_id: 'archived-2025' } as CronJob,
}

/** What `/api/chat/folders` answers per scene: the tree, an empty tree, or a
 *  failure. Answered here so the form's own error and empty branches run. */
function answerFolders(json: (body: unknown) => Response): Response {
  if (scene === 'unavailable') return new Response('gateway unavailable', { status: 503 })
  if (scene === 'empty') return json([])
  return json(FOLDERS)
}

// The form's own fetches, answered locally: only the folder list matters to the
// control under capture, and the other two keep the form from rendering an
// error state that would crowd the frame.
const originalFetch = window.fetch.bind(window)
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  if (url.includes('/api/chat/folders')) return answerFolders(json)
  if (url.includes('/api/models')) return json({ models: [] })
  if (url.includes('/api/kirocrew-agents')) return json({ agents: [], default_agent: '' })
  return originalFetch(input as RequestInfo, init)
}) as typeof window.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [saved, setSaved] = useState(0)
  return (
    <div className="min-h-screen bg-bg text-text p-6" data-capture-root>
      <div className="max-w-[560px] rounded-xl border border-border bg-bg-elevated p-4">
        <JobForm
          key={saved}
          layout="vertical"
          agents={[]}
          defaultAgent=""
          job={JOB_FOR_SCENE[scene]}
          onSaved={() => setSaved(n => n + 1)}
        />
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter>
      <Harness />
    </MemoryRouter>
  </QueryClientProvider>,
)
