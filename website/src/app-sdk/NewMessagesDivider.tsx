/**
 * The line above the first message a reader has not seen: "N new messages",
 * drawn in the accent colour between two hairlines. It marks where the unread
 * run starts; it is not a status, so it never wears a status colour.
 *
 * Rendered by ChatMessageList inside the first unseen message's own row (so
 * the virtualizer measures it as part of that row), and found by the host's
 * landing through `data-unread-divider`.
 */
import { i18nT } from '../i18n/t'

export const UNREAD_DIVIDER_ATTR = 'data-unread-divider'

export default function NewMessagesDivider({ count, contentWidth }: { count: number; contentWidth: string }) {
  const label = i18nT('appSdk.newMessagesDivider.new_messages', { count })
  return (
    <div
      {...{ [UNREAD_DIVIDER_ATTR]: '' }}
      role="separator"
      aria-label={label}
      className="px-4 mx-auto w-full pt-3 pb-1 flex items-center gap-3 text-[11.5px] font-semibold text-accent select-none"
      style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}
    >
      <span aria-hidden className="flex-1 h-px bg-accent opacity-45" />
      <span>{label}</span>
      <span aria-hidden className="flex-1 h-px bg-accent opacity-45" />
    </div>
  )
}
