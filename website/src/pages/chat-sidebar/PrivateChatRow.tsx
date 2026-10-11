/** The body of a "New incognito chat" / "New temporary chat" menu row.
 *
 *  Every create menu that offers the two private modes (the + New caret menu,
 *  the list-view folder menu, the board-column folder menu) renders this one
 *  body inside its own Item primitive, so the label, the one-line memory hint
 *  and the icon cannot drift between surfaces. The hint says what the mode does
 *  with memory and nothing else: both modes keep their transcript in History
 *  (docs/decisions/2026-09-25-incognito-and-temporary-chats-keep-their-transcript.md),
 *  so "learns nothing new" means nothing new reaches memory, not "no record".
 *
 *  A row that carries a guide location passes the same label as `children`:
 *  the UI index reads a registered control's label from the text written at
 *  its own render site, so it must be visible there. */
import type { ReactNode } from 'react'
import { EyeOff, VenetianMask } from 'lucide-react'
import { i18nT } from '../../i18n/t'

export type PrivateChatKind = 'incognito' | 'temporary'

export function PrivateChatRow({ kind, children }: { kind: PrivateChatKind; children?: ReactNode }) {
  const incognito = kind === 'incognito'
  const Icon = incognito ? EyeOff : VenetianMask
  const label = incognito ? i18nT('pages.chatSidebar.new_incognito_chat') : i18nT('pages.chatSidebar.new_temporary_chat')
  const hint = incognito ? i18nT('pages.chatSidebar.incognito_hint') : i18nT('pages.chatSidebar.temporary_hint')
  return (
    <>
      <Icon size={13} className={`${incognito ? 'text-warn' : 'text-aim'} mt-[3px] shrink-0`} aria-hidden="true" />
      <span className="flex min-w-0 flex-col gap-px">
        <span>{children ?? label}</span>
        <span className="whitespace-normal text-[11px] leading-snug text-muted">{hint}</span>
      </span>
    </>
  )
}
