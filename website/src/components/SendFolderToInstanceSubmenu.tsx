import { Fragment, useSyncExternalStore } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertCircle, Check, ChevronRight, Loader2, Send, Server } from 'lucide-react'
import { api, type InstanceView } from '../api/client'
import {
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import {
  ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent, ContextMenuItem,
} from './ui/context-menu'
import ErrorNotice, { ErrorNoticeMenuItem, type ErrorNoticeMenuItemComponent } from './ErrorNotice'
import { isInstancesDisabledError } from '../utils/instancesDisabled'
import { errMessage } from '../utils/thunkError'

import { i18nT } from '../i18n/t'

/** Outcome of the most recent folder send to one instance. */
export type FolderSendState =
  | { kind: 'idle' }
  | { kind: 'sending'; done: number; total: number }
  | {
    kind: 'done'
    sent: number
    total: number
    transcriptOnly: number
    /** Keys whose copy failed: a click after a partial send retries only these,
     *  so the copies that already landed are not sent a second time. */
    failed: readonly string[]
    firstError?: string
  }

// ── state that outlives the menu ─────────────────────────────────────────
//
// A folder send runs much longer than one session's, so a person is likely to
// close the menu partway. The send's state therefore lives HERE, keyed by
// folder and instance, not in the submenu's own `useState`: a reopened menu
// shows the running send (and refuses a second, overlapping one) or its result.

const sends = new Map<string, FolderSendState>()
const listeners = new Set<() => void>()
// Bumped on every write: the snapshot `useSyncExternalStore` compares. The Map
// itself is mutated in place, so it cannot serve as the snapshot.
let version = 0
const IDLE: FolderSendState = { kind: 'idle' }

// One entry per (folder, crew). JSON keeps the pair unambiguous without a separator literal.
const sendKey = (folderId: string, instanceId: string) => JSON.stringify([folderId, instanceId])

function setSend(key: string, state: FolderSendState) {
  sends.set(key, state)
  version++
  for (const l of listeners) l()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** The send state for one folder, per instance id. Exported for tests. */
export function folderSendStates(folderId: string, instanceIds: readonly string[]): Record<string, FolderSendState> {
  const out: Record<string, FolderSendState> = {}
  for (const id of instanceIds) out[id] = sends.get(sendKey(folderId, id)) ?? IDLE
  return out
}

/** Test seam: forget every recorded send. */
export function resetFolderSends() {
  sends.clear()
  version++
  for (const l of listeners) l()
}

/**
 * The folder menu's "Send a copy to ▸ <instance>": the session menu's entry
 * (SendToInstanceSubmenu), applied to every persistent session filed directly in
 * the folder.
 *
 * Each session is one ordinary `send-session` call with `with_folder: true`, so
 * the peer files them together under a folder of the same name
 * (`Imported` / `from <this instance>` / `<folder>`). Sequential, not parallel:
 * every copy rides the same tunnel and lands against the peer's live-slot cap.
 *
 * Same conventions as the session submenu: nothing renders without configured
 * instances, a disconnected peer is listed disabled rather than hidden, and the
 * menu stays open on select so the row reports progress and the outcome. A
 * failure is shown through the shared `ErrorNotice` with its agent hand-off as a
 * sibling menu item, never in a `title=` a keyboard or touch user cannot reach.
 */
export function FolderSendItems({ folderId, instances, states, onSend, sendable, skipped, Item }: {
  readonly folderId: string
  readonly instances: readonly InstanceView[]
  readonly states: Readonly<Record<string, FolderSendState>>
  readonly onSend: (instanceId: string) => void
  readonly sendable: number
  readonly skipped: number
  readonly Item: ErrorNoticeMenuItemComponent
}) {
  const notConnected = i18nT('components.sendToInstanceSubmenu.not_connected')
  return (
    <>
      {instances.map(inst => {
        const connected = inst.status?.state === 'connected'
        const st = states[inst.id] ?? IDLE
        const partial = st.kind === 'done' && st.failed.length > 0
        const errorId = `folder-send-error-${folderId}-${inst.id}`
        const title = !connected
          ? `${inst.name} — ${notConnected}`
          : partial
            ? `${inst.name} — ${i18nT('components.sendFolderToInstanceSubmenu.retry_failed', { count: st.failed.length })}`
            : st.kind === 'done'
              // A finished row stays clickable (a repeat send is allowed), so the
              // hint says a click sends every session again rather than nothing.
              ? `${inst.name} — ${i18nT('components.sendFolderToInstanceSubmenu.resend_all', { total: sendable })}`
              : inst.name
        return (
          <Fragment key={inst.id}>
            <Item
              title={title}
              disabled={!connected || sendable === 0 || st.kind === 'sending'}
              aria-describedby={partial ? errorId : undefined}
              onSelect={connected && sendable > 0
                ? (event: Event) => {
                  // Keep the menu open so the row can report progress and the outcome.
                  event.preventDefault()
                  onSend(inst.id)
                }
                : undefined}
            >
              <Server
                size={13}
                className={`${connected ? 'text-accent' : 'text-muted'} shrink-0${partial ? ' self-start mt-0.5' : ''}`}
              />
              {/* After a partial send the notice sits UNDER the name: beside it, the
               *  name was squeezed to an ellipsis in a 260px row. The wrapper only
               *  swallows pointer events on the passive alert, so a click on it does
               *  not re-run the send; the row stays the control (it retries the
               *  failed sessions). Same shared surface as ExportSessionItem. */}
              <span className={partial ? 'flex min-w-0 flex-1 flex-col gap-1' : 'truncate'}>
                <span className="truncate">{inst.name}</span>
                {partial && (
                  <span
                    role="presentation"
                    onClick={(e) => e.stopPropagation()}
                    onPointerDown={(e) => e.stopPropagation()}
                  >
                    <ErrorNotice
                      id={errorId}
                      title={i18nT('components.sendFolderToInstanceSubmenu.sent_some', { sent: st.sent, total: st.total })}
                      message={st.firstError ?? i18nT('components.sendToInstanceSubmenu.unknown_error')}
                      variant="inline"
                      messagePlacement="below"
                    />
                  </span>
                )}
                {partial && (
                  // Visible, not only in `title=`: the row itself is the retry
                  // control, and nothing else on screen says so.
                  <span className="text-[10px] text-muted">
                    {i18nT('components.sendFolderToInstanceSubmenu.retry_failed', { count: st.failed.length })}
                  </span>
                )}
              </span>
              {!connected && (
                <span className="ml-auto text-[10px] text-muted shrink-0">{notConnected}</span>
              )}
              {st.kind === 'sending' && (
                <span className="ml-auto flex items-center gap-1 text-[10px] text-muted shrink-0">
                  <Loader2 size={12} className="animate-spin" />
                  {i18nT('components.sendFolderToInstanceSubmenu.progress', { done: st.done, total: st.total })}
                </span>
              )}
              {st.kind === 'done' && !partial && st.transcriptOnly === 0 && (
                <span className="ml-auto flex items-center gap-1 text-[10px] text-ok shrink-0">
                  <Check size={12} />
                  {i18nT('components.sendFolderToInstanceSubmenu.sent_all', { count: st.sent })}
                </span>
              )}
              {st.kind === 'done' && !partial && st.transcriptOnly > 0 && (
                <span
                  className="ml-auto flex items-center gap-1 text-[10px] text-warn shrink-0"
                  title={i18nT('components.sendToInstanceSubmenu.transcript_only_hint')}
                >
                  <AlertCircle size={12} />
                  {i18nT('components.sendFolderToInstanceSubmenu.sent_transcript_only', { sent: st.sent, count: st.transcriptOnly })}
                </span>
              )}
            </Item>
            {partial && (
              <ErrorNoticeMenuItem
                Item={Item}
                message={st.firstError ?? i18nT('components.sendToInstanceSubmenu.unknown_error')}
                describedBy={errorId}
              />
            )}
          </Fragment>
        )
      })}
      {sendable === 0 && (
        <Item disabled>
          <span className="text-[11px] text-muted">
            {i18nT('components.sendFolderToInstanceSubmenu.nothing_to_send')}
          </span>
        </Item>
      )}
      {skipped > 0 && (
        // Visible, not a hover hint: a person must see that part of the folder
        // stays behind before they send it.
        <Item disabled>
          <span className="whitespace-normal text-[11px] text-muted">
            {i18nT('components.sendFolderToInstanceSubmenu.skipped_private', { count: skipped })}
          </span>
        </Item>
      )}
    </>
  )
}

/** Send every key in turn, reporting progress; never throws. Exported for tests. */
export async function sendFolderSessions(
  instanceId: string,
  slotKeys: readonly string[],
  send: (id: string, slot: string, withFolder: boolean) => Promise<{ resume_mode?: string } | undefined>,
  onProgress: (state: FolderSendState) => void,
): Promise<FolderSendState> {
  const total = slotKeys.length
  let sent = 0
  let transcriptOnly = 0
  let firstError: string | undefined
  const failed: string[] = []
  for (let i = 0; i < total; i++) {
    onProgress({ kind: 'sending', done: i, total })
    try {
      const res = await send(instanceId, slotKeys[i], true)
      sent++
      if (res?.resume_mode === 'prefix') transcriptOnly++
    } catch (e) {
      // One refused session does not stop the rest: each is an independent copy.
      failed.push(slotKeys[i])
      firstError ??= e instanceof Error && e.message ? e.message : undefined
    }
  }
  const outcome: FolderSendState = { kind: 'done', sent, total, transcriptOnly, failed, firstError }
  onProgress(outcome)
  return outcome
}

/** Run one folder send, recording its progress in the shared store under
 *  (*folderId*, *instanceId*) so it survives the menu closing. Exported for tests. */
export function startFolderSend(
  folderId: string,
  instanceId: string,
  slotKeys: readonly string[],
  send: (id: string, slot: string, withFolder: boolean) => Promise<{ resume_mode?: string } | undefined>,
): Promise<FolderSendState> {
  return sendFolderSessions(instanceId, slotKeys, send, st => setSend(sendKey(folderId, instanceId), st))
}

/** Which keys a click sends: only the failed ones after a partial send, so the
 *  copies that landed are not duplicated; every key otherwise. Exported for tests. */
export function keysToSend(state: FolderSendState, slotKeys: readonly string[]): readonly string[] {
  return state.kind === 'done' && state.failed.length > 0
    ? slotKeys.filter(k => state.failed.includes(k))
    : slotKeys
}

interface SendFolderToInstanceSubmenuProps {
  /** The folder being sent; keys the send state so it survives the menu closing. */
  readonly folderId: string
  /** Keys of the persistent LOCAL sessions filed directly in the folder. */
  readonly slotKeys: readonly string[]
  /** Sessions in the folder that cannot be sent (incognito / temporary). */
  readonly skipped: number
  /** Which menu family this submenu nests inside — Radix Dropdown vs Context. */
  readonly variant: 'dropdown' | 'context'
}

export default function SendFolderToInstanceSubmenu({ folderId, slotKeys, skipped, variant }: SendFolderToInstanceSubmenuProps) {
  const { data, error } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    staleTime: 30_000,
    retry: false,
  })
  const instances = data?.instances ?? []
  // Re-render on any recorded send; the snapshot is the store's version stamp.
  useSyncExternalStore(subscribe, () => version, () => version)
  const states = folderSendStates(folderId, instances.map(i => i.id))

  const sendMutation = useMutation({
    mutationFn: ({ id, keys }: { id: string; keys: readonly string[] }) =>
      startFolderSend(folderId, id, keys, api.sendSessionToInstance),
  })

  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  const Item = (variant === 'context' ? ContextMenuItem : DropdownMenuItem) as ErrorNoticeMenuItemComponent

  // A failed list read must not look like "no crews configured": the action would
  // vanish with nothing saying why (on /embed/sessions no top bar shows the error
  // either). Only the gateway's own "feature off" denial stays silent.
  const listFailed = error != null && !isInstancesDisabledError(error)
  if (instances.length === 0 && !listFailed) return null
  const listError = listFailed
    ? (errMessage(error) || i18nT('components.instanceTabBar.instances_load_failed'))
    : ''
  const listErrorId = `folder-send-list-error-${folderId}`

  const onSend = (id: string) => {
    const current = states[id] ?? IDLE
    // A send still running for this folder and crew owns the row; a second click
    // must not start an overlapping set of copies.
    if (current.kind === 'sending') return
    sendMutation.mutate({ id, keys: keysToSend(current, slotKeys) })
  }

  return (
    <Sub>
      <SubTrigger>
        <Send size={13} className="shrink-0 text-muted" />
        <span className="flex-1">{i18nT('components.sendToInstanceSubmenu.send_a_copy_to')}</span>
        <ChevronRight size={12} className="text-muted" />
      </SubTrigger>
      <SubContent className="min-w-[260px] max-w-[340px] max-h-[320px] overflow-y-auto">
        {listFailed && (
          <>
            <div className="px-2 py-1.5">
              <ErrorNotice id={listErrorId} message={listError} variant="inline" />
            </div>
            <ErrorNoticeMenuItem Item={Item} message={listError} describedBy={listErrorId} />
          </>
        )}
        {instances.length > 0 && (
          <FolderSendItems
            folderId={folderId}
            instances={instances}
            states={states}
            onSend={onSend}
            sendable={slotKeys.length}
            skipped={skipped}
            Item={Item}
          />
        )}
      </SubContent>
    </Sub>
  )
}
