/** The delivery-warning strip both chat surfaces render above the composer while a send's
 *  delivery is in doubt. */
import { Btn } from './ui'
import { i18nT } from '../i18n/t'

const EXIT = 'shrink-0 px-2 py-0.5 text-[12px] leading-5'

export interface DeliveryWarningStripProps {
  /** Unique per surface: two strips must never claim one `aria-describedby` target. */
  noteId: string
  onDismiss: () => void
}

export const DeliveryWarningStrip = ({ noteId, onDismiss }: DeliveryWarningStripProps) => (
  <div className="pt-1.5 text-[12px] leading-5">
    {/* Wraps rather than truncates: the exit carries `shrink-0`, so at 320px a full-length
        caption drove horizontal overflow, and clipping a data-safety sentence is worse. */}
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <span role="status" className="min-w-0 text-warn">
        {i18nT('pages.chatPage.delivery_unconfirmed_resend')}
      </span>
      <Btn onClick={onDismiss} data-testid="delivery-exit-dismiss" aria-describedby={noteId} className={EXIT}>
        {i18nT('pages.chatPage.delivery_remove')}
      </Btn>
    </div>
    {/* A tooltip renders on neither touch nor keyboard focus, so the consequence is stated inline. */}
    <p id={noteId} className="mt-0.5 text-muted">
      {i18nT('pages.chatPage.delivery_unconfirmed_dismiss_note')}
    </p>
  </div>
)
