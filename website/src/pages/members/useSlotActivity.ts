/**
 * useSlotActivity — the ONE reading of "what the crewmate is doing right now"
 * for a member slot, shared by the DM header's identity pill (MembersPage)
 * and the chat's live status line (ChatPane → CrewmateLiveActivity), so the two
 * can never name the same moment differently.
 *
 * Its inputs are the slot's live status seam (`slotStatusDetail`, the SAME
 * record the sessions sidebar and the command palette render through
 * `toolStatusLabel`), the stream state, and whether the status's tool call has
 * RETURNED (its output is in the tool log — the model is reading it, so the
 * crewmate is thinking again). A `nothing_to_do` call reads as thinking on
 * both surfaces (#16429). Each read is memo-safe on its own (a string, a
 * boolean, a stable ref), so a host does not re-render on every WS frame;
 * `resolvePillActivity` folds them at render time.
 */
import { useCallback, useMemo } from 'react'
import { useAppSelector } from '../../store'
import { QUIET_END_SERVER, QUIET_END_TOOL, selectSlotStreamState, selectSlotToolLog } from '../../store/chatSlice'
import { toolStatusLabel, type ToolStatusDetail } from '../../utils/toolStatusLabel'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useLanguage } from '../../i18n/LanguageProvider'
import { resolvePillActivity, type PillActivity } from './pillActivity'

export interface SlotActivityOptions {
  /** The host's own liveness for the slot (the roster's running flag, the
   *  pane's `running || slot.running`), folded with the stream state. */
  running: boolean
  /** Only workers run; the crewmate's own turn is idle. */
  delegatedOnly?: boolean
}

export function useSlotActivity(slotKey: string, opts: SlotActivityOptions): PillActivity {
  const { running, delegatedOnly = false } = opts
  const streamState = useAppSelector((s) => (slotKey ? selectSlotStreamState(s, slotKey) : 'idle'))
  const detail = useAppSelector((s) => (slotKey ? s.chat.slotStatusDetail[slotKey] : undefined))
  // Matched by the call's own id, so parallel calls cannot be confused, and
  // tested with `!== undefined`: an empty output is still a return.
  const toolReturned = useAppSelector((s) => {
    const d = slotKey ? s.chat.slotStatusDetail[slotKey] : undefined
    if (d?.kind !== 'tool' || !d.toolCallId) return false
    const entry = selectSlotToolLog(s, slotKey).findLast((e) => e.type === 'tool' && e.tool_call_id === d.toolCallId)
    return entry !== undefined && entry.output !== undefined
  })
  // The `nothing_to_do` call (#16429) never names itself on ANY surface: the
  // quiet end it asks for must look quiet from its first frame, so the open
  // call reads as thinking. Matched on the tool log's trusted identity
  // (`tool_name` + `mcp_server`), never on its title.
  const quietEnd = useAppSelector((s) => {
    const d = slotKey ? s.chat.slotStatusDetail[slotKey] : undefined
    if (d?.kind !== 'tool' || !d.toolCallId) return false
    const entry = selectSlotToolLog(s, slotKey).findLast((e) => e.type === 'tool' && e.tool_call_id === d.toolCallId)
    return entry?.tool_name === QUIET_END_TOOL && entry?.mcp_server === QUIET_END_SERVER
  })
  const simplified = useSimplifiedToolNames()
  const uiLang = useLanguage().resolved
  const labelOf = useCallback(
    (d: ToolStatusDetail) => toolStatusLabel(d, simplified, uiLang),
    [simplified, uiLang],
  )
  return useMemo(
    () => resolvePillActivity({ streamState, detail, toolReturned: toolReturned || quietEnd, running, delegatedOnly, labelOf }),
    [streamState, detail, toolReturned, quietEnd, running, delegatedOnly, labelOf],
  )
}
