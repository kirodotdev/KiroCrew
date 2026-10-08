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
 *  The user row is the shared one plus the peer's rewind. The reply row is
 *  the peer's plain text: the shared reply's verdict thumbs and file chips
 *  would write or open this hub's own records. Code is copy-only for every
 *  row through the window's `ReadOnlyCodeCtx`. */
import type { ReactNode } from 'react'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import { defaultMessageRenderers, type MessageRenderer } from '../../../app-sdk/messageRenderers'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { isHiddenInvisibleAssistantRow } from '../../../utils/invisibleText'
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

const defaultUser = defaultMessageRenderers.find(r => r.id === 'user')!

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
  onRewind: (m: ChatMessage) => void
  rewindDisabled: boolean
}): readonly MessageRenderer[] {
  const shared = createTranscriptRenderers({ slot: crewWindowSlot(o.instanceId, o.key) })
  return [
    {
      id: 'user',
      roles: ['user'],
      render: (m, ctx) => defaultUser.render(m, {
        ...ctx,
        wrapper: (children: ReactNode, isUser?: boolean) => ctx.wrapper(
          <>
            {children}
            {o.canRewind(m) && (
              <Btn disabled={o.rewindDisabled} onClick={() => { if (!o.rewindDisabled) o.onRewind(m) }}>
                {i18nT('pages.chat.crewWindow.rewind')}
              </Btn>
            )}
          </>,
          isUser,
        ),
      }),
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
    // workflow completion) must win over this plain reply row.
    {
      id: 'assistant',
      roles: ['assistant', 'streaming'],
      render: (m, ctx) => (isHiddenInvisibleAssistantRow(m)
        ? null
        : ctx.row(<div data-testid="crew-window-assistant"><MarkdownRenderer content={m.content || ''} streaming={m.role === 'streaming'} softBreaks /></div>)),
    },
  ]
}
