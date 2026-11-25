import { Check } from 'lucide-react'
import { i18nT } from '../../i18n/t'

/** Added to the Quote button while it shows the done state. The action row
 *  (`ICON_ACTION_ROW_CLS`) pins every button to a square icon cell; the check
 *  and "Quoted" need the button to grow to its content instead, or the label
 *  spills over Copy beside it. */
export const QUOTED_BTN_CLS = '!w-auto !px-1.5'

/** A message row's done state after Quote: a check and "Quoted", read out
 *  politely, shared by user and assistant rows (`useDoneFlash`). */
export default function QuotedMark() {
  return (
    <>
      <Check size={14} className="text-ok shrink-0" aria-hidden />
      <span role="status" className="text-[12px] leading-4 text-muted whitespace-nowrap">{i18nT('pages.chat.assistantMessage.quoted')}</span>
    </>
  )
}
