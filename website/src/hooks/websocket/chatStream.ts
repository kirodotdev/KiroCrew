/** Transcript frames: stored rows, steer echoes, streamed content and
 *  reasoning, and tool calls, with the attention, recency, voice and
 *  run-status effects each one carries. */
import { useMemo, type MutableRefObject } from 'react'
import { store, type AppDispatch } from '../../store'
import { markSlotUnread } from '../../store/dashboardSlice'
import { sseChatMessage, appendSlotMessage, setSlotStatusDetail, sseToolActivity, queueEntryQuote } from '../../store/chatSlice'
import { dispatchMcNotification, APPROVAL_KIND, shouldChimeOnPermissionRow } from '../notificationEvent'
import { chatMessageMarksUnread, isMemberThreadSlot, memberThreadRowMarksUnread, noteMemberThreadRow, unreadWatermarkTs } from '../unreadOnAttention'
import { isSlotMutedByCreator } from '../sessionMute'
import { noteUnsavedRowTs } from '../../lib/slotReadRelay'
import { emitThemeSound } from '../themeSound'
import { isReconcileNote } from '../../lib/noteContract'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { deriveToolCallTitle } from '../../utils/toolCallTitle'
import { attendArrival } from './attention'
import { emitToolCall } from './browserEvents'
import type { StreamBuffers } from './streamBuffers'
import type { VoicePlayback } from './voicePlayback'
import type { FrameData } from './frames'

export interface ChatStreamDeps {
  dispatch: AppDispatch
  buffers: StreamBuffers
  voice: VoicePlayback
  reconnectingRef: MutableRefObject<boolean>
}

export interface ChatStream {
  onChatMessage(data: FrameData): void
  onSteerPush(data: FrameData): void
  onChatChunk(data: FrameData): void
  onChatThinking(data: FrameData): void
  onToolCall(data: FrameData): void
}

