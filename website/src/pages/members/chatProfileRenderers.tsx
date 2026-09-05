/**
 * Chat-profile renderer entries for a member DM thread.
 *
 * Supplied by ChatPane (displayProfile="chat") as host entries to the SDK's
 * row registry, on top of the store-connected transcript set. Three rows:
 * the escalation card (with a live send handler so its options reply, and the
 * backend index's entry so its state is decided by the authority rather than
 * by a simulation over the hydrated window), the
 * silent-rounds fold row, and an `assistant` override that DELEGATES to the
 * SDK's default reply bubble and appends the process disclosure the
 * projection attached to the row.
 */
import type { MessageRenderContext, MessageRenderer } from '../../app-sdk/messageRenderers'
import { defaultMessageRenderers } from '../../app-sdk/messageRenderers'
import EscalationCard, { type SendOutcome } from './EscalationCard'
import ChatFoldRow from './ChatFoldRow'
import ProcessDisclosure from './ProcessDisclosure'
import { deriveEscalationState } from './escalationState'
import type { EscalationIndexStates } from './useEscalationIndex'

export type { SendOutcome } from './EscalationCard'

export interface ChatProfileRendererOptions {
  memberName: string
  /**
   * `extra` is merged into the sent message's meta (the escalation id). May
   * resolve to what became of the send (ChatPane.doSend does — a `SendOutcome`,
   * or a bare boolean read as `{ ok }`): `ok: false` lets the escalation card
   * unlock so the person can try again; a `queueId` binds the card's latch to
   * that queue entry (see `queuedIds`).
   */
  onSend?: (text: string, extra?: Record<string, unknown>) => Promise<SendOutcome | boolean> | void
  /**
   * The queue ids currently in this pane's queue stack. The escalation card
   * keeps a QUEUED reply latched while its id is in here and lets go only once
   * the entry is gone without a confirming row. Omit when the host has no
   * queue to report (the card then falls back to its timed valve).
   */
  queuedIds?: ReadonlySet<string>
  /**
   * The backend's per-id escalation records (useEscalationIndex). When an id
   * is present its entry decides the card's state; null / unknown id falls
   * back to the window simulation.
   */
  escalationStates?: EscalationIndexStates | null
  /** Asks the index for a fresh read (after a confirmed send, at the latch valve). */
  onEscalationRefresh?: () => void
}

/** The index entry for an escalation row, if the index knows its id. */
function entryFor(m: { meta?: Record<string, unknown> }, states?: EscalationIndexStates | null) {
  if (!states) return undefined
  const id = m.meta?.escalation_id
  return typeof id === 'string' && id ? states[id] : undefined
}

export function createChatProfileRenderers({
  memberName,
  onSend,
  queuedIds,
  escalationStates,
  onEscalationRefresh,
}: ChatProfileRendererOptions): MessageRenderer[] {
  const defaultAssistant = defaultMessageRenderers.find((r) => r.id === 'assistant')
  return [
    {
      id: 'escalation',
      roles: ['escalation'],
      render: (m, ctx) => {
        const authoritative = entryFor(m, escalationStates)
        return ctx.row(
          <EscalationCard
            key={ctx.key}
            message={m}
            memberName={memberName}
            state={deriveEscalationState(m, ctx.messages, ctx.index, Date.now(), authoritative)}
            authoritative={authoritative}
            onSend={onSend}
            queuedIds={queuedIds}
            onRefresh={onEscalationRefresh}
          />,
          true,
        )
      },
    },
    {
      id: 'chat_fold',
      roles: ['chat_fold'],
      render: (m, ctx) => ctx.row(<ChatFoldRow key={ctx.key} message={m} />, true),
    },
    {
      // The SDK default draws the bubble (footer rule, variants, file chips —
      // one implementation, not a copy); this entry only adds the process
      // trail underneath, inside the same bubble layout. When the default
      // returns `null` (an invisible-only row — a quiet round) the tool steps
      // must still be reachable, so the trail is drawn on its own tight row.
      id: 'assistant',
      roles: ['assistant', 'streaming'],
      render: (m, ctx) => {
        if (!defaultAssistant) return null
        const meta = m.meta as Record<string, unknown> | undefined
        const process = Array.isArray(meta?.chat_process) ? (meta!.chat_process as unknown[]) : []
        if (process.length === 0) return defaultAssistant.render(m, ctx)
        const withProcess: MessageRenderContext = {
          ...ctx,
          wrapper: (children, isUser) => ctx.wrapper(
            <div className="flex flex-col gap-0">
              {children}
              <ProcessDisclosure message={m} />
            </div>,
            isUser,
          ),
        }
        const rendered = defaultAssistant.render(m, withProcess)
        if (rendered !== null) return rendered
        return ctx.row(<ProcessDisclosure key={ctx.key} message={m} />, true)
      },
    },
  ]
}
