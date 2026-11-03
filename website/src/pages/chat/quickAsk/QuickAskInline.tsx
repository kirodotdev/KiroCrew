import { ChevronRight } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { askIcon, askTitle } from './QuickAskParts'
import type { QuickAsk } from './types'

/**
 * V1, folded: the one-line hint a surface shows while the ask's sheet is
 * folded away (Esc, Not now) or open on the other surface. One click opens
 * the sheet here. A sign-in reads as its own card ("Secure sign-in / Sign in
 * to <host>", Enter): the person answers in the sheet, never in the page.
 */
export default function QuickAskInline({ ask, onOpen }: { ask: QuickAsk; onOpen: () => void }) {
  const Icon = askIcon(ask.kind)
  const signIn = ask.kind === 'credentials'
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`${i18nT('pages.chat.quickAsk.waiting_hint')}: ${askTitle(ask)}`}
      data-testid="quick-ask-inline"
      className="flex w-full items-center gap-2 border-0 border-t border-accent/50 bg-accent-subtle/60 px-3 py-2 text-left text-[13px] text-text-strong cursor-pointer hover:bg-accent-subtle"
    >
      {signIn ? (
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-subtle text-accent">
          <Icon size={16} aria-hidden="true" />
        </span>
      ) : <Icon size={14} aria-hidden="true" className="shrink-0 text-accent" />}
      {signIn ? (
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="text-[11px] font-medium text-muted">{i18nT('pages.chat.quickAsk.secure_sign_in')}</span>
          <span className="truncate font-semibold">{askTitle(ask)}</span>
        </span>
      ) : <span className="min-w-0 flex-1 font-medium">{i18nT('pages.chat.quickAsk.waiting_hint')}</span>}
      {signIn ? (
        <span className="shrink-0 rounded-md bg-accent px-3 py-1.5 text-[13px] font-semibold text-accent-fg">{i18nT('pages.chat.quickAsk.enter')}</span>
      ) : (
        <span className="inline-flex shrink-0 items-center gap-0.5 font-semibold text-accent">
          {i18nT('pages.chat.quickAsk.open')}
          <ChevronRight size={14} aria-hidden="true" />
        </span>
      )}
    </button>
  )
}
