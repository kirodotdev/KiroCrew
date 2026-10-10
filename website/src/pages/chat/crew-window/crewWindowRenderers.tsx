/** The crew window's row set: the dashboard's shared rows that draw from the
 *  row itself, and nothing that reads the hub's OWN state.
 *
 *  An ALLOWLIST, so a shared row added later falls back to the store-free SDK
 *  default until someone decides it is safe for a peer's rows. Left out today:
 *  - the workflow and sub-agent launch cards read the run by id from the hub's
 *    own API, where a peer's run id names nothing (or another run), so a launch
 *    row draws as the generic tool line instead;
 *  - a sent file plays from the hub's own outbox by file name, which would be a
 *    different file (or none), so it draws as a labelled name only.
 *  The user and reply rows are the local chat's own bubbles, hover action bar
 *  included: Edit & Resend rewinds on the peer, Regenerate regenerates there,
 *  Quote stages the message for the next send.
 *  The window strips each peer row's decision record and file changes first:
 *  the shared reply's verdict thumbs and file chips would write or open this
 *  hub's own records. Code is copy-only for
 *  every row through the window's `ReadOnlyCodeCtx`. */
import { i18nT } from '../../../i18n/t'
import { formatTs, quoteMessageFor, renderAssistantBubble, type MessageRenderer } from '../../../app-sdk/messageRenderers'
import UserMessage from '../UserMessage'
import { renderUserContent } from '../ChatPageMessageContent'
import { fmtMessageTimeFull } from '../messageTime'
import { createTranscriptRenderers } from '../transcriptRenderers'
import type { ChatMessage } from '../../../types'

/** The slot key the window's shared rows and composer are mounted under. It
 *  names no local session (a local key has no `crew-window:` prefix), so their
 *  store reads come back empty instead of finding a hub session that happens
 *  to share the peer's key. */
export function crewWindowSlot(instanceId: string, key: string): string {
  return 'crew-window:' + JSON.stringify([instanceId, key])
}

/** Shared rows that draw from the row alone (no hub API, no hub file). */
export const PEER_SAFE_ROWS: ReadonlySet<string> = new Set([
  'skill_load', 'subagent_completion', 'tool', 'tool_completion', 'thinking_block',
  'nudge', 'recovery_inject', 'system_notice', 'workflow_completion', 'error',
])

function peerFileName(m: ChatMessage): string {
  try {
    const name = (JSON.parse(m.content) as { filename?: unknown }).filename
    return typeof name === 'string' ? name : ''
  } catch {
    return ''
  }
}

export function createCrewWindowRenderers(o: {
  instanceId: string
  /** The crew's display name, for the sent-file row. */
  name: string
  key: string
  canRewind: (m: ChatMessage) => boolean
  /** Edit & Resend on a user row: rewind the peer's session to `ts`. */
  /** `false`: not taken now (the peer is busy); the bubble keeps its editor open. */
  onRewind: (ts: string, content: string) => boolean
  /** The reply that offers Regenerate (the newest, on an idle turn), or null. */
  regenerateRow: ChatMessage | null
  onRegenerate: () => void
}): readonly MessageRenderer[] {
  const shared = createTranscriptRenderers({ slot: crewWindowSlot(o.instanceId, o.key) })
  return [
    {
      id: 'user',
      roles: ['user'],
      // The local chat's bubble and hover row (Quote, Copy, More). No copy
      // link or pin: each names a session or record on THIS machine.
      render: (m, ctx) => ctx.wrapper(
        <UserMessage
          content={m.content}
          meta={m.meta}
          timestamp={formatTs(m.ts)}
          timestampTitle={fmtMessageTimeFull(m.ts)}
          renderContent={(c, mt) => renderUserContent({ content: c, meta: mt })}
          canEdit={o.canRewind(m)}
          slotRunning={ctx.running}
          messageIndex={ctx.index}
          messageTs={m.ts || ''}
          onEditResend={(_i, ts, content) => o.onRewind(ts, content)}
          onQuoteMessage={quoteMessageFor(m, ctx, 'user')}
        />,
        true,
      ),
    },
    {
      id: 'file',
      roles: ['file'],
      render: (m, ctx) => {
        const name = peerFileName(m)
        return name
          ? ctx.row(<div className="text-muted break-words" data-testid="crew-window-file">{i18nT('pages.chat.crewWindow.file_on_crew', { file: name, name: o.name })}</div>)
          : null
      },
    },
    ...shared.filter(r => PEER_SAFE_ROWS.has(r.id)),
    // After the shared rows: their assistant-role refinements (system notice,
    // workflow completion) must win over this reply row.
    {
      id: 'assistant',
      roles: ['assistant', 'streaming'],
      render: (m, ctx) => {
        const bubble = renderAssistantBubble(m, ctx, undefined, {
          onRegenerate: m === o.regenerateRow ? o.onRegenerate : undefined,
        })
        return bubble === null ? null : ctx.wrapper(<div data-testid="crew-window-assistant">{bubble}</div>)
      },
    },
  ]
}
