/** ONE owner of the delivery-warning strip both chat surfaces render above the composer.
 *
 *  The resolvers behind it are already single-owner, but this decision-and-copy layer was spelled
 *  twice, and every round of review feedback on the caption, the affordance labels or the dismiss
 *  note had to be applied to both copies by hand -- which is what lets them drift apart.
 */
import { Btn } from './ui'
import { i18nT } from '../i18n/t'

/** Does dismissing this warning also drop an unconfirmed send? Decides both the destructive label
 *  and whether the consequence is stated at all. */
const dismissDropsBubble = (delivered: boolean, discardable: boolean): boolean =>
  !delivered && !discardable

const EXIT = 'p-0 border-none bg-transparent hover:bg-transparent hover:border-transparent'
  + ' active:scale-100 text-[12px] leading-5 underline shrink-0'
/** The harmless exits: muted, because acknowledging a warning costs the user nothing. */
const EXIT_MUTED = `${EXIT} text-muted hover:text-text`
/** The exits that DESTROY the only copy of a possibly-undelivered send. Muted styling made a total
 *  work loss read as the same weight as dismissing a nag, so these carry the strip's warn colour. */
const EXIT_DESTRUCTIVE = `${EXIT} font-medium text-warn hover:text-warn`

export interface DeliveryWarningStripProps {
  /** Unique per surface: two strips must never claim one `aria-describedby` target. */
  noteId: string
  delivered: boolean
  discardable: boolean
  onDiscard: () => void
  onDismiss: () => void
}

export const DeliveryWarningStrip = ({
  noteId, delivered, discardable, onDiscard, onDismiss,
}: DeliveryWarningStripProps) => {
  const dropsBubble = dismissDropsBubble(delivered, discardable)
  return (
    <div className="pt-1.5 text-[12px] leading-5 text-warn">
      {/* Wraps rather than truncates: the action carries `shrink-0`, so at 320px a full-length
          caption drove horizontal overflow, and clipping a data-safety sentence is worse. */}
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span role="status" className="min-w-0">
          {i18nT(delivered ? 'pages.chatPage.delivery_delivered' : 'pages.chatPage.delivery_unconfirmed_resend')}
        </span>
        {discardable ? (
          <Btn
            onClick={onDiscard}
            data-testid="delivery-exit-discard"
            /* Only the UNDELIVERED arm is a total loss: it drops the row AND clears the text. Once
               delivery is proven the words survive in the transcript, so clearing is harmless. */
            className={delivered ? EXIT_MUTED : EXIT_DESTRUCTIVE}
          >
            {i18nT(delivered ? 'pages.chatPage.delivery_clear' : 'pages.chatPage.delivery_discard')}
          </Btn>
        ) : (
          <Btn
            onClick={onDismiss}
            data-testid="delivery-exit-dismiss"
            aria-describedby={dropsBubble ? noteId : undefined}
            className={EXIT_MUTED}
          >
            {i18nT(dropsBubble ? 'pages.chatPage.delivery_remove' : 'app.dismiss')}
          </Btn>
        )}
      </div>
      {/* Carries the strip's warn colour and weight, not muted body text: this consequence is
          destructive, and a tooltip renders on neither touch nor keyboard focus. */}
      {dropsBubble && (
        <p id={noteId} className="mt-0.5 font-medium text-warn">
          {i18nT('pages.chatPage.delivery_unconfirmed_dismiss_note')}
        </p>
      )}
    </div>
  )
}
