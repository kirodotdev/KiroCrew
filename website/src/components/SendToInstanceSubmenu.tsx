import { Fragment, useId, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertCircle, Check, ChevronRight, Loader2, Send, Server } from 'lucide-react'
import { api, type InstanceView } from '../api/client'
import { store } from '../store'
import {
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import {
  ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent, ContextMenuItem,
} from './ui/context-menu'

import ErrorNotice, {
  ErrorNoticeMenuItem,
  type ErrorNoticeMenuItemComponent,
} from './ErrorNotice'
import OfflineMenuReason from './OfflineMenuReason'
import { i18nT } from '../i18n/t'
import { offlineProps } from '../utils/offline'
import { useConnected } from '../hooks/useConnected'
import { reportActionFailure } from '../utils/actionFailure'
import { findReport } from '../utils/errorReport'

/** Per-instance outcome of the most recent send attempt in this open menu. */
type SendState =
  | { kind: 'idle' }
  | { kind: 'sending' }
  | { kind: 'sent'; transcriptOnly?: boolean }
  | { kind: 'error'; message: string }

interface SendToInstanceSubmenuProps {
  /** The session to copy. */
  readonly slotKey: string
  /** Which menu family this submenu nests inside — Radix Dropdown vs Context. */
  readonly variant: 'dropdown' | 'context'
}

/**
 * "Send a copy to ▸ <instance>" as a native Radix submenu, mirroring
 * FolderMoveSubmenu so the sidebar's session menus stay consistent.
 *
 * Renders NOTHING when there are no configured instances — including when the
 * Instances feature is disabled, where `listInstances` rejects with a 403 and
 * the query simply has no data. A dead entry that can only ever say "no
 * targets" is worse than no entry, and the feature is opt-in (instances.md §2),
 * so an install with nothing to show is the common case.
 *
 * Only a CONNECTED instance is selectable: the transfer rides that instance's
 * open tunnel, so without one there is nothing to send over. Disconnected peers
 * still render (disabled, with a hint) rather than being hidden, so a peer the
 * user configured never silently vanishes — it tells them to connect it.
 *
 * **The menu deliberately stays open on select** (`preventDefault` on the item's
 * select event) and the outcome renders on the row itself. Every other action in
 * this menu produces a visible local change, so closing on click is its own
 * confirmation; a transfer's only effect happens on ANOTHER machine, so a
 * close-and-say-nothing would leave the user with no way to tell a completed
 * copy from a silently dropped one. A FAILURE renders the peer's own words on the
 * row through `errors-use-error-notice`'s sanctioned in-menu pair — a passive
 * inline `ErrorNotice` plus a sibling `ErrorNoticeMenuItem` carrying the hand-off,
 * never a hand-written danger span — AND reports to the page notice, which is what
 * survives the menu closing.
 *
 * Copy semantics: the local session is untouched and the peer allocates its own
 * key, so a repeat click is harmless and sends a second copy. That is also why
 * this needs no confirm step.
 */
/**
 * The submenu's row list, split out so the visibility / disabled / outcome logic
 * can be unit-tested with a plain `Item` stub — jsdom cannot drive a real Radix
 * submenu open (no PointerEvent), the same reason `FolderPickerItems` is
 * exported from FolderMoveSubmenu.
 *
 * `Item` is the Radix menu-item primitive of the hosting menu family; items must
 * match their parent menu's family.
 */
export function InstanceSendItems({ instances, states, onSend, Item }: {
  readonly instances: readonly InstanceView[]
  readonly states: Readonly<Record<string, SendState>>
  readonly onSend: (instanceId: string) => void
  readonly Item: ErrorNoticeMenuItemComponent
}) {
  const notConnected = i18nT('components.sendToInstanceSubmenu.not_connected')
  const errorIdBase = useId()
  return (
    <>
      {instances.map(inst => {
        const connected = inst.status?.state === 'connected'
        const st = states[inst.id] ?? { kind: 'idle' }
        const errorId = `${errorIdBase}-${inst.id}`
        return (
          <Fragment key={inst.id}>
          <Item
            title={connected ? inst.name : `${inst.name} — ${notConnected}`}
            disabled={!connected || st.kind === 'sending'}
            onSelect={connected
              ? (event: Event) => {
                // Keep the menu open so the row can report the outcome.
                event.preventDefault()
                onSend(inst.id)
              }
              : undefined}
          >
            <Server
              size={13}
              className={connected ? 'text-accent shrink-0' : 'text-muted shrink-0'}
            />
            <span className="truncate">{inst.name}</span>
            {!connected && (
              <span className="ml-auto text-[10px] text-muted shrink-0">{notConnected}</span>
            )}
            {st.kind === 'sending' && (
              <Loader2 size={13} className="ml-auto shrink-0 animate-spin text-muted" />
            )}
            {st.kind === 'sent' && st.transcriptOnly && (
              <span
                className="ml-auto flex items-center gap-1 text-[10px] text-warn shrink-0"
                title={i18nT('components.sendToInstanceSubmenu.transcript_only_hint')}
              >
                <AlertCircle size={12} />
                {i18nT('components.sendToInstanceSubmenu.sent_transcript_only')}
              </span>
            )}
            {st.kind === 'sent' && !st.transcriptOnly && (
              <span className="ml-auto flex items-center gap-1 text-[10px] text-ok shrink-0">
                <Check size={12} />
                {i18nT('components.sendToInstanceSubmenu.sent')}
              </span>
            )}
            {st.kind === 'error' && (
              // role="presentation" and the blocked handlers: a click reaching the
              // row would replace this error with a fresh spinner.
              <span
                className="ml-auto"
                role="presentation"
                onClick={(e) => e.stopPropagation()}
                onPointerDown={(e) => e.stopPropagation()}
              >
                <ErrorNotice id={errorId} message={st.message} variant="inline" />
              </span>
            )}
          </Item>
          {st.kind === 'error' && (
            <ErrorNoticeMenuItem Item={Item} message={st.message} describedBy={errorId} />
          )}
          </Fragment>
        )
      })}
    </>
  )
}

export default function SendToInstanceSubmenu({ slotKey, variant }: SendToInstanceSubmenuProps) {
  // Read here rather than taken as a prop: every caller wants the same gate, and
  // an optional one left a caller ungated.
  const connected = useConnected()
  const [states, setStates] = useState<Record<string, SendState>>({})

  const { data } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    // The list changes only when the user edits it in Settings; the viewport's
    // own 60s poll keeps this shared cache fresh enough for a menu.
    staleTime: 30_000,
    retry: false,
  })

  const sendMutation = useMutation({
    mutationFn: ({ id }: { id: string }) => api.sendSessionToInstance(id, slotKey),
    onMutate: ({ id }) => { setStates(s => ({ ...s, [id]: { kind: 'sending' } })) },
    onSuccess: (res, { id }) => {
      // A peer that materialised Layer B reports 'session_load'; 'prefix' means
      // the copy landed but only as a transcript, so it must NOT read as a plain
      // "Sent" -- that is the silent degradation this feature removes. '' (an
      // older peer that cannot report) stays plain "Sent".
      setStates(s => ({
        ...s,
        [id]: { kind: 'sent', transcriptOnly: res?.resume_mode === 'prefix' },
      }))
    },
    onError: (e, { id }) => {
      const detail = e instanceof Error && e.message
        ? e.message
        : i18nT('components.sendToInstanceSubmenu.unknown_error')
      // The page notice survives the menu closing; the row carries the peer's own
      // words, which findReport can only relay when the journal matched.
      reportActionFailure(
        i18nT('components.sendToInstanceSubmenu.send_failed'),
        store.getState().dashboard.slots.find(s => s.key === slotKey)?.title ?? '',
        findReport(detail),
      )
      setStates(s => ({ ...s, [id]: { kind: 'error', message: detail } }))
    },
  })

  const instances = data?.instances ?? []
  if (instances.length === 0) return null

  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem

  // Neither held closed nor `disabled`: Radix drops a disabled trigger from
  // roving focus, and closing the flyout mid-browse yanks it from the pointer.
  return (
    <Sub>
      <SubTrigger
        {...offlineProps(connected, i18nT('utils.offline.send_to_instances'))}
        className={connected ? undefined : 'opacity-40 text-muted'}
      >
        <Send size={13} className="shrink-0 text-muted" />
        <span className="flex-1">{i18nT('components.sendToInstanceSubmenu.send_a_copy_to')}</span>
        <ChevronRight size={12} className="text-muted" />
      </SubTrigger>
      <SubContent className="min-w-[210px] max-h-[280px] overflow-y-auto">
        {!connected && <OfflineMenuReason testId="send-instance-offline-reason" />}
        <InstanceSendItems
          instances={instances}
          states={states}
          // Refused at the sink as well as the trigger: the touch submenu opened
          // regardless until now, and its sibling FolderMoveSubmenu guards onPick.
          onSend={(id) => { if (!connected) return; sendMutation.mutate({ id }) }}
          Item={Item}
        />
      </SubContent>
    </Sub>
  )
}
