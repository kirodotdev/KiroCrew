import { useId, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { errorCode } from '../api/cards'
import { api } from '../api/client'
import type { ChatSteering, ChatSteeringFile } from '../api/client/chatSlotSettings'
import ErrorNotice from '../components/ErrorNotice'
import { Toggle } from '../components/ui'
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
  switched_off: 'pages.chatSteering.reason_switched_off',
}

/** The switch route's refusal codes, each with its own message. */
const SWITCH_ERROR_KEY: Record<string, string> = {
  store_unreadable: 'pages.chatSteering.switch_error_store_unreadable',
  write_failed: 'pages.chatSteering.switch_error_write_failed',
  session_changed: 'pages.chatSteering.switch_error_session_changed',
  not_switchable: 'pages.chatSteering.switch_error_not_switchable',
}

/** The one line that says whether this file reaches the chat, and why not. */
function reachesLine(file: ChatSteeringFile): string {
  if (file.reaches === 'yes') return i18nT('pages.chatSteering.reaches_yes')
  const key = REASON_KEY[file.reason]
  return key ? i18nT(key) : i18nT('pages.chatSteering.reaches_unknown')
}

/** The pure, data-in list, kept apart from the fetch so tests render it directly.
 *  *onSwitch* receives a row the person switched; without it no switch renders. */
export function ChatSteeringListView({ data, onSwitch }: {
  data: ChatSteering
  onSwitch?: (file: ChatSteeringFile, off: boolean) => void
}) {
  const headingId = useId()
  return (
    <section aria-labelledby={headingId} className="mt-4">
      <h3 id={headingId} className="m-0 text-[13px] font-semibold text-text">
        {i18nT('pages.chatSteering.heading')}
      </h3>
      <p className="m-0 mt-1 text-[12px] text-muted">{i18nT('pages.chatSteering.intro')}</p>
      {onSwitch && data.files.some(f => f.switch === 'on' || f.switch === 'off') && (
        // The switch changes what the next session start (or compaction) sends;
        // text a running conversation already holds stays in it.
        <p className="m-0 mt-1 text-[12px] text-muted">{i18nT('pages.chatSteering.switch_note')}</p>
      )}
      {data.files.length === 0 ? (
        <p className="m-0 mt-2 text-[12px] text-muted">{i18nT('pages.chatSteering.empty')}</p>
      ) : (
        <ul className="m-0 mt-2 p-0 list-none space-y-2">
          {data.files.map(file => (
            <li key={file.source + ':' + file.path} className="rounded-md border border-border p-2 text-[12px]">
              <div className="flex items-start justify-between gap-2">
                <div className="font-medium text-text-strong break-all" title={file.path}>{file.name}</div>
                {onSwitch && (file.switch === 'on' || file.switch === 'off') && (
                  <Toggle
                    checked={file.switch === 'on'}
                    onChange={on => onSwitch(file, !on)}
                    label={i18nT('pages.chatSteering.switch_label', { name: file.name })}
                    tone="muted"
                  />
                )}
              </div>
              <div className="text-muted">
                {i18nT(SOURCE_KEY[file.source] ?? 'pages.chatSteering.source_folder')}
                {' · '}
                {i18nT(APPLIES_KEY[file.applies] ?? 'pages.chatSteering.applies_unknown')}
              </div>
              <div className={file.reaches === 'no' ? 'text-warn-text' : 'text-muted'}>
                {reachesLine(file)}
              </div>
              {file.switch === 'unavailable' && (
                <div className="text-muted">{i18nT('pages.chatSteering.switch_unavailable')}</div>
              )}
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
  const queryKey = ['chat-slot-steering', slot, project, agent, folder, revision ?? 0]
  const qc = useQueryClient()
  const [steeringError, setSteeringError] = useState<string | null>(null)
  // One save at a time: a second click while one is in flight is ignored rather
  // than disabling the switch, which would drop its keyboard focus.
  const saving = useRef(false)
  const { data, error } = useQuery<ChatSteering>({
    queryKey,
    queryFn: () => api.chatSlotSteering(slot),
    enabled: !!slot && typeof api.chatSlotSteering === 'function',
    // Steering files change on disk, not by a pushed frame: read on every mount.
    refetchOnMount: 'always',
  })
  const onSwitch = async (file: ChatSteeringFile, off: boolean) => {
    if (saving.current) return
    saving.current = true
    setSteeringError(null)
    try {
      qc.setQueryData(queryKey, await api.chatSlotSteeringSwitch(slot, file.path, off))
      // A new turn or a project, agent or folder change while this was in flight
      // re-keys the list: read the one now shown, which may predate this save.
      void qc.invalidateQueries({ queryKey: ['chat-slot-steering', slot] })
    } catch (e) {
      const code = errorCode(e)
      const key = code ? SWITCH_ERROR_KEY[code] : undefined
      setSteeringError(key ? i18nT(key) : e instanceof Error ? e.message : String(e))
      // Any failure leaves the stored state unknown here: the list may have moved
      // under the person, or the save may have applied before its reply was lost.
      // Read it again so the rows show what is stored and what a retry would act on.
      void qc.invalidateQueries({ queryKey: ['chat-slot-steering', slot] })
    } finally {
      saving.current = false
    }
  }
  if (error) {
    return <ErrorNotice message={error instanceof Error ? error.message : String(error)} askAgent className="mt-4" />
  }
  if (!data) return null
  return (
    <>
      <ErrorNotice message={steeringError} askAgent className="mt-4" />
      <ChatSteeringListView
        data={data}
        onSwitch={typeof api.chatSlotSteeringSwitch === 'function' ? onSwitch : undefined}
      />
    </>
  )
}
