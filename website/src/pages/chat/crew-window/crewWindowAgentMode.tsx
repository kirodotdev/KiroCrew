/** The crew window's agent picker and approval mode: the local composer's own
 *  agent chip, agent list and approval-mode picker, fed the PEER's state and
 *  writing the PEER's slot.
 *
 *  The agent list is the peer's roster (`/api/instances/{id}/capabilities`,
 *  already read by the window), and the mode is the peer slot's own trust
 *  flags plus the peer's YOLO flag and denied modes (`/approval-state`,
 *  polled while the window is open). Both writes are the peer's own routes through the hub proxy, so the
 *  peer runs its own owner and policy checks and its refusal comes back here.
 *  YOLO is not offered: it is the peer's app-wide switch, not this session's. */
import { useCallback, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { apiErrorCode } from '../../../api/apiError'
import AgentDropdownList, { type AgentItem } from '../../../components/AgentDropdownList'
import type { ApprovalModeRemote } from '../../../components/ApprovalModePicker'
import { Input } from '../../../components/ui'
import { useAnchoredTriggerRect } from '../../../hooks/useAnchoredTriggerRect'
import { useFilteredDropdown } from '../../../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../../../hooks/useListboxKeyboard'
import { i18nT } from '../../../i18n/t'
import { agentOrDefaultLabel } from '../../../utils/agentLabel'
import { slotApprovalMode } from '../../../utils/slotApprovalMode'
import { errMessage } from '../../../utils/thunkError'
import type { RemoteCrewCapabilities } from '../../../types'

/** The agent and approval fields of the peer's slot row. */
export interface PeerAgentModeFields { agent?: string; trust?: boolean; trust_scope?: string; trust_reads?: boolean }

/** How often the peer's YOLO and denied modes are re-read while the window is
 *  open, so the picker follows YOLO turning on or expiring. */
const APPROVAL_STATE_REFRESH_MS = 30_000

export function useCrewWindowAgentMode({ instanceId, slotKey, slotPath, name, slot, caps, enabled, onWritten }: {
  instanceId: string
  /** The peer's own key for this session. */
  slotKey: string
  /** `api/chat/slots/<key>` on the peer. */
  slotPath: string
  /** The crew's name, for the YOLO note. */
  name: string
  slot: PeerAgentModeFields | null | undefined
  caps: RemoteCrewCapabilities | undefined
  /** The peer answers this build's routes (the window's version gate). */
  enabled: boolean
  /** A write landed (or failed): re-read the peer's slot. */
  onWritten: () => void
}) {
  const approvalQ = useQuery({
    queryKey: ['crew-window', instanceId, 'approval-state'],
    queryFn: () => api.instancesApprovalState(instanceId),
    enabled,
    refetchInterval: APPROVAL_STATE_REFRESH_MS,
  })
  const peer = approvalQ.data
  const mutation = useMutation({
    mutationFn: ({ path, body }: { path: string; body: object }) => api.crewPeerPost(instanceId, path, body),
    // One at a time per crew: a second pick waits for the first, so the peer
    // applies them in the order they were made.
    scope: { id: 'crew-window-write:' + instanceId },
    onSettled: onWritten,
  })
  const { mutateAsync } = mutation
  // A policy refusal of a mode is re-thrown for the picker, which shows it in
  // its own menu as the local picker does; every other failure is the
  // window's notice (`errorText` below).
  const write = useCallback((path: string, body: object) => mutateAsync({ path, body }).catch((e: unknown) => {
    if (apiErrorCode(e) === 'mode_disabled_by_policy') throw e
  }), [mutateAsync])

  const agents = useMemo<AgentItem[]>(
    () => (caps?.agents ?? []).map(a => ({ name: a.name, source: a.scope, description: a.description })),
    [caps?.agents],
  )
  const defaultAgent = caps?.default_agent ?? ''
  const activeAgent = slot?.agent || defaultAgent
  const dd = useFilteredDropdown(agents)
  const { rect, anchorTo } = useAnchoredTriggerRect(dd.open)
  const pickAgent = useCallback((agent: string) => {
    dd.setOpen(false)
    void write(slotPath + '/agent', { agent })
  }, [dd, write, slotPath])
  const { onListKeyDown } = useListboxKeyboard({
    open: dd.open,
    dropdownRef: dd.dropdownRef,
    inputRef: dd.inputRef,
    hasFilterInput: true,
    filteredCount: dd.filtered.length,
    onEnterSingleMatch: () => pickAgent(dd.filtered[0].name),
    closeToTrigger: () => dd.setOpen(false),
  })

  const chipProps = {
    agentName: activeAgent,
    agentLabel: agentOrDefaultLabel(slot?.agent, defaultAgent),
    agentIsInheritedDefault: !slot?.agent && !!defaultAgent,
    agentSource: agents.find(a => a.name === activeAgent)?.source,
    // No roster, no list: an empty menu would read as "this crew has no agents".
    onAgentClick: agents.length ? (r: DOMRect, trigger?: HTMLElement) => { anchorTo(r, trigger); dd.setOpen(!dd.open) } : undefined,
    // No picker until both halves are known (the slot's flags, the peer's
    // YOLO): a guessed "Normal" on a peer that auto-approves everything lies.
    approvalMode: slot && typeof peer?.yolo === 'boolean' ? slotApprovalMode(peer.yolo ? 'yolo' : undefined, slot) : undefined,
    approvalModeRemote: {
      write: mode => write('api/chat/mode', { mode, slot: slotKey }),
      trustScoped: !slot?.trust && !!slot?.trust_scope,
      disabledModes: peer?.disabled_approval_modes ?? [],
      note: i18nT('pages.chat.crewWindow.yolo_elsewhere', { name }),
    } satisfies ApprovalModeRemote,
  }
  const portal = dd.open && rect ? createPortal(
    // Same box as the local chat's agent picker (ChatPage).
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div ref={dd.dropdownRef} role="dialog" aria-label={i18nT('pages.chatPage.agent_selector')} tabIndex={-1} onKeyDown={onListKeyDown} className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[260px] max-w-[340px] flex flex-col p-1 gap-0.5 animate-slide-up" style={{ bottom: window.innerHeight - rect.top + 4, left: Math.max(8, Math.min(rect.left, window.innerWidth - 348)) }}>
      <div className="px-1.5 pt-1.5 pb-1">
        <Input ref={dd.inputRef} type="text" aria-label={i18nT('pages.chatPage.filter_agents')} placeholder={i18nT('pages.chatPage.type_to_filter')} value={dd.filter} onChange={e => dd.setFilter(e.target.value)} className="w-full px-2 py-1 text-[13px]" />
      </div>
      <div role="listbox" aria-label={i18nT('pages.chatPage.agent_list')} className="overflow-y-auto max-h-[280px]">
        <AgentDropdownList agents={dd.filtered} activeAgent={activeAgent} defaultAgent={defaultAgent} onSelect={pickAgent} filter={dd.filter} />
      </div>
    </div>,
    document.body,
  ) : null
  const error = mutation.error
  const errorText = error == null || apiErrorCode(error) === 'mode_disabled_by_policy' ? '' : errMessage(error)
  // A failed read hides its control, so say so rather than let it look absent.
  const readFailed = {
    agents: !!caps?.unavailable?.agents,
    mode: approvalQ.isError || (!!peer && peer.yolo == null),
  }
  return { chipProps, portal, error: errorText, clearError: mutation.reset, readFailed }
}
