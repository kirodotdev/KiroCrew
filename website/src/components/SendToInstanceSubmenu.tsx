import type React from 'react'
import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertCircle, Check, ChevronRight, Loader2, Send, Server } from 'lucide-react'
import { api, type InstanceView } from '../api/client'
import {
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import {
  ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent, ContextMenuItem,
} from './ui/context-menu'

import { i18nT } from '../i18n/t'

/** Per-instance outcome of the most recent send attempt in this open menu. */
type SendState =
  | { kind: 'idle' }
  | { kind: 'sending' }
  | { kind: 'sent' }
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
 * copy from a silently dropped one. There is no toast primitive in this app —
 * the sibling convention is an inline note next to the control
 * (InstancesPanel's `actionErr` / `connectedNote`), and the row IS the control
 * here.
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
  readonly Item: React.ComponentType<{
    title?: string
    disabled?: boolean
    onSelect?: (event: Event) => void
    children?: React.ReactNode
  }>
}) {
  const notConnected = i18nT('components.sendToInstanceSubmenu.not_connected')
  return (
    <>
      {instances.map(inst => {
        const connected = inst.status?.state === 'connected'
        const st = states[inst.id] ?? { kind: 'idle' }
        return (
          <Item
            key={inst.id}
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
            {st.kind === 'sent' && (
              <span className="ml-auto flex items-center gap-1 text-[10px] text-ok shrink-0">
                <Check size={12} />
                {i18nT('components.sendToInstanceSubmenu.sent')}
              </span>
            )}
            {st.kind === 'error' && (
              <span
                className="ml-auto flex items-center gap-1 text-[10px] text-danger shrink-0"
                title={st.message}
              >
                <AlertCircle size={12} />
                {i18nT('components.sendToInstanceSubmenu.failed')}
              </span>
            )}
          </Item>
        )
      })}
    </>
  )
}

export default function SendToInstanceSubmenu({ slotKey, variant }: SendToInstanceSubmenuProps) {
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
    onSuccess: (_res, { id }) => { setStates(s => ({ ...s, [id]: { kind: 'sent' } })) },
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

  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem

  return (
    <Sub>
      <SubTrigger>
        <Send size={13} className="shrink-0 text-muted" />
        <span className="flex-1">{i18nT('components.sendToInstanceSubmenu.send_a_copy_to')}</span>
        <ChevronRight size={12} className="text-muted" />
      </SubTrigger>
      <SubContent className="min-w-[210px] max-h-[280px] overflow-y-auto">
        <InstanceSendItems
          instances={instances}
          states={states}
          onSend={(id) => sendMutation.mutate({ id })}
          Item={Item}
        />
      </SubContent>
    </Sub>
  )
}
