import { Fragment, useId, useState, type ComponentType } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertCircle, Check, ChevronRight, Loader2, Send, Server } from 'lucide-react'
import { api, type InstanceView } from '../api/client'
import {
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuItem,
  DropdownMenuSeparator,
} from './ui/dropdown-menu'
import {
  ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent, ContextMenuItem,
  ContextMenuSeparator,
} from './ui/context-menu'

import ErrorNotice, {
  ErrorNoticeMenuItem,
  type ErrorNoticeMenuItemComponent,
} from './ErrorNotice'
import { OfflineReasonRow } from './OfflineMenuReason'
import { i18nT } from '../i18n/t'
import { resolveDefaultDestination, withDefaultFirst } from '../utils/defaultDestinationCrew'
import { offlineProps } from '../utils/offline'
import { useConnected } from '../hooks/useConnected'

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
  /** The session's folder's `default_destination_crew`, which overrides the
   *  global default destination crew for this session. */
  readonly folderDefault?: string
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
 * never a hand-written danger span.
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
export function InstanceSendItems({ instances, states, onSend, Item, Separator, defaultId = '' }: {
  readonly instances: readonly InstanceView[]
  /** The default destination crew's id ('' = none). Its row is marked; the
   *  caller orders it first. Marking never sends: the click still does. */
  readonly defaultId?: string
  readonly states: Readonly<Record<string, SendState>>
  readonly onSend: (instanceId: string) => void
  readonly Item: ErrorNoticeMenuItemComponent
  /** The hosting family's separator, used to detach the hand-off row from the
   *  peer above it so it does not read as another send destination. */
  readonly Separator?: ComponentType<{ className?: string }>
}) {
  // The gateway link, not the peer's `status.state` below — read here, as
  // FolderPickerItems does, so no caller can omit the gate.
  const gatewayOnline = useConnected()
  const notConnected = i18nT('components.sendToInstanceSubmenu.not_connected')
  const defaultLabel = i18nT('components.sendToInstanceSubmenu.default_badge')
  const sendVerb = i18nT('utils.offline.send_to_instances')
  const errorIdBase = useId()
  // Radix dismisses on select unless defaulted, which took the reason row with it.
  const refuse = (event: Event) => { event.preventDefault() }
  return (
    <>
      {instances.map(inst => {
        const connected = inst.status?.state === 'connected'
        const st = states[inst.id] ?? { kind: 'idle' }
        const isDefault = inst.id === defaultId
        const name = isDefault ? `${inst.name} (${defaultLabel})` : inst.name
        const errorId = `${errorIdBase}-${inst.id}`
        return (
          <Fragment key={inst.id}>
          <Item
            title={connected ? name : `${name} — ${notConnected}`}
            offline={!gatewayOnline}
            {...(gatewayOnline ? {} : offlineProps(false, sendVerb, name))}
            disabled={!connected || st.kind === 'sending'}
            onSelect={connected && gatewayOnline
              ? (event: Event) => {
                // Keep the menu open so the row can report the outcome.
                event.preventDefault()
                onSend(inst.id)
              }
              : refuse}
          >
            <Server
              size={13}
              className={connected ? 'text-accent shrink-0' : 'text-muted shrink-0'}
            />
            <span className="truncate">{inst.name}</span>
            {isDefault && (
              <span
                data-testid="send-to-instance-default"
                className="shrink-0 rounded border border-border px-1 text-[10px] text-muted"
              >
                {defaultLabel}
              </span>
            )}
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
              // Blocked handlers: a click reaching the row would replace this with a spinner.
              <span
                className="ml-auto"
                role="presentation"
                onClick={(e) => e.stopPropagation()}
                onPointerDown={(e) => e.stopPropagation()}
              >
                {/* Capped, but WRAPPING: an uncapped peer message widens the flyout,
                    and truncating put the reason itself out of reach of touch and keyboard.
                    The cap is wide enough for a typical peer reason to settle in about two
                    lines; at half this it stacked four lines of red beside the peer name. */}
                <ErrorNotice id={errorId} message={st.message} variant="inline" className="max-w-[380px]" messageClassName="whitespace-normal" messageTooltip={st.message} />
              </span>
            )}
          </Item>
          {st.kind === 'error' && (
            <>
            {/* Under the hand-off row's own gate, so a gateway that drops while
                the menu is open cannot leave a divider behind. */}
            {gatewayOnline && st.message && Separator && <Separator />}
            <ErrorNoticeMenuItem Item={Item} message={st.message} describedBy={errorId} />
            </>
          )}
          </Fragment>
        )
      })}
    </>
  )
}

export default function SendToInstanceSubmenu({ slotKey, variant, folderDefault }: SendToInstanceSubmenuProps) {
  // Read here, not taken as a prop: an optional one left a caller ungated.
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
      setStates(s => ({
        ...s,
        [id]: {
          kind: 'error',
          // The API client throws ApiError (an Error subclass) carrying the
          // peer's own message, so this surfaces "peer refused the transfer"
          // rather than a generic failure.
          message: e instanceof Error && e.message
            ? e.message
            : i18nT('components.sendToInstanceSubmenu.unknown_error'),
        },
      }))
    },
  })

  const instances = data?.instances ?? []
  if (instances.length === 0) return null
  // The default crew goes first and is marked; an offline default stays
  // disabled like any other offline crew, and the others stay usable.
  const defaultId = resolveDefaultDestination(
    instances.map(i => i.id), data?.default_destination, folderDefault,
  )

  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const Separator = variant === 'context' ? ContextMenuSeparator : DropdownMenuSeparator

  // Neither held closed nor `disabled`: Radix drops a disabled trigger from roving focus.
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
        <InstanceSendItems
          instances={withDefaultFirst(instances, defaultId)}
          defaultId={defaultId}
          states={states}
          // The trigger only announces, so an opened flyout must still refuse.
          onSend={(id) => { if (!connected) return; sendMutation.mutate({ id }) }}
          Item={Item}
          Separator={Separator}
        />
        {/* LAST, as the top-level menu does: a reconnect unmounts this row, and above the peers it would shift them under a mid-aim pointer. */}
        {!connected && <OfflineReasonRow testId="send-instance-offline-reason" className="px-2 py-1" message={i18nT('utils.offline.gateway_offline_reconnect', { action: i18nT('utils.offline.send_to_instances') })} />}
      </SubContent>
    </Sub>
  )
}
