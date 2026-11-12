import { useId } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { ChatSteering, ChatSteeringFile } from '../api/client/chatSlotSettings'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { useAppSelector } from '../store'

const SOURCE_KEY: Record<ChatSteeringFile['source'], string> = {
  global: 'pages.chatSteering.source_global',
  project: 'pages.chatSteering.source_project',
  folder: 'pages.chatSteering.source_folder',
  agent: 'pages.chatSteering.source_agent',
}

const APPLIES_KEY: Record<ChatSteeringFile['applies'], string> = {
  always: 'pages.chatSteering.applies_always',
  manual: 'pages.chatSteering.applies_manual',
  auto: 'pages.chatSteering.applies_auto',
  fileMatch: 'pages.chatSteering.applies_fileMatch',
  unknown: 'pages.chatSteering.applies_unknown',
}

const REASON_KEY: Record<string, string> = {
  harness_has_no_path: 'pages.chatSteering.reason_harness_has_no_path',
  agent_resources: 'pages.chatSteering.reason_agent_resources',
  // The plan could not tell which agent spec kiro-cli serves: no firm claim.
  agent_spec_unread: 'pages.chatSteering.reaches_unknown',
  inheritance_off: 'pages.chatSteering.reason_inheritance_off',
  link_not_admitted: 'pages.chatSteering.reason_link_not_admitted',
  custom_agent: 'pages.chatSteering.reason_custom_agent',
  inclusion_withheld: 'pages.chatSteering.reason_inclusion_withheld',
  not_loaded: 'pages.chatSteering.reason_not_loaded',
  folder_cap: 'pages.chatSteering.reason_folder_cap',
  harness_native: 'pages.chatSteering.reason_harness_native',
  member_briefing: 'pages.chatSteering.reason_member_briefing',
}

/** The one line that says whether this file reaches the chat, and why not. */
function reachesLine(file: ChatSteeringFile): string {
  if (file.reaches === 'yes') return i18nT('pages.chatSteering.reaches_yes')
  const key = REASON_KEY[file.reason]
  return key ? i18nT(key) : i18nT('pages.chatSteering.reaches_unknown')
}

/** The pure, data-in list, kept apart from the fetch so tests render it directly. */
export function ChatSteeringListView({ data }: { data: ChatSteering }) {
  const headingId = useId()
  return (
    <section aria-labelledby={headingId} className="mt-4">
      <h3 id={headingId} className="m-0 text-[13px] font-semibold text-text">
        {i18nT('pages.chatSteering.heading')}
      </h3>
      <p className="m-0 mt-1 text-[12px] text-muted">{i18nT('pages.chatSteering.intro')}</p>
      {data.files.length === 0 ? (
        <p className="m-0 mt-2 text-[12px] text-muted">{i18nT('pages.chatSteering.empty')}</p>
      ) : (
        <ul className="m-0 mt-2 p-0 list-none space-y-2">
          {data.files.map(file => (
            <li key={file.source + ':' + file.path} className="rounded-md border border-border p-2 text-[12px]">
              <div className="font-medium text-text-strong break-all" title={file.path}>{file.name}</div>
              <div className="text-muted">
                {i18nT(SOURCE_KEY[file.source] ?? 'pages.chatSteering.source_folder')}
                {' · '}
                {i18nT(APPLIES_KEY[file.applies] ?? 'pages.chatSteering.applies_unknown')}
              </div>
              <div className={file.reaches === 'no' ? 'text-warn-text' : 'text-muted'}>
                {reachesLine(file)}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** "Steering in this chat", under the chat's Context breakdown tab.
 *
 *  Keyed on what decides the answer: the slot's project, agent and folder, and the
 *  context trace's revision, so a pick or a new turn re-reads it. */
export function ChatSteeringList({ slot, revision }: { slot: string; revision?: number }) {
  const project = useAppSelector(s => s.dashboard.slots.find(r => r.key === slot)?.project ?? '')
  const agent = useAppSelector(s => s.dashboard.slots.find(r => r.key === slot)?.agent ?? '')
  const folder = useAppSelector(s => s.dashboard.slots.find(r => r.key === slot)?.folder_id ?? '')
  const { data, error } = useQuery<ChatSteering>({
    queryKey: ['chat-slot-steering', slot, project, agent, folder, revision ?? 0],
    queryFn: () => api.chatSlotSteering(slot),
    enabled: !!slot && typeof api.chatSlotSteering === 'function',
    // Steering files change on disk, not by a pushed frame: read on every mount.
    refetchOnMount: 'always',
  })
  if (error) {
    return <ErrorNotice message={error instanceof Error ? error.message : String(error)} askAgent className="mt-4" />
  }
  return data ? <ChatSteeringListView data={data} /> : null
}