export function useChatStream({ dispatch, buffers, voice, reconnectingRef }: ChatStreamDeps): ChatStream {
  return useMemo<ChatStream>(() => ({
    onChatMessage(data) {
      buffers.flushChunks()
      dispatch(sseChatMessage(data))
      // Approval-blocked chime for an INTERACTIVE chat. The chat runner
      // parks its turn on this `permission` row and emits no `approval`
      // frame for it (that frame is the coordinator registry's, and chimes
      // on its own), so this row is where the sound is synthesized — the
      // `chat_done` / `question_card` layering: client-side, sound only,
      // no feed row, no toast. The row is delivered once, so one frame is
      // one sound; a row carrying `resolved` — the batch-rejection
      // re-append, or a turn with no budget left to wait, decided before
      // the append — and a reconnect replay stay silent. A Slack post
      // that fails after the row went out retires it via
      // `approval_resolved`; that arrival chime is the accepted residual.
      if (data.role === 'permission' && shouldChimeOnPermissionRow({ meta: data.meta, reconnecting: reconnectingRef.current })) {
        dispatchMcNotification(APPROVAL_KIND)
      }
      // Re-rank the sidebar the instant a session sees a message, instead of waiting
      // for the next full slots push. `last_ts` moves for agent output too (it feeds
      // "last message" reads); the ORDERING key moves only for an inbound prompt —
      // user or inject — so a running turn holds its position instead of shuffling the
      // list on every tool call. Fallback ts is computed here so the touchSlotActivity
      // reducer stays pure (Redux contract).
      if (data.slot && (data.role === 'user' || data.role === 'inject' || data.role === 'assistant' || data.role === 'tool_call' || data.role === 'tool_result')) {
        buffers.bufferSlotActivity(
          data.slot,
          data.ts || new Date().toISOString(),
          data.role === 'user' || data.role === 'inject',
        )
      }
      // The slot's last_ts skips an unsaved row, so reads this window
      // relays later carry its ts explicitly (see noteUnsavedRowTs).
      if (data.slot && unreadWatermarkTs(data.role, data.ts) === undefined && data.ts) noteUnsavedRowTs(data.slot, data.ts)
      // The "only when done or waiting" opt-in leaves routine rows unbadged.
      // A member DM thread badges only on a row the crewmate wrote to the
      // user (`memberThreadRowMarksUnread`): its tool calls are its own work,
      // not a message, so they must not light the Crewmates rail.
      // Recorded whether or not the thread is on screen: the `chat_done`
      // badge for a member thread asks "did this turn say anything", and a
      // row the user watched arrive still counts as said.
      if (data.slot && isMemberThreadSlot(data.slot, store.getState().dashboard.slots)) noteMemberThreadRow(data.slot, data.role)
      attendArrival(data.slot, data.ts, reconnectingRef.current, slot => {
        const slots = store.getState().dashboard.slots
        const marks = isMemberThreadSlot(slot, slots)
          ? memberThreadRowMarksUnread(data.role)
          : chatMessageMarksUnread(data.role)
        // Criterion 7: a session muted by its creator never becomes
        // unread from its own activity. This is the chat_message half of the
        // two automatic markSlotUnread sites; turnCompletion.ts has the other.
        if (marks && !isSlotMutedByCreator(slots, slot)) {
          dispatch(markSlotUnread({ slot, ts: unreadWatermarkTs(data.role, data.ts), localTs: data.ts || undefined }))
        }
      })
      // Theme audio: an agent reply arriving is the `message-received`
      // trigger (no-op unless an L2 theme with that manifest sound is
      // active + unmuted). User/tool messages don't chime.
      if (data.role === 'assistant') emitThemeSound('message-received')
      // A note breadcrumb starts no turn, so no chat_done arrives to undo either
      // effect: cutting speech would strand it and a thinking status would never clear.
      const isPassiveNote = data.role === 'inject' && isReconcileNote(data.cls)
      if (!isPassiveNote && data.slot === store.getState().chat.activeSlot && (data.role === 'user' || data.role === 'inject' || data.role === 'subagent')) {
        voice.resetForNewTurn()
      }
      if (!isPassiveNote && data.slot && (data.role === 'user' || data.role === 'inject' || data.role === 'subagent')) {
        dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'thinking', ts: Date.now() }))
      }
    },
    onSteerPush(data) {
      // Mid-turn steer echo: show the user's steered text inline in the
      // target slot's transcript. Uses appendSlotMessage so the bubble
      // appears whether or not the slot is currently active (background
      // tabs). Persisted server-side — survives page reload.
      // Drain the per-frame chunk buffer FIRST: a pre-steer chunk still
      // pending here means the reducer's finalize-on-steer would find no
      // streaming row to freeze, so that text would later flush BELOW
      // this card and post-steer chunks would append to the same row.
      buffers.flushChunks()
      // `sendId` (present when the initiating client minted one) rides
      // into the meta so the reconcile in appendSlotMessage can match the
      // optimistic bubble by id instead of by content (#6075).
      const steerSid = (data as { sendId?: unknown }).sendId
      // `steerState` says which of written/consumed/requeued this row is in.
      // The server sends `written` here and patches the row to consumed or
      // requeued later via `chat_message_update`, so the badge only claims a
      // successful mid-turn injection once the backend has confirmed one
      // (#7246). Absent on a pre-#7246 server, which the renderer treats as
      // the legacy row shape.
      const steerState = (data as { steerState?: unknown }).steerState
      // The server row's own id. Stored so the later `chat_message_update`,
      // which is keyed on `mid`, resolves this row -- without it that patch
      // matches nothing and the state never moves until a reload.
      const steerMid = (data as { mid?: unknown }).mid
      const steerMeta = (data as { meta?: unknown }).meta
      const steerFiles = steerMeta && typeof steerMeta === 'object' ? (steerMeta as { files?: unknown }).files : undefined
      const steerDirs = steerMeta && typeof steerMeta === 'object' ? (steerMeta as { dirs?: unknown }).dirs : undefined
      // The quote the steer carries (`messageQuote.ts`): without it another
      // tab would draw the blockquote as body text until a reload.
      const steerQuote = queueEntryQuote(steerMeta).quote
      dispatch(appendSlotMessage({
        slot: (data as { slot?: string }).slot || store.getState().chat.activeSlot || '',
        message: { role: 'user', content: (data as { content?: string }).content || '', cls: 'msg msg-u', meta: { steer: true, ...(typeof steerSid === 'string' && steerSid ? { sendId: steerSid } : {}), ...(typeof steerState === 'string' && steerState ? { steerState } : {}), ...(typeof steerMid === 'string' && steerMid ? { mid: steerMid } : {}), ...(Array.isArray(steerFiles) ? { files: steerFiles } : {}), ...(Array.isArray(steerDirs) ? { dirs: steerDirs } : {}), ...(steerQuote ? { quote: steerQuote } : {}) }, ts: (data as { ts?: string }).ts },
      }))
      // Steering is the other way to type into a busy session, so it
      // settles the rank exactly like a queued send. The server appends a
      // real `user` row for it, so the authoritative snapshot already
      // agrees — this only avoids waiting for the next slots push.
      if ((data as { slot?: string }).slot) {
        buffers.bufferSlotActivity(
          (data as { slot: string }).slot,
          (data as { ts?: string }).ts || new Date().toISOString(),
          true,
        )
      }
    },
    onChatChunk(data) {
      // Buffer the chunk and flush once per frame instead of dispatching —
      // and recomputing the O(N) displayItems / index maps — on every token.
      const cs = data.slot
      if (cs) {
        const entry = buffers.bufferChunk(cs, data.seq, data.content ?? '', data.gen)
        if (!entry) return
        if (store.getState().chat.slotStatusDetail[cs]?.kind !== 'streaming') {
          dispatch(setSlotStatusDetail({ slot: cs, kind: 'streaming', ts: Date.now() }))
        }
        buffers.drainOrScheduleChunks(entry)
      }
    },
    onChatThinking(data) {
      // kiro-cli/ACP reasoning (agent_thought_chunk) -> collapsible block.
      // Buffered into the shared chunk buffer and flushed once per frame:
      // reasoning streams run for hundreds of tokens, and a per-token
      // dispatch recomputes the O(N) displayItems on each.
      const thinkSlot = data.slot as string | undefined
      const thinkText = (data as { content?: string }).content || ''
      if (thinkSlot && thinkText) buffers.bufferThinking(thinkSlot, thinkText)
      // Dispatch the status detail only on a genuine kind TRANSITION into
      // 'thinking'. Guarding merely on `!== 'streaming'` would not
      // self-limit — 'thinking' is itself `!== 'streaming'`, so it would
      // re-dispatch on EVERY thought frame with a fresh `ts`. Because
      // setSlotStatusDetail replaces slotStatusDetail[slot] wholesale, that
      // bumps the map identity per frame and re-renders every whole-map
      // subscriber (ChatSidebar, CommandPalette) for the duration of the
      // model's reasoning. The sibling chat_chunk guard writes 'streaming'
      // and so is naturally idempotent; this is the same shape, made
      // explicit.
      const detailKind = data.slot ? store.getState().chat.slotStatusDetail[data.slot]?.kind : undefined
      if (data.slot && detailKind !== 'streaming' && detailKind !== 'thinking') {
        dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'thinking', ts: Date.now() }))
      }
    },
    onToolCall(data) {
      emitToolCall(data)
      dispatch(sseToolActivity({ ...data as { slot: string; tool: string; kind: string; purpose: string; input_preview: string; is_shell?: boolean; tool_name?: string; mcp_server?: string }, auto: (data as Record<string, unknown>).auto === true, tool_call_id: (data as Record<string, unknown>).tool_call_id as string | undefined, is_update: (data as Record<string, unknown>).is_update === true, is_shell: (data as Record<string, unknown>).is_shell === true }))
      if (data.slot) {
        // A refinement (`is_update`) carries only the fields it refines,
        // so merge it into the live status the way sseToolActivity merges
        // the tool-log entry: an update that omits `purpose` must not
        // replace the purpose the initial tool_call supplied with the raw
        // command, and one that omits `tool` must not blank the title.
        // Without this the session-list row of a running session flips
        // from the agent's purpose to the literal command mid-call.
        // Merging is gated on the tool_call_id matching, so when several
        // tools run in parallel a refinement of one cannot inherit a
        // sibling's purpose.
        //
        // `purpose` holds the PURPOSE ALONE and stays empty when the agent
        // supplied none — the fallback to the tool title belongs to
        // toolStatusLabel, which owns the label rule. Storing the title
        // in `purpose` instead would make the two indistinguishable here,
        // and a purpose-less call would then pin the initial stub title
        // ("Terminal") for the whole call instead of advancing to the
        // refined command.
        //
        // `toolName` stays the RAW title, and `derivedTitle` carries the
        // argument-derived one (see utils/toolCallTitle) — a shell call's
        // `List files in src`, an MCP call's `Session send: …`. The label
        // rule that picks between them per the raw-titles preference lives
        // in toolStatusLabel, so this frame handler only stores the parts.
        const tcid = (data as Record<string, unknown>).tool_call_id as string | undefined
        const isUpdate = (data as Record<string, unknown>).is_update === true
        const purpose = sanitizeLlmOutput((data as Record<string, unknown>).purpose as string || '')
        const frame = data as Record<string, unknown>
        const toolName = sanitizeLlmOutput(data.tool || '')
        const derivedInfo = deriveToolCallTitle({
          title: (data.tool as string) || '',
          kind: (frame.kind as string) || '',
          rawInput: frame.input_preview,
          isShell: frame.is_shell === true,
          toolName: (frame.tool_name as string) || '',
          mcpServer: (frame.mcp_server as string) || '',
        })
        // A template's language-neutral action is stored and rendered at read
        // time (toolStatusLabel), so a language switch re-renders the status
        // line; only the backend's own description (R0.0), transport text that
        // is not localized, is stored as a string.
        const derivedAction = derivedInfo.action
        const derivedTitle = derivedInfo.derived && !derivedAction ? sanitizeLlmOutput(derivedInfo.title) : ''
        const prev = store.getState().chat.slotStatusDetail[data.slot]
        const mergeInto = isUpdate && tcid && prev?.kind === 'tool' && prev.toolCallId === tcid
          ? prev
          : undefined
        dispatch(setSlotStatusDetail({
          slot: data.slot,
          kind: 'tool',
          purpose: purpose || mergeInto?.purpose || '',
          toolName: toolName || mergeInto?.toolName || '',
          derivedTitle: derivedTitle || mergeInto?.derivedTitle || '',
          ...(derivedAction
            ? { derivedAction, derivedMore: derivedInfo.more || 0 }
            : mergeInto?.derivedAction
              ? { derivedAction: mergeInto.derivedAction, derivedMore: mergeInto.derivedMore || 0 }
              : {}),
          ...(tcid ? { toolCallId: tcid } : {}),
          ts: Date.now(),
        }))
      }
      // Note: do NOT dispatch sseChatMessage here. The backend persists the
      // tool message via slot.append and broadcasts it as 'chat_message'.
      // Dispatching here would insert a duplicate entry in the message list.
    },
  }), [dispatch, buffers, voice, reconnectingRef])
}
