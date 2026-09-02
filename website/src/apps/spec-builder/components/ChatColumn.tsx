// ChatColumn — the center column. Binds Kiro Crew's native chat renderer
// (ChatEmbed → ChatMessageList: markdown, tool cards, options, streaming) to
// this spec's slot. Uses the new frameless + startAtBottom ChatEmbed props
// instead of the CSS overrides the external app relied on.
import ChatEmbed from '../../../app-sdk/ChatEmbed'

import { i18nT } from '../../../i18n/t'
export interface ChatColumnProps {
  name: string
  /**
   * The slot key from the detail payload. Slot keys are per-creation, so
   * deriving one from the NAME mounted the embed against a previous spec's
   * transcript once a name was reused after a delete. The server is the only
   * authority on which session belongs to this spec; the name-derived form
   * remains as a fallback for entries that predate the persisted key.
   */
  slotKey?: string
  /**
   * Routes the composer through the APP's message endpoint instead of the
   * generic `POST /api/chat`. That endpoint carries the rendered `spec_dir` and
   * refuses a stale identity, so a tab whose spec was deleted elsewhere cannot
   * resurrect an unscoped slot whose approved tools would run outside the
   * project.
   */
  onSend: (message: string) => Promise<unknown>
}

export default function ChatColumn({ name, slotKey, onSend }: ChatColumnProps) {
  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <ChatEmbed
        slotKey={slotKey || 'spec-builder-' + name}
        onSend={onSend}
        placeholder={i18nT('apps.specBuilder.components.chatColumn.reply_to_the_spec_agent')}
        frameless
        startAtBottom
      />
    </div>
  )
}
